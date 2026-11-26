"""Sign-in with a Home Assistant account (ADR 053).

Home Assistant has an IndieAuth-style authorization code flow: the client is not
registered, its ``client_id`` is a URL and the ``redirect_uri`` must be on the same host.
There is no ID token, so the identity comes from the WebSocket command
``auth/current_user`` called with the user's access token. HA tokens carry all rights of
the user (no scopes): xomtra uses them only for that call.

* MCP (``XOMTRA_AUTH_MODE=ha``, public access): :class:`HomeAssistantProvider`, a FastMCP
  ``OAuthProxy`` with HA upstream, and :class:`HomeAssistantTokenVerifier`.
* Admin GUI: :class:`HomeAssistantLogin` (code -> token -> user -> token revoked).

Roles: HA administrators (and the owner) get the admin role. Other HA users get the role
an admin gave them on the admin page Access (ADR 081), else the user role of
``XOMTRA_HA_ROLE_MAP`` (default: none). An optional allow-list limits who may sign in at
all. For the Claude connector xomtra also requires two-factor authentication in HA
(``XOMTRA_HA_REQUIRE_MFA``) and ends a sign-in after ``XOMTRA_SESSION_DAYS`` (ADR 081).

No HA token of a user stays in xomtra (ADR 081): right after the sign-in of the
connector xomtra records a session (:mod:`xomtra_mcp.access`) and revokes the user's HA
token, like the admin GUI does. Later requests are checked against that session, the
access list and, when xomtra's own HA connection may list users, against HA's user
directory (removed or deactivated users and changed admin rights take effect within
minutes).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from urllib.parse import urlencode

import httpx2
from fastmcp.server.auth import TokenVerificationError, TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from mcp.server.auth.provider import TokenError

from hacore import HAClient
from hacore.errors import HAAuthError, HAError
from xomtra_mcp.access import (
    DEFAULT_SESSION_DAYS,
    AccessStore,
    AccessStoreError,
    SessionAccess,
)
from xomtra_mcp.audit import AuditError, AuditRecord, AuditSink

logger = logging.getLogger(__name__)

IDP = "ha"
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
    user: str | None = "xomtra.Reader"

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
    # At least one multi-factor module (e.g. TOTP) is enabled for the user.
    mfa: bool = False

    @classmethod
    def from_reply(cls, reply: dict[str, Any]) -> HAUser:
        modules = reply.get("mfa_modules")
        return cls(
            id=str(reply["id"]),
            name=str(reply.get("name") or ""),
            is_owner=bool(reply.get("is_owner")),
            is_admin=bool(reply.get("is_admin")),
            mfa=isinstance(modules, list)
            and any(isinstance(m, dict) and m.get("enabled") for m in modules),
        )

    @property
    def admin(self) -> bool:
        return self.is_admin or self.is_owner

    def claims(self, roles: list[str]) -> dict[str, Any]:
        return {
            "sub": self.id,
            "name": self.name,
            "preferred_username": self.name,
            "roles": roles,
            "idp": IDP,
        }


def roles_for(
    user: HAUser,
    role_map: RoleMap,
    allowed: list[str] | None = None,
    listed: str | None = None,
) -> list[str]:
    """xomtra roles of an HA user; empty = the user may not sign in.

    ``listed`` is the role from the access list of the admin GUI (ADR 081); it applies
    to users who are not HA administrators and takes precedence over ``role_map.user``.
    """
    if allowed:
        wanted = {a.casefold() for a in allowed}
        if user.id.casefold() not in wanted and user.name.casefold() not in wanted:
            return []
    role = role_map.admin if user.admin else (listed or role_map.user)
    return [role] if role else []


def session_id(access_token: str) -> str | None:
    """Id of the HA refresh token behind an HA access token (its ``iss`` claim).

    Not a verification: the token is checked by HA (``auth/current_user``) before this
    value is used.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    iss = payload.get("iss") if isinstance(payload, dict) else None
    return iss if isinstance(iss, str) and 0 < len(iss) <= 128 else None


class RefusedError(Exception):
    """The HA user may not use xomtra now; the message is safe to show."""


async def delete_ha_session(ha_url: str, access_token: str, refresh_token_id: str) -> None:
    """Delete the HA refresh token (the whole HA session) as its own user; best effort."""
    try:
        async with HAClient(ha_url, access_token, timeout=10) as ha:
            await ha.send_command("auth/delete_refresh_token", refresh_token_id=refresh_token_id)
    except (HAError, TimeoutError):
        logger.warning("Could not delete the Home Assistant session of a closed sign-in")


async def revoke_ha_refresh_token(ha_url: str, refresh_token: str) -> bool:
    """Revoke an HA refresh token (and its access tokens) at ``/auth/revoke``."""
    try:
        async with httpx2.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{ha_url.rstrip('/')}/auth/revoke", data={"token": refresh_token}
            )
    except httpx2.HTTPError:
        return False
    return resp.status_code == 200


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    id: str
    name: str
    is_active: bool
    is_admin: bool


