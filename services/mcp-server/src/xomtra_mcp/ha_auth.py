"""Sign-in with a Home Assistant account (ADR 053).

Home Assistant has an IndieAuth-style authorization code flow: the client is not
registered, its ``client_id`` is a URL and the ``redirect_uri`` must be on the same host.
There is no ID token, so the identity comes from the WebSocket command
``auth/current_user`` called with the user's access token. HA tokens carry all rights of
the user (no scopes): xomtra uses them only for that call.

* MCP (``XOMTRA_AUTH_MODE=ha``, public access): :class:`HomeAssistantProvider`, a FastMCP
  ``OAuthProxy`` with HA upstream, and :class:`HomeAssistantTokenVerifier`.
* Admin GUI: :class:`HomeAssistantLogin` (code -> token -> user -> token revoked).

Roles: HA administrators (and the owner) get the admin role, other users the user role
(``XOMTRA_HA_ROLE_MAP``); an optional allow-list limits who may sign in at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2
from fastmcp.server.auth import TokenVerificationError, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from mcp.server.auth.provider import TokenError

from hacore import HAClient
from hacore.errors import HAAuthError, HAError

logger = logging.getLogger(__name__)

IDP = "ha"
# Only Home Assistant administrators by default; other users are refused until an
# administrator maps them (e.g. user=xomtra.Reader).
DEFAULT_ROLE_MAP = "admin=xomtra.Admin,user="
USER_CACHE_SECONDS = 60


class LoginError(Exception):
    """Sign-in failed; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class RoleMap:
    """xomtra role of HA administrators (incl. the owner) and of other HA users.

    ``None`` refuses that kind of user (e.g. ``user=`` lets only administrators in).
    """

    admin: str | None = "xomtra.Admin"
    user: str | None = None

    @classmethod
    def parse(cls, text: str) -> RoleMap:
        values: dict[str, str | None] = {}
        for item in text.split(","):
            if not item.strip():
                continue
            key, sep, role = item.partition("=")
            key = key.strip().lower()
            if not sep or key not in {"admin", "user"}:
                raise ValueError(
                    f"XOMTRA_HA_ROLE_MAP: expected admin=<role>,user=<role>, got {item!r}"
                )
            values[key] = role.strip() or None
        return cls(admin=values.get("admin"), user=values.get("user"))


@dataclass(frozen=True, slots=True)
class HAUser:
    id: str
    name: str
    is_owner: bool
    is_admin: bool

    @classmethod
    def from_reply(cls, reply: dict[str, Any]) -> HAUser:
        return cls(
            id=str(reply["id"]),
            name=str(reply.get("name") or ""),
            is_owner=bool(reply.get("is_owner")),
            is_admin=bool(reply.get("is_admin")),
        )

    def claims(self, roles: list[str]) -> dict[str, Any]:
        return {
            "sub": self.id,
            "name": self.name,
            "preferred_username": self.name,
            "roles": roles,
            "idp": IDP,
        }


def roles_for(user: HAUser, role_map: RoleMap, allowed: list[str] | None = None) -> list[str]:
    """xomtra roles of an HA user; empty = the user may not sign in."""
    if allowed:
        wanted = {a.casefold() for a in allowed}
        if user.id.casefold() not in wanted and user.name.casefold() not in wanted:
            return []
    role = role_map.admin if (user.is_admin or user.is_owner) else role_map.user
    return [role] if role else []


async def fetch_current_user(ha_url: str, access_token: str) -> HAUser:
    """Identity behind an HA access token. Raises HAAuthError if HA rejects it."""
    async with HAClient(ha_url, access_token, timeout=10) as ha:
        reply = await ha.send_command("auth/current_user")
    return HAUser.from_reply(reply)


