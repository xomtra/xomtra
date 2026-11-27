"""Admin GUI settings (environment, prefix ``XOMTRA_``)."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from hacore.ha_connection import Resolver
from xomtra_mcp.ha_auth import DEFAULT_ROLE_MAP, RoleMap

PREFIX = "/admin"


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XOMTRA_", extra="ignore")

    # ingress: behind Home Assistant Ingress (add-on, ADR 070); HA signs the user in and
    # the Supervisor's proxy passes them in X-Remote-User-* headers.
    # Default: a Home Assistant account (ADR 081); Entra ID is optional.
    auth_mode: Literal["ha", "entra", "ingress", "none"] = "ha"
    access_mode: Literal["public", "local"] = "public"
    host: str = "0.0.0.0"  # noqa: S104 - container port, only reachable through Caddy
    admin_port: int = Field(default=8100, ge=1, le=65535)
    log_level: str = "INFO"
    # Address of the Supervisor's Ingress proxy, the only peer trusted with
    # auth_mode=ingress (Home Assistant's fixed address in the hassio network).
    ingress_proxy: str = "172.30.32.2"
    # Dagster UI under /admin/dagster/ (full edition); the lite edition has none.
    dagster_ui: bool = True

    base_url: AnyHttpUrl | None = None
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: SecretStr | None = None
    admin_role: str = "xomtra.Admin"
    # Signs the session cookie; >= 32 characters, stable across restarts.
    admin_session_key: SecretStr | None = None
    admin_session_hours: int = Field(default=8, ge=1, le=24)

    ha_url: str | None = None
    ha_token: SecretStr | None = None
    # Compose edition (ADR 077): without XOMTRA_HA_URL/TOKEN, they are entered on the
    # setup page and stored in the audit database, the token encrypted with this key.
    settings_key: SecretStr | None = None
    # Sign-in with a Home Assistant account (ADR 053), see xomtra_mcp.config.
    ha_auth_url: str | None = None
    ha_allowed_users: Annotated[list[str], NoDecode] = []
    ha_role_map: str = DEFAULT_ROLE_MAP
    # Shown on the page Access (the MCP server applies them, ADR 081).
    ha_require_mfa: bool = True
    session_days: int = Field(default=180, ge=7, le=365)
    # MCP endpoint for clients in the LAN (local access), shown with new tokens and put
    # into the Claude Desktop extension; default <base_url>/mcp.
    mcp_url: AnyHttpUrl | None = None
    timezone: str = "UTC"

    # Read access to the audit table (role with SELECT + INSERT, ADR 040/041).
    audit_pg_host: str | None = None
    audit_pg_port: int = 5432
    audit_pg_db: str = "audit"
    audit_admin_user: str = "xomtra_audit_admin"
    audit_admin_password: SecretStr | None = None

    # Free space of the lake's local disk on the overview (ADR 072); the worker pauses
    # writing below worker_stop_free_ratio. Unset with object storage.
    lake_data_path: str | None = None
    worker_min_free_ratio: float = Field(default=0.10, ge=0, lt=1)
    worker_stop_free_ratio: float = Field(default=0.03, ge=0, lt=1)

    # Shown on the retention page (the ingest applies them, ADR 034).
    states_retention_days: int = 365
    statistics_5min_retention_days: int = 365
    statistics_hour_retention_days: int = 1825
    catalog_window_days: int = 30

    @field_validator(
        "mcp_url", "ha_auth_url", "base_url", "ha_url", "ha_token", "settings_key", mode="before"
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # compose passes unset optional variables as empty strings
        return None if value == "" else value

    @field_validator("ha_allowed_users", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auth_mode in ("none", "ingress"):
            return self
        required: tuple[str, ...] = ("base_url", "admin_session_key")
        if self.auth_mode == "entra":
            required += ("entra_tenant_id", "entra_client_id", "entra_client_secret")
        else:
            if self.settings_key is None or self.audit_dsn is None:
                required += ("ha_url",)  # no setup page without the store
            RoleMap.parse(self.ha_role_map)
        missing = [n for n in required if getattr(self, n) is None]
        if missing:
            raise ValueError(
                "Admin GUI requires: " + ", ".join(f"XOMTRA_{m.upper()}" for m in missing)
            )
        key = self.admin_session_key
        if key is not None and len(key.get_secret_value()) < 32:
            raise ValueError("XOMTRA_ADMIN_SESSION_KEY must have at least 32 characters")
        return self

    def ha_resolver(self) -> Resolver:
        key = self.settings_key.get_secret_value() if self.settings_key else None
        return Resolver(
            self.ha_url,
            self.ha_token.get_secret_value() if self.ha_token else None,
            self.audit_dsn if key else None,
            key,
        )

    @property
    def endpoint(self) -> str:
        """MCP endpoint URL for clients (local access)."""
        if self.mcp_url is not None:
            return str(self.mcp_url).rstrip("/")
        return f"{str(self.base_url or 'https://localhost').rstrip('/')}/mcp"

    @property
    def client_id(self) -> str:
        """OAuth client id of the admin GUI towards Home Assistant (IndieAuth URL)."""
        return f"{str(self.base_url).rstrip('/')}/"

    @property
    def redirect_uri(self) -> str:
        return f"{str(self.base_url).rstrip('/')}{PREFIX}/auth/callback"

    @property
    def audit_dsn(self) -> str | None:
        if not (self.audit_pg_host and self.audit_admin_password):
            return None
        parts = {
            "host": self.audit_pg_host,
            "port": str(self.audit_pg_port),
            "dbname": self.audit_pg_db,
            "user": self.audit_admin_user,
            "password": self.audit_admin_password.get_secret_value(),
            "application_name": "xomtra-admin",
        }
        return " ".join(
            f"{k}='" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for k, v in parts.items()
        )
