"""Ingest service settings, from environment variables (``HAMMOND_*``)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAMMOND_", extra="ignore")

    ha_url: str = ""
    ha_token: SecretStr = SecretStr("")

    # Rows older than this are deleted from bronze and silver (daily retention job).
    retention_days: int = Field(default=365, ge=1)
    # DuckLake snapshots older than this are expired; their files are then deleted.
    snapshot_retention_days: int = Field(default=7, ge=1)

    stream_flush_seconds: float = Field(default=30.0, gt=0)
    stream_max_buffer: int = Field(default=5_000, gt=0)
    stream_max_catchup_days: int = Field(default=10, ge=0)
    heartbeat_file: Path | None = Path("/tmp/hammond-stream.heartbeat")  # noqa: S108

    # Postgres database of SQLMesh state (models, intervals); Dagster has its own.
    ops_pg_host: str = "postgres"
    ops_pg_port: int = 5432
    ops_pg_user: str = "hammond_ops"
    ops_pg_password: SecretStr = SecretStr("")
    sqlmesh_pg_db: str = "sqlmesh"

    # Schedules of the Dagster jobs (cron, in ``timezone``).
    timezone: str = "UTC"

    @field_validator("ha_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def retention(self) -> timedelta:
        return timedelta(days=self.retention_days)

    def require_ha(self) -> tuple[str, str]:
        token = self.ha_token.get_secret_value()
        if not self.ha_url or not token:
            raise ValueError("HAMMOND_HA_URL and HAMMOND_HA_TOKEN must be set")
        return self.ha_url, token

    def ops_pg_uri(self, database: str) -> str:
        user = quote(self.ops_pg_user, safe="")
        password = quote(self.ops_pg_password.get_secret_value(), safe="")
        return f"postgresql://{user}:{password}@{self.ops_pg_host}:{self.ops_pg_port}/{database}"
