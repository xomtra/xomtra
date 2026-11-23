"""Admin GUI settings (environment, prefix ``HAMMOND_``)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PREFIX = "/admin"


class AdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAMMOND_", extra="ignore")

    auth_mode: Literal["entra", "none"] = "entra"
    host: str = "0.0.0.0"  # noqa: S104 - container port, only reachable through Caddy
    admin_port: int = Field(default=8100, ge=1, le=65535)
    log_level: str = "INFO"

    base_url: AnyHttpUrl | None = None
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: SecretStr | None = None
    admin_role: str = "HAmmond.Admin"
    # Signs the session cookie; >= 32 characters, stable across restarts.
    admin_session_key: SecretStr | None = None
    admin_session_hours: int = Field(default=8, ge=1, le=24)

    ha_url: str | None = None
    ha_token: SecretStr | None = None
    timezone: str = "UTC"

    # Read access to the audit table (role with SELECT + INSERT, ADR 040/041).
    audit_pg_host: str | None = None
    audit_pg_port: int = 5432
    audit_pg_db: str = "audit"
    audit_admin_user: str = "hammond_audit_admin"
    audit_admin_password: SecretStr | None = None

    # Shown on the retention page (the ingest applies them, ADR 034).
    states_retention_days: int = 365
    statistics_5min_retention_days: int = 365
    statistics_hour_retention_days: int = 1825
    catalog_window_days: int = 30

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auth_mode == "none":
            return self
        missing = [
            n
            for n in (
                "base_url",
                "entra_tenant_id",
                "entra_client_id",
                "entra_client_secret",
                "admin_session_key",
            )
            if getattr(self, n) is None
        ]
        if missing:
            raise ValueError(
                "Admin GUI requires: " + ", ".join(f"HAMMOND_{m.upper()}" for m in missing)
            )
        key = self.admin_session_key
        if key is not None and len(key.get_secret_value()) < 32:
            raise ValueError("HAMMOND_ADMIN_SESSION_KEY must have at least 32 characters")
        return self

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
            "application_name": "hammond-admin",
        }
        return " ".join(
            f"{k}='" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for k, v in parts.items()
        )
