"""Server configuration from environment variables (prefix ``XOMTRA_``).

Nothing installation specific has defaults here: tenant/client IDs, URLs and secrets
always come from the environment (``deploy/compose/.env``).
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from hacore.ha_connection import Resolver
from xomtra_mcp.ha_auth import DEFAULT_ROLE_MAP, RoleMap

# Redirect URIs used by Claude (claude.ai / Claude Desktop) for remote MCP connectors.
CLAUDE_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XOMTRA_", extra="ignore")

    # Who signs in (admin GUI; MCP in public access): Entra ID, a Home Assistant account
    # (ADR 053) or nobody (local development on loopback only).
    auth_mode: Literal["entra", "ha", "none"] = "entra"
    # public = OAuth for Claude through Caddy on a public host (ADR 026/028);
    # local = MCP only in the LAN with personal tokens from the admin GUI (ADR 056/059).
    access_mode: Literal["public", "local"] = "public"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    # Public URL of this server as seen by Claude, e.g. https://mcp.example.org
    base_url: AnyHttpUrl | None = None

    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: SecretStr | None = None
    # Scope names WITHOUT the api://<client-id>/ prefix.
    entra_required_scopes: Annotated[list[str], NoDecode] = ["mcp.access"]
    # Secret used to sign the tokens FastMCP issues to Claude and to encrypt its
    # OAuth state. Must be stable across restarts, otherwise users must reconnect.
    jwt_signing_key: SecretStr | None = None
    allowed_client_redirect_uris: Annotated[list[str], NoDecode] = CLAUDE_REDIRECT_URIS

    # Optional Home Assistant connection (read-only use in the PoC). Without it in the
    # environment, the one entered in the admin GUI (ADR 077), decrypted with
    # settings_key.
    ha_url: str | None = None
    ha_token: SecretStr | None = None
    settings_key: SecretStr | None = None
    # HA URL as the user's browser reaches it (sign-in page); default XOMTRA_HA_URL.
    ha_auth_url: str | None = None
    # Optional allow-list of HA users (id or name); empty = every HA user with a role.
    ha_allowed_users: Annotated[list[str], NoDecode] = []
    # xomtra role of HA administrators and of other HA users (empty value = refused).
    ha_role_map: str = DEFAULT_ROLE_MAP

    # Access policy (ADR 038); default: the packaged policy.yaml.
    policy_file: Path | None = None
    # Roles of the (unauthenticated) caller with XOMTRA_AUTH_MODE=none, local dev only.
    dev_roles: Annotated[list[str], NoDecode] = ["xomtra.Admin"]

    # Data tools over the lake (connection from XOMTRA_LAKE_* / XOMTRA_S3_*).
    data_enabled: bool = True
    query_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # Time zone of daily buckets in get_timeseries.
    timezone: str = "UTC"
    # Default guidelines for answering (tone, role, data rules; ADR 073, 080). The
    # operator may turn them off; a central theme (ADR 075) still applies.
    default_guidelines: bool = True
    # Style of the default guidelines: friendly (light humour) or pub (crude, ADR 080).
    answer_style: Literal["friendly", "pub"] = "friendly"

    # Audit database (ADR 040). Required with Entra auth: every call must be audited.
    audit_pg_host: str | None = None
    audit_pg_port: int = 5432
    audit_pg_db: str = "audit"
    audit_pg_user: str = "xomtra_audit"
    audit_pg_password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _csv_lists(cls, data: object) -> object:
        if isinstance(data, dict):
            for key in (
                "entra_required_scopes",
                "allowed_client_redirect_uris",
                "dev_roles",
                "ha_allowed_users",
            ):
                if key in data:
                    data[key] = _split_csv(data[key])
            for key in ("policy_file", "base_url", "ha_url", "ha_token", "settings_key"):
                if data.get(key) == "":
                    data[key] = None
        return data

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auth_mode == "none":
            if not _is_loopback(self.host):
                raise ValueError(
                    "XOMTRA_AUTH_MODE=none is only allowed on a loopback address "
                    "(local development)."
                )
            return self
        RoleMap.parse(self.ha_role_map)  # fail early on a malformed map
        if self.access_mode == "local":
            # Personal tokens live in the audit database (ADR 059); no OAuth here.
            required: tuple[str, ...] = ("audit_pg_host", "audit_pg_password")
        elif self.auth_mode == "ha":
            required = ("base_url", "ha_url", "jwt_signing_key", "audit_pg_host")
            required += ("audit_pg_password",)
        else:
            required = (
                "base_url",
                "entra_tenant_id",
                "entra_client_id",
                "entra_client_secret",
                "jwt_signing_key",
                "audit_pg_host",
                "audit_pg_password",
            )
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            env = ", ".join(f"XOMTRA_{m.upper()}" for m in missing)
            what = (
                "Local access"
                if self.access_mode == "local"
                else {"entra": "Entra auth", "ha": "Home Assistant auth"}[self.auth_mode]
            )
            raise ValueError(f"{what} requires: {env}")
        key = self.jwt_signing_key
        if key is not None and len(key.get_secret_value()) < 32:
            raise ValueError("XOMTRA_JWT_SIGNING_KEY must have at least 32 characters")
        if self.entra_tenant_id in {"common", "organizations", "consumers"}:
            raise ValueError("Use a specific tenant ID, multi-tenant sign-in is not allowed")
        return self

    @property
    def role_map(self) -> RoleMap:
        return RoleMap.parse(self.ha_role_map)

    @property
    def browser_ha_url(self) -> str | None:
        return self.ha_auth_url or self.ha_url

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    def ha_resolver(self) -> Resolver:
        key = self.settings_key.get_secret_value() if self.settings_key else None
        return Resolver(
            self.ha_url,
            self.ha_token.get_secret_value() if self.ha_token else None,
            self.audit_dsn if key else None,
            key,
        )

    @property
    def audit_dsn(self) -> str | None:
        if not (self.audit_pg_host and self.audit_pg_password):
            return None
        parts = {
            "host": self.audit_pg_host,
            "port": str(self.audit_pg_port),
            "dbname": self.audit_pg_db,
            "user": self.audit_pg_user,
            "password": self.audit_pg_password.get_secret_value(),
            "application_name": "xomtra-mcp",
        }
        return " ".join(f"{k}={_libpq(v)}" for k, v in parts.items())


def _libpq(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
