"""Sign-in with Microsoft Entra ID (OpenID Connect, authorization code + PKCE).

The ID token is verified against the tenant's signing keys: issuer, audience, expiry,
tenant and the nonce of the login transaction. Only a user with the configured app role
gets a session.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx2
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

LOGIN = "https://login.microsoftonline.com"


class LoginError(Exception):
    """Sign-in failed; the message is safe to show."""


@dataclass(slots=True)
class LoginTransaction:
    state: str
    nonce: str
    verifier: str

    @classmethod
    def new(cls) -> LoginTransaction:
        return cls(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            verifier=secrets.token_urlsafe(48),
        )

    @property
    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(slots=True)
class EntraOIDC:
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_uri: str
    http: httpx2.AsyncClient | None = None
    _keys: KeySet | None = field(default=None, init=False)
    _keys_at: float = field(default=0.0, init=False)

    @property
    def issuer(self) -> str:
        return f"{LOGIN}/{self.tenant_id}/v2.0"

    def authorize_url(self, tx: LoginTransaction) -> str:
        query = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": tx.state,
            "nonce": tx.nonce,
            "code_challenge": tx.challenge,
            "code_challenge_method": "S256",
        }
        return f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/authorize?{urlencode(query)}"

    async def _client(self) -> httpx2.AsyncClient:
        if self.http is None:
            self.http = httpx2.AsyncClient(timeout=10)
        return self.http

    async def _keyset(self, *, refresh: bool = False) -> KeySet:
        if self._keys is None or refresh or time.time() - self._keys_at > 3600:
            client = await self._client()
            resp = await client.get(f"{LOGIN}/{self.tenant_id}/discovery/v2.0/keys")
            resp.raise_for_status()
            self._keys = KeySet.import_key_set(resp.json())
            self._keys_at = time.time()
        return self._keys

    async def exchange(self, code: str, tx: LoginTransaction) -> dict[str, Any]:
        client = await self._client()
        resp = await client.post(
            f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": tx.verifier,
                "scope": "openid profile email",
            },
        )
        if resp.status_code != 200:
            raise LoginError("Entra did not issue a token")
        id_token = resp.json().get("id_token")
        if not isinstance(id_token, str):
            raise LoginError("Entra returned no ID token")
        return await self.verify(id_token, tx.nonce)

    async def verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            try:
                token = jwt.decode(id_token, await self._keyset(), algorithms=["RS256"])
            except JoseError:  # key rotation: refresh once
                token = jwt.decode(id_token, await self._keyset(refresh=True), algorithms=["RS256"])
            claims = jwt.JWTClaimsRegistry(
                iss={"essential": True, "value": self.issuer},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
                nonce={"essential": True, "value": nonce},
                tid={"essential": True, "value": self.tenant_id},
            )
            claims.validate(token.claims)
        except (JoseError, ValueError) as err:
            raise LoginError("The ID token is not valid") from err
        return dict(token.claims)
