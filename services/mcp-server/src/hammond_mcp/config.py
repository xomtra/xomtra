"""Server configuration from environment variables (prefix ``HAMMOND_``).

Nothing installation specific has defaults here: tenant/client IDs, URLs and secrets
always come from the environment (``deploy/compose/.env``).
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    model_config = SettingsConfigDict(env_prefix="HAMMOND_", extra="ignore")

    auth_mode: Literal["entra", "none"] = "entra"
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

    # Optional Home Assistant connection (read-only use in the PoC).
    ha_url: str | None = None
    ha_token: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _csv_lists(cls, data: object) -> object:
        if isinstance(data, dict):
            for key in ("entra_required_scopes", "allowed_client_redirect_uris"):
                if key in data:
                    data[key] = _split_csv(data[key])
        return data

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.auth_mode == "none":
            if not _is_loopback(self.host):
                raise ValueError(
                    "HAMMOND_AUTH_MODE=none is only allowed on a loopback address "
                    "(local development)."
                )
            return self
        missing = [
            name
            for name in (
                "base_url",
                "entra_tenant_id",
                "entra_client_id",
                "entra_client_secret",
                "jwt_signing_key",
            )
            if getattr(self, name) is None
        ]
        if missing:
            env = ", ".join(f"HAMMOND_{m.upper()}" for m in missing)
            raise ValueError(f"Entra auth requires: {env}")
        key = self.jwt_signing_key
        if key is not None and len(key.get_secret_value()) < 32:
            raise ValueError("HAMMOND_JWT_SIGNING_KEY must have at least 32 characters")
        if self.entra_tenant_id in {"common", "organizations", "consumers"}:
            raise ValueError("Use a specific tenant ID, multi-tenant sign-in is not allowed")
        return self

    @property
    def ha_configured(self) -> bool:
        return bool(self.ha_url and self.ha_token)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