class HAUserDirectory:
    """HA's users as xomtra's own HA connection sees them (``config/auth/list``).

    Needs an administrator token; otherwise (or with HA unreachable) :meth:`users`
    returns None and the state recorded at the sign-in applies.
    """

    def __init__(
        self,
        connection: Callable[[], Awaitable[tuple[str, str] | None]],
        cache_seconds: int = 300,
    ) -> None:
        self._connection = connection
        self.cache_seconds = cache_seconds
        self._cached: tuple[float, dict[str, DirectoryUser] | None] | None = None

    async def users(self) -> dict[str, DirectoryUser] | None:
        now = time.monotonic()
        if self._cached and self._cached[0] > now:
            return self._cached[1]
        result: dict[str, DirectoryUser] | None = None
        conn = await self._connection()
        if conn is not None:
            try:
                async with HAClient(conn[0], conn[1], timeout=10) as ha:
                    reply = await ha.send_command("config/auth/list")
                result = {
                    str(u["id"]): DirectoryUser(
                        id=str(u["id"]),
                        name=str(u.get("name") or ""),
                        is_active=bool(u.get("is_active", True)),
                        is_admin=bool(u.get("is_owner"))
                        or "system-admin" in (u.get("group_ids") or []),
                    )
                    for u in reply
                    if isinstance(u, dict) and "id" in u
                }
            except (HAError, TimeoutError, TypeError, KeyError):
                logger.info("Home Assistant users could not be listed; using the sign-in state")
        self._cached = (now + self.cache_seconds, result)
        return result


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
        store: AccessStore | None = None,
        require_mfa: bool = False,
        session_days: int = DEFAULT_SESSION_DAYS,
        directory: HAUserDirectory | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        super().__init__()
        self.directory = directory
        self.audit = audit
        self._ended: set[str] = set()
        self.ha_url = ha_url
        self.role_map = role_map
        self.allowed_users = allowed_users or []
        self.cache_seconds = cache_seconds
        self.store = store
        self.require_mfa = require_mfa
        self.session_days = session_days
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def user_claims(self, token: str) -> dict[str, Any] | None:
        """Claims of the token's user, or None if the session, HA or the user refuse.

        With a store, a token whose sign-in has a session is checked against the session
        only (its HA token was revoked at the sign-in); otherwise HA checks the token.
        """
        try:
            sid = session_id(token) if self.store is not None else None
            state = await self._store_call(self._check, sid) if sid else None
            if state is not None:
                try:
                    return await self.session_claims(state)
                except RefusedError as why:
                    await self._session_ended(state, str(why))
                    raise
            return await self.checked_claims(token)
        except RefusedError as why:
            logger.info("Home Assistant sign-in refused: %s", why)
            return None

    async def _session_ended(self, state: SessionAccess, reason: str) -> None:
        """Audit a refused connector session once per reason (per server process)."""
        row = state.session
        key = f"{row.id}|{reason}"
        if self.audit is None or key in self._ended:
            return
        self._ended.add(key)
        record = AuditRecord(
            tool="connector:session_refused",
            decision="denied",
            outcome="denied",
            arguments={"session": row.id},
            subject=row.subject,
            username=row.name,
            idp=IDP,
            error=reason,
        )
        try:
            await self.audit.write(record)
        except AuditError:
            logger.warning("Could not audit a refused connector session")

    def _check(self, sid: str) -> SessionAccess | None:
        assert self.store is not None  # noqa: S101 - only called with a store
        return self.store.session_check(sid)

    async def session_claims(self, state: SessionAccess) -> dict[str, Any]:
        """Claims of a recorded session; raises :class:`RefusedError` if it may not go on."""
        row = state.session
        if not row.active():
            raise RefusedError("This sign-in has ended (disconnected or expired): connect again")
        is_admin = state.is_admin
        if self.directory is not None and (users := await self.directory.users()) is not None:
            current = users.get(row.subject)
            if current is None or not current.is_active:
                raise RefusedError("The Home Assistant user was removed or deactivated")
            is_admin = current.is_admin
        user = HAUser(row.subject, row.name or "", is_owner=False, is_admin=is_admin)
        roles = roles_for(user, self.role_map, self.allowed_users, state.role)
        if not roles:
            raise RefusedError(f"Home Assistant user {user.name or user.id} has no access")
        claims = user.claims(roles)
        claims["sid"] = row.id
        claims["session_expires_at"] = row.expires_at.astimezone(UTC).isoformat()
        return claims

    async def checked_claims(self, token: str) -> dict[str, Any]:
        """Claims of the token's user; raises :class:`RefusedError` with the reason."""
        try:
            user = await fetch_current_user(self.ha_url, token)
        except HAAuthError as err:
            raise RefusedError("Home Assistant did not accept the sign-in") from err
        except HAError as err:
            # HA unreachable: not a verdict on the token.
            raise TokenVerificationError("Home Assistant is not reachable") from err
        listed = await self._store_call(self._seen, user)
        roles = roles_for(user, self.role_map, self.allowed_users, listed)
        if not roles:
            raise RefusedError(
                f"Home Assistant user {user.name or user.id} has no access to xomtra yet: "
                "an administrator gives access on the admin page Access"
            )
        if self.require_mfa and not user.mfa:
            raise RefusedError(
                f"Home Assistant user {user.name or user.id} has no two-factor "
                "authentication: turn it on in the Home Assistant profile (Security) first"
            )
        claims = user.claims(roles)
        sid = session_id(token) if self.store is not None else None
        if sid is not None:
            row = await self._store_call(
                self.store.session_touch,  # type: ignore[union-attr]
                sid,
                IDP,
                user.id,
                user.name,
                self.session_days,
            )
            if not row.active():
                await delete_ha_session(self.ha_url, token, sid)
                raise RefusedError(
                    "This sign-in has ended (disconnected or expired): connect again"
                )
            claims["sid"] = sid
            claims["session_expires_at"] = row.expires_at.astimezone(UTC).isoformat()
        return claims

    def _seen(self, user: HAUser) -> str | None:
        assert self.store is not None  # noqa: S101 - only called with a store
        return self.store.ha_user_seen(user.id, user.name, user.admin)

    async def _store_call(self, fn: Any, *args: Any) -> Any:
        if self.store is None:
            return None
        try:
            return await asyncio.to_thread(fn, *args)
        except AccessStoreError as err:
            raise TokenVerificationError("The access store is unavailable") from err

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
        store: AccessStore | None = None,
        require_mfa: bool = True,
        session_days: int = DEFAULT_SESSION_DAYS,
        directory: HAUserDirectory | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        self._ha_url = ha_url
        self._audit = audit
        self._revoke_after_sign_in = store is not None
        self._verifier = HomeAssistantTokenVerifier(
            ha_url=ha_url,
            role_map=role_map,
            allowed_users=allowed_users,
            store=store,
            require_mfa=require_mfa,
            session_days=session_days,
            directory=directory,
            audit=audit,
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
            # HA sends no refresh_expires_in: Claude's tokens live as long as the session
            # (ADR 081), fixed from the sign-in. The HA token is revoked at the sign-in,
            # so nothing is refreshed at HA; every request is checked against the session.
            fallback_refresh_token_expiry_seconds=session_days * 86400,
            fastmcp_access_token_expiry_seconds=session_days * 86400 if store is not None else None,
        )

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        # Called when Claude exchanges its code: refuse users without a role here, so the
        # connector reports a failed sign-in instead of looping on 401.
        claims: dict[str, Any] | None = None
        reason: str | None = None
        try:
            claims = await self._verifier.checked_claims(str(idp_tokens.get("access_token", "")))
        except RefusedError as why:
            logger.info("Home Assistant sign-in refused: %s", why)
            reason = str(why)
        revoked = await self._drop_ha_token(idp_tokens)
        try:
            await self._audit_sign_in(claims, reason, revoked)
        except AuditError:
            # Every sign-in is audited (ADR 040): without the audit, no sign-in.
            reason = reason or "The audit is unavailable"
            claims = None
        if claims is not None:
            return claims
        # Raised outside the handler: TokenError is frozen and cannot take a __cause__.
        raise TokenError("invalid_grant", reason or "Sign-in refused")

    async def _drop_ha_token(self, idp_tokens: dict[str, Any]) -> bool | None:
        """ADR 081: keep no HA token of the user once the sign-in is checked.

        True/False: revoked or not; None: nothing to revoke (no store or no token).
        """
        refresh = idp_tokens.get("refresh_token")
        if not self._revoke_after_sign_in or not isinstance(refresh, str):
            return None
        if await revoke_ha_refresh_token(self._ha_url, refresh):
            return True
        logger.warning("Could not revoke the Home Assistant token of a connector sign-in")
        return False

    async def _audit_sign_in(
        self, claims: dict[str, Any] | None, reason: str | None, revoked: bool | None
    ) -> None:
        if self._audit is None:
            return
        arguments: dict[str, Any] = {"ha_token_revoked": revoked}
        if claims and claims.get("sid"):
            arguments["session"] = claims["sid"]
            arguments["ends"] = claims.get("session_expires_at")
        await self._audit.write(
            AuditRecord(
                tool="connector:sign_in",
                decision="allowed" if claims else "denied",
                outcome="ok" if claims else "denied",
                arguments=arguments,
                subject=(claims or {}).get("sub"),
                username=(claims or {}).get("name"),
                roles=list((claims or {}).get("roles") or []),
                idp=IDP,
                error=reason,
            )
        )


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
