"""Ingest tasks shared by the CLI and the Dagster jobs (synchronous entry points)."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from hacore import (
    anomalies,
    bronze,
    catalog,
    correlations,
    findings,
    ingest,
    lake,
    maintenance,
    weather,
)
from hacore.client import HAClient
from hammond_ingest import transform
from hammond_ingest.settings import IngestSettings

logger = logging.getLogger(__name__)


@contextmanager
def open_lake() -> Iterator[duckdb.DuckDBPyConnection]:
    con = lake.connect(lake.LakeSettings.from_env())
    try:
        bronze.ensure_schema(con)
        # Finding tables with the columns of this release, before any analysis runs:
        # the MCP server reads them right after an upgrade (ADR 062: p_value,
        # explained_by).
        correlations.ensure_schema(con)
        findings.ensure_schema(con)
        yield con
    finally:
        con.close()


def _with_ha[T](
    fn: Callable[[HAClient, duckdb.DuckDBPyConnection], Awaitable[T]],
    settings: IngestSettings | None = None,
) -> T:
    url, token = (settings or IngestSettings()).require_ha()

    async def run() -> T:
        with open_lake() as con:
            async with HAClient(url, token) as ha:
                return await fn(ha, con)

    return asyncio.run(run())


def sync_registries() -> dict[str, int]:
    return _with_ha(ingest.sync_registries).written


def sync_statistics(settings: IngestSettings | None = None) -> dict[str, int]:
    s = settings or IngestSettings()
    # First run: fetch what HA still has, but nothing the retention would delete again.
    lookback = {
        period: min(ingest.DEFAULT_LOOKBACK[period], s.retention.statistics[period])
        for period in ingest.DEFAULT_LOOKBACK
    }
    fetch = functools.partial(ingest.sync_statistics, lookback=lookback, labels=s.labels)
    return _with_ha(fetch, s).written


def backfill_history(
    start: datetime, end: datetime | None = None, settings: IngestSettings | None = None
) -> dict[str, int]:
    s = settings or IngestSettings()
    stop = end or datetime.now(UTC)
    return _with_ha(
        lambda ha, con: ingest.backfill_history(ha, con, start, stop, labels=s.labels), s
    ).written


def pending_gaps(after_us: int | None) -> list[tuple[datetime, datetime, int]]:
    """Caught-up ranges recorded by the stream after ``after_us`` (see the sensor)."""
    with open_lake() as con:
        return bronze.gaps_since(con, after_us)


def run_transform() -> transform.TransformResult:
    return transform.apply_and_run()


def restate(start: datetime, end: datetime) -> None:
    transform.restate(start, end)


def retention(settings: IngestSettings | None = None) -> dict[str, Any]:
    s = settings or IngestSettings()
    with open_lake() as con:
        result = maintenance.apply_retention(con, s.retention)
    return {
        "cutoffs": {k: v.isoformat() for k, v in result.cutoffs.items()},
        "deleted": result.deleted,
    }


def lake_maintenance(settings: IngestSettings | None = None) -> None:
    s = settings or IngestSettings()
    with open_lake() as con:
        maintenance.compact_and_expire(con, timedelta(days=s.snapshot_retention_days))


def profile_catalog(settings: IngestSettings | None = None) -> dict[str, Any]:
    s = settings or IngestSettings()
    with open_lake() as con:
        result = catalog.profile(con, window=timedelta(days=s.catalog_window_days))
    return {"entities": result.entities, "drift": result.drift}


def sync_outside_temperature(settings: IngestSettings | None = None) -> dict[str, Any]:
    """Open-Meteo archive of the outside temperature at the HA home (ADR 043).

    A failure is reported, not raised: the correlations run without the older hours.
    """
    s = settings or IngestSettings()
    if not s.open_meteo_enabled:
        return {"skipped": "HAMMOND_OPEN_METEO_ENABLED is off"}

    async def home(ha: HAClient, _con: duckdb.DuckDBPyConnection) -> tuple[float, float]:
        config = await ha.get_config()
        return float(config["latitude"]), float(config["longitude"])

    try:
        lat, lon = _with_ha(home, s)
        fetch = functools.partial(weather.fetch_archive, url=s.open_meteo_url)
        with open_lake() as con:
            days = max(s.correlation_windows_days) + 1
            result = weather.sync(con, lat, lon, days=days, fetch=fetch)
    except (OSError, ValueError, KeyError) as err:  # network, HTTP, JSON, config
        logger.warning("Open-Meteo archive not updated: %s", err)
        return {"error": str(err)}
    return result.as_dict()


def find_correlations(settings: IngestSettings | None = None) -> list[dict[str, Any]]:
    """Correlations for every configured window (ADR 042), findings in catalog.finding."""
    s = settings or IngestSettings()
    out = []
    with open_lake() as con:
        for days in s.correlation_windows_days:
            params = correlations.CorrelationParams(
                window_days=days,
                max_lag=s.correlation_max_lag_hours,
                min_coverage=s.correlation_min_coverage,
                min_r=s.correlation_min_r,
                max_q=s.correlation_max_q,
                top=s.correlation_top,
                outside_temp=s.correlation_outside_temp or None,
                timezone=s.timezone,
                labels=s.labels,
                method=s.correlation_method,
            )
            out.append(correlations.run(con, params).as_dict())
    return out


def find_anomalies(settings: IngestSettings | None = None) -> dict[str, Any]:
    """Anomalies and change points of single entities (ADR 046) into catalog.finding."""
    s = settings or IngestSettings()
    if not s.anomalies_enabled:
        return {"skipped": "HAMMOND_ANOMALIES_ENABLED is off"}
    params = anomalies.AnomalyParams(
        baseline_days=s.anomaly_baseline_days,
        recent_days=s.anomaly_recent_days,
        z_threshold=s.anomaly_z,
        changepoint_days=s.changepoint_window_days,
        changepoint_recent_days=min(s.changepoint_recent_days, s.changepoint_window_days),
        changepoint_min_shift=s.changepoint_min_shift,
        min_coverage=s.correlation_min_coverage,
        top=s.anomaly_top,
        outside_temp=s.correlation_outside_temp or None,
        timezone=s.timezone,
        labels=s.labels,
    )
    with open_lake() as con:
        return anomalies.run(con, params).as_dict()


def snapshot_findings(settings: IngestSettings | None = None) -> dict[str, Any]:
    """Weekly snapshot of the current findings (ADR 048)."""
    s = settings or IngestSettings()
    with open_lake() as con:
        return findings.snapshot_week(
            con, timezone=s.timezone, keep_weeks=s.findings_weekly_keep_weeks
        ).as_dict()


def parse_time(value: str) -> datetime:
    """ISO date/datetime (UTC if no offset) or a relative ``<n>d`` (days ago)."""
    if value.endswith("d") and value[:-1].isdigit():
        return datetime.now(UTC) - timedelta(days=int(value[:-1]))
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
