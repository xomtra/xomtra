"""Ingest tasks shared by the CLI and the Dagster jobs (synchronous entry points)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from hacore import bronze, ingest, lake, maintenance, recorder
from hacore.client import HAClient
from hammond_ingest import transform
from hammond_ingest.settings import IngestSettings

logger = logging.getLogger(__name__)


@contextmanager
def open_lake() -> Iterator[duckdb.DuckDBPyConnection]:
    con = lake.connect(lake.LakeSettings.from_env())
    try:
        bronze.ensure_schema(con)
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


def sync_statistics() -> dict[str, int]:
    return _with_ha(ingest.sync_statistics).written


def backfill_history(start: datetime, end: datetime | None = None) -> dict[str, int]:
    stop = end or datetime.now(UTC)
    return _with_ha(lambda ha, con: ingest.backfill_history(ha, con, start, stop)).written


def import_recorder(
    path: Path, start: datetime | None = None, end: datetime | None = None
) -> recorder.ImportResult:
    with open_lake() as con:
        result = recorder.import_recorder_db(path, con, start=start, end=end)
    logger.info("Recorder import of %s: %s", path, result)
    return result


def run_transform() -> transform.TransformResult:
    return transform.apply_and_run()


def restate(start: datetime, end: datetime) -> None:
    transform.restate(start, end)


def retention(settings: IngestSettings | None = None) -> dict[str, Any]:
    s = settings or IngestSettings()
    with open_lake() as con:
        result = maintenance.apply_retention(con, s.retention)
    return {"cutoff": result.cutoff.isoformat(), "deleted": result.deleted}


def lake_maintenance(settings: IngestSettings | None = None) -> None:
    s = settings or IngestSettings()
    with open_lake() as con:
        maintenance.compact_and_expire(con, timedelta(days=s.snapshot_retention_days))


def parse_time(value: str) -> datetime:
    """ISO date/datetime (UTC if no offset) or a relative ``<n>d`` (days ago)."""
    if value.endswith("d") and value[:-1].isdigit():
        return datetime.now(UTC) - timedelta(days=int(value[:-1]))
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
