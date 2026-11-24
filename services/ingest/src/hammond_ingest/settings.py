"""Ingest service settings, from environment variables (``HAMMOND_*``)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hacore.maintenance import Retention


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAMMOND_", extra="ignore")

    ha_url: str = ""
    ha_token: SecretStr = SecretStr("")

    # Rows older than this are deleted from bronze and silver (daily retention job, ADR 034).
    states_retention_days: int = Field(default=365, ge=1)
    statistics_5min_retention_days: int = Field(default=365, ge=1)
    statistics_hour_retention_days: int = Field(default=5 * 365, ge=1)
    # DuckLake snapshots older than this are expired; their files are then deleted.
    snapshot_retention_days: int = Field(default=7, ge=1)

    # Days of state history the daily catalog profile looks at (ADR 037).
    catalog_window_days: int = Field(default=30, ge=1, le=365)

    # Correlations between entities (phase 1b.1, ADR 042): one run per window, findings
    # in ``catalog.finding``. The outside temperature (an entity_id with hourly
    # statistics, or a weather.* entity and its temperature attribute) is regressed out
    # as a common driver when set.
    correlation_windows_days: list[int] = Field(default_factory=lambda: [365, 30])
    correlation_outside_temp: str = ""
    correlation_max_lag_hours: int = Field(default=24, ge=0, le=168)
    correlation_min_coverage: float = Field(default=0.7, gt=0, le=1)
    correlation_min_r: float = Field(default=0.3, gt=0, lt=1)
    correlation_max_q: float = Field(default=0.01, gt=0, le=1)
    correlation_top: int = Field(default=100, ge=1, le=1000)
    # Hourly outside temperature history from the Open-Meteo archive (ADR 043) for the
    # hours the outside temperature entity has no data. Sends the HA home coordinates,
    # rounded to ~1 km, to Open-Meteo; off by default.
    open_meteo_enabled: bool = False
    open_meteo_url: str = "https://archive-api.open-meteo.com/v1/archive"

    # Anomalies and change points of single entities (phase 1b.2, ADR 046), run daily
    # after the correlations; they use the same outside temperature.
    anomalies_enabled: bool = True
    anomaly_baseline_days: int = Field(default=56, ge=14, le=365)
    anomaly_recent_days: int = Field(default=7, ge=1, le=30)
    anomaly_z: float = Field(default=4.0, ge=2.0, le=20.0)
    anomaly_top: int = Field(default=100, ge=1, le=1000)
    changepoint_window_days: int = Field(default=90, ge=28, le=730)
    changepoint_recent_days: int = Field(default=30, ge=7, le=365)
    changepoint_min_shift: float = Field(default=3.0, ge=1.0, le=20.0)
    # Weekly snapshots of the findings (ADR 048) kept this many weeks.
    findings_weekly_keep_weeks: int = Field(default=104, ge=1, le=520)

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

    @field_validator("open_meteo_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("HAMMOND_OPEN_METEO_URL must use https")
        return v

    @field_validator("correlation_windows_days")
    @classmethod
    def _windows(cls, v: list[int]) -> list[int]:
        if not v or any(d < 7 or d > 5 * 365 for d in v):
            raise ValueError("correlation windows must be 7..1825 days")
        return sorted(set(v), reverse=True)

    @property
    def retention(self) -> Retention:
        return Retention(
            states=timedelta(days=self.states_retention_days),
            statistics={
                "5minute": timedelta(days=self.statistics_5min_retention_days),
                "hour": timedelta(days=self.statistics_hour_retention_days),
            },
        )

    def require_ha(self) -> tuple[str, str]:
        token = self.ha_token.get_secret_value()
        if not self.ha_url or not token:
            raise ValueError("HAMMOND_HA_URL and HAMMOND_HA_TOKEN must be set")
        return self.ha_url, token

    def ops_pg_uri(self, database: str) -> str:
        user = quote(self.ops_pg_user, safe="")
        password = quote(self.ops_pg_password.get_secret_value(), safe="")
        return f"postgresql://{user}:{password}@{self.ops_pg_host}:{self.ops_pg_port}/{database}"
