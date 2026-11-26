"""Ingest service settings, from environment variables (``XOMTRA_*``)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hacore.edition import PROFILES, Edition
from hacore.exclusions import NO_ANALYSIS, NO_INGEST, ExclusionLabels
from hacore.ha_connection import Resolver
from hacore.maintenance import Retention


class HANotConnectedError(ValueError):
    """Neither the environment nor the admin GUI has a Home Assistant connection yet."""


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XOMTRA_", extra="ignore")

    # Home Assistant: from the environment, else as entered in the admin GUI (ADR 077),
    # stored in the database ``analysis_settings_db`` and encrypted with settings_key.
    ha_url: str = ""
    ha_token: SecretStr = SecretStr("")
    settings_key: SecretStr = SecretStr("")

    # Resource profile (ADR 050); only changes the defaults of the values below.
    edition: Edition = Edition.FULL

    # Rows older than this are deleted from bronze and silver (daily retention job, ADR 034).
    # Defaults by edition: full 365/365/1825 days, lite 90/90/1825 days.
    states_retention_days: int = Field(default=365, ge=1)
    statistics_5min_retention_days: int = Field(default=365, ge=1)
    statistics_hour_retention_days: int = Field(default=5 * 365, ge=1)
    # DuckLake snapshots older than this are expired; their files are then deleted.
    snapshot_retention_days: int = Field(default=7, ge=1)

    # Home Assistant label ids that leave entities out (on the entity or its device,
    # ADR 044): not stored at all, or stored but left out of the analyses.
    label_no_ingest: str = Field(default=NO_INGEST, min_length=1)
    label_no_analysis: str = Field(default=NO_ANALYSIS, min_length=1)

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
    # v2 = calibrated significance and structural flags (ADR 062); v1 = ADR 042.
    correlation_method: Literal["v1", "v2"] = "v2"
    correlation_structure_display: Literal["hide", "lower", "show"] = "hide"
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
    # v2 = conformal / permutation p-values and BH over series (ADR 062); v1 = ADR 046
    # fixed thresholds (anomaly_z, changepoint_min_shift).
    anomaly_method: Literal["v1", "v2"] = "v2"
    anomaly_max_q: float = Field(default=0.05, gt=0, le=1)
    # v2: |z| counted at most this in the episode statistic (nearly constant sensors).
    anomaly_z_cap: float = Field(default=50.0, gt=3)
    # Database of the analysis settings changed in the admin GUI (ADR 060; schema
    # ``analysis`` next to the audit). They win over the variables above; empty = off.
    analysis_settings_db: str = "audit"
    # Weekly snapshots of the findings (ADR 048) kept this many weeks.
    findings_weekly_keep_weeks: int = Field(default=104, ge=1, le=520)

    stream_flush_seconds: float = Field(default=30.0, gt=0)
    stream_max_buffer: int = Field(default=5_000, gt=0)
    stream_max_catchup_days: int = Field(default=10, ge=0)
    heartbeat_file: Path | None = Path("/tmp/xomtra-stream.heartbeat")  # noqa: S108

    # Postgres databases of the ops role: SQLMesh state (models, intervals) and the
    # worker's runs; Dagster has its own.
    ops_pg_host: str = "postgres"
    ops_pg_port: int = 5432
    ops_pg_user: str = "xomtra_ops"
    ops_pg_password: SecretStr = SecretStr("")
    sqlmesh_pg_db: str = "sqlmesh"
    # Runs and cursors of the built-in worker (lite edition, ``xomtra-ingest worker``).
    worker_pg_db: str = "ops"
    worker_runs_keep_days: int = Field(default=90, ge=1)
    # The worker warns and brings the retention forward below this free share of the
    # lake's disk (local data path only).
    worker_min_free_ratio: float = Field(default=0.10, ge=0, lt=1)
    # Below this free share the stream and the writing jobs pause until there is space
    # again (only the retention runs); the stream then catches up from Home Assistant's
    # history (at most stream_max_catchup_days).
    worker_stop_free_ratio: float = Field(default=0.03, ge=0, lt=1)

    # Schedules of the Dagster jobs (cron, in ``timezone``).
    timezone: str = "UTC"

    @model_validator(mode="before")
    @classmethod
    def _edition_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        edition = Edition(str(data.get("edition") or Edition.FULL).strip().lower())
        defaults = PROFILES[edition]
        for name in (
            "states_retention_days",
            "statistics_5min_retention_days",
            "statistics_hour_retention_days",
        ):
            if data.get(name) in (None, ""):
                data[name] = getattr(defaults, name)
        return data

    @field_validator("ha_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("open_meteo_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("XOMTRA_OPEN_METEO_URL must use https")
        return v

    @field_validator("correlation_windows_days")
    @classmethod
    def _windows(cls, v: list[int]) -> list[int]:
        if not v or any(d < 7 or d > 5 * 365 for d in v):
            raise ValueError("correlation windows must be 7..1825 days")
        return sorted(set(v), reverse=True)

    @field_validator("label_no_ingest", "label_no_analysis")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("label id must not be empty")
        return v

    @property
    def labels(self) -> ExclusionLabels:
        return ExclusionLabels(no_ingest=self.label_no_ingest, no_analysis=self.label_no_analysis)

    @property
    def retention(self) -> Retention:
        return Retention(
            states=timedelta(days=self.states_retention_days),
            statistics={
                "5minute": timedelta(days=self.statistics_5min_retention_days),
                "hour": timedelta(days=self.statistics_hour_retention_days),
            },
        )

    def ha_resolver(self) -> Resolver:
        stored = bool(
            self.analysis_settings_db
            and self.ops_pg_password.get_secret_value()
            and self.settings_key.get_secret_value()
        )
        return Resolver(
            self.ha_url,
            self.ha_token.get_secret_value(),
            self.ops_pg_uri(self.analysis_settings_db) if stored else None,
            self.settings_key.get_secret_value() if stored else None,
        )

    def require_ha(self) -> tuple[str, str]:
        found = self.ha_resolver().get()
        if found is None:
            raise HANotConnectedError(
                "Home Assistant is not connected yet: enter its URL and token in the "
                "admin GUI (or set XOMTRA_HA_URL and XOMTRA_HA_TOKEN)"
            )
        return found.url, found.token

    def ops_pg_uri(self, database: str) -> str:
        user = quote(self.ops_pg_user, safe="")
        password = quote(self.ops_pg_password.get_secret_value(), safe="")
        return f"postgresql://{user}:{password}@{self.ops_pg_host}:{self.ops_pg_port}/{database}"
