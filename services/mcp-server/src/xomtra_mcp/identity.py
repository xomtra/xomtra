"""Caller identity derived from the validated access token."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastmcp.server.dependencies import get_access_token


@dataclass(frozen=True, slots=True)
class Identity:
    authenticated: bool
    subject: str | None = None
    name: str | None = None
    username: str | None = None
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = field(default_factory=tuple)
    # Who vouched for the identity: entra | ha | token (ADR 040/053/059); dev without auth.
    idp: str | None = None
    # End of the connector's sign-in (Home Assistant sessions, ADR 081), ISO 8601 UTC.
    session_expires_at: str | None = None


def identity_from_claims(claims: dict[str, Any], scopes: list[str]) -> Identity:
    # Upstream claims may be nested under "upstream_claims" (FastMCP-issued JWT) or be
    # the token payload itself. Home Assistant users and personal tokens carry "idp".
    upstream = claims.get("upstream_claims")
    source: dict[str, Any] = upstream if isinstance(upstream, dict) else claims
    roles = source.get("roles") or []
    return Identity(
        authenticated=True,
        subject=source.get("oid") or source.get("sub"),
        name=source.get("name"),
        username=source.get("preferred_username") or source.get("upn") or source.get("email"),
        roles=tuple(str(r) for r in roles) if isinstance(roles, list) else (),
        scopes=tuple(scopes),
        idp=str(source.get("idp") or claims.get("idp") or "entra"),
        session_expires_at=_text(source.get("session_expires_at")),
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def current_identity() -> Identity:
    token = get_access_token()
    if token is None:
        return Identity(authenticated=False)
    return identity_from_claims(dict(token.claims or {}), list(token.scopes))
