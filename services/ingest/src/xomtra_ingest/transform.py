"""SQLMesh transformations bronze -> silver on DuckLake.

The SQLMesh project lives in ``sqlmesh_project/`` (shipped inside the package). Its
gateway is built here from the same environment variables as the rest of Xomtra:

* data: DuckDB with the DuckLake catalog attached as ``lake`` (default catalog), so
  SQLMesh physical tables (``sqlmesh__silver``) and views (``silver``) live in the lake;
* state: a Postgres database (``XOMTRA_SQLMESH_PG_DB``), not the lake, so that SQLMesh
  bookkeeping never mixes with data and survives a rebuild of the lake.

SQLMesh writes values into ``ATTACH``/``CREATE SECRET`` statements without escaping, so
the catalog DSN is a percent-encoded URI and secret values are pre-escaped here.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlmesh import Context
from sqlmesh.core.config import (
    Config,
    DuckDBConnectionConfig,
    GatewayConfig,
    ModelDefaultsConfig,
    PostgresConnectionConfig,
)
from sqlmesh.core.config.connection import DuckDBAttachOptions
from sqlmesh.utils import CompletionStatus

from hacore.lake import LAKE_ALIAS, LakeSettings
from xomtra_ingest.settings import IngestSettings

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent / "sqlmesh_project"
GATEWAY = "xomtra"
INCREMENTAL_MODELS = ("silver.states", "silver.statistics")


def _escape(value: str) -> str:
    return value.replace("'", "''")


def catalog_uri(settings: LakeSettings) -> str:
    user = quote(settings.pg_user, safe="")
    password = quote(settings.pg_password, safe="")
    db = quote(settings.pg_db, safe="")
    return f"postgres:postgresql://{user}:{password}@{settings.pg_host}:{settings.pg_port}/{db}"


def _connector_config(
    extension_dir: str | None, settings: LakeSettings | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {"TimeZone": "UTC"}
    if settings is not None:
        config.update(settings.duckdb_config())
    config["autoinstall_known_extensions"] = False
    if extension_dir:
        config["extension_directory"] = extension_dir
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in config.items()}


def lake_connection(settings: LakeSettings) -> DuckDBConnectionConfig:
    secrets: dict[str, dict[str, str]] = {}
    if not settings.is_local:
        secrets["xomtra_s3"] = _s3_secret(settings)
    return DuckDBConnectionConfig(
        catalogs={
            LAKE_ALIAS: DuckDBAttachOptions(
                type="ducklake",
                path=catalog_uri(settings),
                data_path=_escape(settings.data_path),
                data_inlining_row_limit=0,
            )
        },
        connector_config=_connector_config(settings.extension_dir, settings),
        secrets=secrets,
    )


def _s3_secret(settings: LakeSettings) -> dict[str, str]:
    return {
        "type": "s3",
        "key_id": _escape(settings.s3_access_key),
        "secret": _escape(settings.s3_secret_key),
        "endpoint": _escape(settings.s3_endpoint),
        "region": _escape(settings.s3_region),
        "url_style": "path",
        "use_ssl": "true" if settings.s3_use_ssl else "false",
        "scope": _escape(f"s3://{settings.s3_bucket}"),
    }


def build_config(
    connection: DuckDBConnectionConfig, state: PostgresConnectionConfig | DuckDBConnectionConfig
) -> Config:
    return Config(
        gateways={GATEWAY: GatewayConfig(connection=connection, state_connection=state)},
        default_gateway=GATEWAY,
        model_defaults=ModelDefaultsConfig(dialect="duckdb"),
        # No telemetry from a private installation (SQLMesh sends anonymous usage
        # events to its vendor by default).
        disable_anonymized_analytics=True,
        # The project ships inside the (read-only) package; the cache goes elsewhere.
        cache_dir=str(Path(tempfile.gettempdir()) / "xomtra-sqlmesh-cache"),
    )


def config_from_env() -> Config:
    lake = LakeSettings.from_env()
    ingest = IngestSettings()
    state = PostgresConnectionConfig(
        host=ingest.ops_pg_host,
        port=ingest.ops_pg_port,
        user=ingest.ops_pg_user,
        password=ingest.ops_pg_password.get_secret_value(),
        database=ingest.sqlmesh_pg_db,
    )
    return build_config(lake_connection(lake), state)


def local_config(
    catalog_path: str, data_path: str, state_path: str, extension_dir: str | None = None
) -> Config:
    """Local DuckLake catalog and DuckDB state file (tests, development)."""
    connection = DuckDBConnectionConfig(
        catalogs={
            LAKE_ALIAS: DuckDBAttachOptions(
                type="ducklake", path=catalog_path, data_path=data_path, data_inlining_row_limit=0
            )
        },
        connector_config=_connector_config(extension_dir),
    )
    state = DuckDBConnectionConfig(
        database=state_path, connector_config=_connector_config(extension_dir)
    )
    return build_config(connection, state)


class TransformError(RuntimeError):
    """A model or audit failed."""


@dataclass(frozen=True, slots=True)
class TransformResult:
    applied: bool
    status: str


def context(config: Config | None = None) -> Context:
    return Context(paths=[str(PROJECT_DIR)], config=config or config_from_env())


class _FailedModels(logging.Handler):
    """Collects the model errors SQLMesh logs (it returns only a status from ``run``)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[1] if record.exc_info else None
        if error is None:
            return
        cause = error.__cause__ or error
        self.messages.append(f"{error}: {type(cause).__name__}: {cause}")


def _run(ctx: Context, execution_time: datetime) -> CompletionStatus:
    log = logging.getLogger("sqlmesh.core.scheduler")
    handler = _FailedModels()
    level = log.level
    log.addHandler(handler)
    if not log.isEnabledFor(logging.INFO):
        log.setLevel(logging.INFO)
    try:
        completion = ctx.run(execution_time=execution_time)
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    if completion.is_failure:
        detail = "; ".join(handler.messages) or "see log"
        raise TransformError(f"SQLMesh run failed: {detail}")
    return completion


def apply_and_run(config: Config | None = None) -> TransformResult:
    """Apply model changes to prod (no-op if unchanged), then process due intervals."""
    # One execution time for plan and run: otherwise an hour boundary between the two
    # makes the run evaluate a new interval right after the plan's backfill.
    execution_time = datetime.now(UTC).replace(second=0, microsecond=0)
    ctx = context(config)
    try:
        plan = ctx.plan(auto_apply=True, no_prompts=True, execution_time=execution_time)
        applied = plan.has_changes or bool(plan.missing_intervals)
        completion = _run(ctx, execution_time)
        return TransformResult(applied=applied, status=completion.value)
    finally:
        ctx.close()


def restate(start: datetime, end: datetime, config: Config | None = None) -> None:
    """Recompute the incremental silver models for a range (after a backfill)."""
    # SQLMesh refuses plan ends after its execution time (now, floored to the minute).
    end = min(end, datetime.now(UTC).replace(second=0, microsecond=0))
    if end <= start:
        return
    ctx = context(config)
    try:
        # Make sure prod exists and is current before restating part of it.
        ctx.plan(auto_apply=True, no_prompts=True)
        ctx.plan(
            restate_models=list(INCREMENTAL_MODELS),
            start=start,
            end=end,
            auto_apply=True,
            no_prompts=True,
        )
    finally:
        ctx.close()