class HomeAssistantTokenVerifier(TokenVerifier):
    """Checks an HA access token with ``auth/current_user`` (cached) and maps roles."""

    def __init__(
        self,
        *,
        ha_url: str,
        role_map: RoleMap,
        allowed_users: list[str] | None = None,
        cache_seconds: int = USER_CACHE_SECONDS,
    ) -> None:
        super().__init__()
        self.ha_url = ha_url
        self.role_map = role_map
        self.allowed_users = allowed_users or []
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def user_claims(self, token: str) -> dict[str, Any] | None:
        """Claims of the token's user, or None if HA rejects the token or the user."""
        try:
            user = await fetch_current_user(self.ha_url, token)
        except HAAuthError:
            return None
        except HAError as err:
            # HA unreachable: not a verdict on the token.
            raise TokenVerificationError("Home Assistant is not reachable") from err
        roles = roles_for(user, self.role_map, self.allowed_users)
        if not roles:
            logger.info("HA user %s is not allowed to use xomtra", user.id)
            return None
        return user.claims(roles)

    async def verify_token(self, token: str) -> AccessToken | None:
        now = time.monotonic()
        cached = self._cache.get(token)
        if cached and cached[0] > now:
            return cached[1]
        claims = await self.user_claims(token)
        result = (
            None
            if claims is None
            else AccessToken(
                token=token,
                client_id=f"ha:{claims['sub']}",
                scopes=[],
                expires_at=None,
                subject=claims["sub"],
                claims=claims,
            )
        )
        if len(self._cache) > 1000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[token] = (now + self.cache_seconds, result)
        return result


class HomeAssistantProvider(OAuthProxy):
    """OAuth proxy for Claude with Home Assistant as the upstream identity provider.

    HA is a public client without secret, PKCE or resource indicators. Claude registers
    with xomtra (DCR, limited to Claude's redirect URIs) and gets xomtra's own tokens;
    the HA tokens stay encrypted in the proxy's storage.
    """

    def __init__(
        self,
        *,
        base_url: str,
        ha_url: str,
        ha_auth_url: str,
        jwt_signing_key: str,
        role_map: RoleMap,
        allowed_users: list[str] | None = None,
        allowed_client_redirect_uris: list[str] | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        self._verifier = HomeAssistantTokenVerifier(
            ha_url=ha_url, role_map=role_map, allowed_users=allowed_users
        )
        super().__init__(
            upstream_authorization_endpoint=f"{ha_auth_url.rstrip('/')}/auth/authorize",
            upstream_token_endpoint=f"{ha_url.rstrip('/')}/auth/token",
            upstream_client_id=f"{base}/",
            upstream_client_secret=None,
            token_endpoint_auth_method="none",  # noqa: S106 - public client, no secret
            token_verifier=self._verifier,
            base_url=base,
            forward_pkce=False,
            forward_resource=False,
            jwt_signing_key=jwt_signing_key,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
        )

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        # Called when Claude exchanges its code: refuse users without a role here, so the
        # connector reports a failed sign-in instead of looping on 401.
        claims = await self._verifier.user_claims(str(idp_tokens.get("access_token", "")))
        if claims is None:
            raise TokenError(
                "invalid_grant", "This Home Assistant user is not allowed to use xomtra"
            )
        return claims


@dataclass(slots=True)
class HomeAssistantLogin:
    """Admin GUI sign-in through HA; the HA token is revoked right after the lookup."""

    ha_url: str
    ha_auth_url: str
    client_id: str
    redirect_uri: str
    http: httpx2.AsyncClient | None = None

    def authorize_url(self, state: str) -> str:
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{self.ha_auth_url.rstrip('/')}/auth/authorize?{urlencode(query)}"

    async def _client(self) -> httpx2.AsyncClient:
        if self.http is None:
            self.http = httpx2.AsyncClient(timeout=10)
        return self.http

    async def exchange(self, code: str) -> HAUser:
        client = await self._client()
        try:
            resp = await client.post(
                f"{self.ha_url.rstrip('/')}/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.client_id,
                },
            )
        except httpx2.HTTPError as err:
            raise LoginError("Home Assistant is not reachable") from err
        if resp.status_code != 200:
            raise LoginError("Home Assistant did not issue a token")
        tokens = resp.json()
        access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
        if not isinstance(access, str):
            raise LoginError("Home Assistant returned no access token")
        try:
            return await fetch_current_user(self.ha_url, access)
        except HAError as err:
            raise LoginError("The Home Assistant user could not be read") from err
        finally:
            if isinstance(refresh, str):
                await self.revoke(refresh)

    async def revoke(self, refresh_token: str) -> None:
        """Revoke the HA session (refresh token and its access tokens); best effort."""
        client = await self._client()
        try:
            await client.post(
                f"{self.ha_url.rstrip('/')}/auth/revoke", data={"token": refresh_token}
            )
        except httpx2.HTTPError:
            logger.warning("Could not revoke the Home Assistant token of an admin sign-in")
