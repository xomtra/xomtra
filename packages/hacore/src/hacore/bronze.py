"""Bronze layer: raw Home Assistant data in DuckLake, append-only and idempotent.

Tables (schema ``bronze``):

``ha_states``
    Every recorded state change. Key ``(entity_id, last_updated)``. Partitioned by
    year and month of ``last_updated`` so that retention drops whole files.
``ha_statistics``
    5-minute and hourly statistics. Key ``(statistic_id, period, period_start)``.
``ha_registry``
    Change log of the registries (floors, areas, labels, devices, entities), of the
    statistics metadata and of descriptive entity attributes (unit, device class): a
    row is written only when an item appears, changes or disappears (``removed``), so
    the table stays small and keeps the full history.

All writers skip rows whose key already exists, so re-running a backfill, a catch-up
after a reconnect never duplicates data.
Rows are staged as newline-delimited JSON in a temporary file and inserted with one
statement per batch; the key lookup is limited to the time range of the batch, which
DuckLake prunes by file statistics.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

import duckdb

from hacore.history import StateRow, StatisticRow
from hacore.lake import LAKE_ALIAS, sql_literal

logger = logging.getLogger(__name__)

type JSON = dict[str, Any]

SCHEMA = "bronze"
STATES = "ha_states"
STATISTICS = "ha_statistics"
REGISTRY = "ha_registry"
# Ranges the stream caught up after an outage; silver is restated for them (ADR 031).
GAPS = "ingest_gaps"

# Primary key field of each registry item. Entities are keyed by their registry id, not
# by entity_id, so that a rename shows up as a change of the same item.
REGISTRY_KEYS: dict[str, str] = {
    "floors": "floor_id",
    "areas": "area_id",
    "labels": "label_id",
    "devices": "id",
    "entities": "id",
    "statistics_meta": "statistic_id",
    "entity_attributes": "entity_id",
}

STATES_STAGE_COLUMNS = {
    "entity_id": "VARCHAR",
    "state": "VARCHAR",
    "attributes": "JSON",
    "last_changed_us": "BIGINT",
    "last_updated_us": "BIGINT",
    "context_id": "VARCHAR",
}
STATISTICS_STAGE_COLUMNS = {
    "statistic_id": "VARCHAR",
    "period": "VARCHAR",
    "start_us": "BIGINT",
    "mean": "DOUBLE",
    "min": "DOUBLE",
    "max": "DOUBLE",
    "state": "DOUBLE",
    "sum": "DOUBLE",
    "last_reset_us": "BIGINT",
}
REGISTRY_STAGE_COLUMNS = {"item_id": "VARCHAR", "payload": "JSON", "content_hash": "VARCHAR"}


def table(name: str, alias: str = LAKE_ALIAS) -> str:
    return f"{alias}.{SCHEMA}.{name}"


def _ddl(alias: str) -> dict[str, tuple[str, str]]:
    """Table name -> (CREATE TABLE, partitioning)."""
    return {
        STATES: (
            f"CREATE TABLE {table(STATES, alias)} ("
            "entity_id VARCHAR NOT NULL, "
            "state VARCHAR, "
            "attributes JSON, "
            "last_changed TIMESTAMPTZ, "
            "last_updated TIMESTAMPTZ NOT NULL, "
            "context_id VARCHAR, "
            "source VARCHAR NOT NULL, "
            "ingested_at TIMESTAMPTZ NOT NULL)",
            "year(last_updated), month(last_updated)",
        ),
        STATISTICS: (
            f"CREATE TABLE {table(STATISTICS, alias)} ("
            "statistic_id VARCHAR NOT NULL, "
            "period VARCHAR NOT NULL, "
            "period_start TIMESTAMPTZ NOT NULL, "
            "mean DOUBLE, min DOUBLE, max DOUBLE, state DOUBLE, sum DOUBLE, "
            "last_reset TIMESTAMPTZ, "
            "source VARCHAR NOT NULL, "
            "ingested_at TIMESTAMPTZ NOT NULL)",
            "period, year(period_start)",
        ),
        REGISTRY: (
            f"CREATE TABLE {table(REGISTRY, alias)} ("
            "registry VARCHAR NOT NULL, "
            "item_id VARCHAR NOT NULL, "
            "payload JSON, "
            "content_hash VARCHAR, "
            "removed BOOLEAN NOT NULL, "
            "source VARCHAR NOT NULL, "
            "fetched_at TIMESTAMPTZ NOT NULL)",
            "registry",
        ),
        GAPS: (
            f"CREATE TABLE {table(GAPS, alias)} ("
            "range_start TIMESTAMPTZ NOT NULL, "
            "range_end TIMESTAMPTZ NOT NULL, "
            "states_written BIGINT NOT NULL, "
            "recorded_at TIMESTAMPTZ NOT NULL)",
            "year(recorded_at)",
        ),
    }


def ensure_schema(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> None:
    """Create the bronze schema and tables if missing (idempotent)."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {alias}.{SCHEMA}")
    existing = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = ? AND schema_name = ?",
            [alias, SCHEMA],
        ).fetchall()
    }
    for name, (create, partitioning) in _ddl(alias).items():
        if name in existing:
            continue
        with_conflict_retry(functools.partial(_create, con, create, name, partitioning, alias))
        logger.info("Created %s", table(name, alias))


def _create(
    con: duckdb.DuckDBPyConnection, create: str, name: str, partitioning: str, alias: str
) -> None:
    con.execute("BEGIN")
    try:
        con.execute(create.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
        con.execute(f"ALTER TABLE {table(name, alias)} SET PARTITIONED BY ({partitioning})")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


# -- helpers -----------------------------------------------------------------------


def with_conflict_retry[T](fn: Callable[[], T], attempts: int = 5) -> T:
    """Run ``fn``, retrying DuckLake commit conflicts of concurrent writers."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except duckdb.TransactionException as err:
            if attempt == attempts or "onflict" not in str(err):
                raise
            delay = 0.2 * 2**attempt
            logger.warning("DuckLake commit conflict, retry %d in %.1fs", attempt, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _finite(value: Any) -> Any:
    """Replace NaN/inf (not valid JSON) with None, recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def _dumps(record: Mapping[str, Any]) -> str:
    try:
        return json.dumps(record, allow_nan=False, default=str)
    except ValueError:
        return json.dumps(_finite(record), allow_nan=False, default=str)


def _stage(
    con: duckdb.DuckDBPyConnection,
    name: str,
    columns: Mapping[str, str],
    records: Iterable[Mapping[str, Any]],
) -> int:
    """Load records into a temporary table via an NDJSON file; returns the row count."""
    fd, path = tempfile.mkstemp(prefix=f"xomtra-{name}-", suffix=".ndjson")
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(_dumps(record))
                fh.write("\n")
                count += 1
        cols = ", ".join(f"{sql_literal(k)}: {sql_literal(v)}" for k, v in columns.items())
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT * FROM read_json("
            f"{sql_literal(path)}, format = 'newline_delimited', columns = {{{cols}}})"
        )
    finally:
        os.unlink(path)
    return count


def _bounds(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[str, str] | None:
    row = con.execute(sql).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0]), str(row[1])


def _inserted(result: duckdb.DuckDBPyConnection) -> int:
    row = result.fetchone()
    return int(row[0]) if row else 0


# -- states ------------------------------------------------------------------------


def write_states(
    con: duckdb.DuckDBPyConnection,
    rows: Iterable[StateRow],
    *,
    source: str,
    alias: str = LAKE_ALIAS,
) -> int:
    """Insert states whose key is not stored yet; returns the number of new rows."""
    if not _stage(con, "_stage_states_raw", STATES_STAGE_COLUMNS, (r.as_record() for r in rows)):
        return 0
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _stage_states AS SELECT entity_id, state, attributes, "
        "make_timestamp(last_changed_us)::TIMESTAMPTZ AS last_changed, "
        "make_timestamp(last_updated_us)::TIMESTAMPTZ AS last_updated, "
        "context_id FROM _stage_states_raw"
    )
    return insert_states_from_stage(con, "_stage_states", source=source, alias=alias)


def insert_states_from_stage(
    con: duckdb.DuckDBPyConnection,
    stage: str,
    *,
    source: str,
    alias: str = LAKE_ALIAS,
) -> int:
    """Insert from a staged relation with the columns entity_id, state, attributes,
    last_changed, last_updated, context_id."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _dedup_states AS SELECT * FROM "
        f"{stage} WHERE entity_id IS NOT NULL AND last_updated IS NOT NULL "
        "QUALIFY row_number() OVER (PARTITION BY entity_id, last_updated "
        "ORDER BY context_id NULLS LAST) = 1"
    )
    bounds = _bounds(
        con,
        "SELECT min(last_updated)::VARCHAR, max(last_updated)::VARCHAR FROM _dedup_states",
    )
    if bounds is None:
        return 0
    lower, upper = (sql_literal(b) for b in bounds)
    target = table(STATES, alias)
    sql = (
        f"INSERT INTO {target} "
        "SELECT s.entity_id, s.state, s.attributes, s.last_changed, s.last_updated, "
        f"s.context_id, {sql_literal(source)}, now() FROM _dedup_states s "
        f"ANTI JOIN (SELECT entity_id, last_updated FROM {target} "
        f"WHERE last_updated BETWEEN {lower}::TIMESTAMPTZ AND {upper}::TIMESTAMPTZ) t "
        "ON s.entity_id = t.entity_id AND s.last_updated = t.last_updated "
        "ORDER BY s.last_updated, s.entity_id"
    )
    return with_conflict_retry(lambda: _inserted(con.execute(sql)))


def state_watermark_us(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> int | None:
    """Latest stored ``last_updated`` (epoch microseconds), or None if empty."""
    row = con.execute(f"SELECT epoch_us(max(last_updated)) FROM {table(STATES, alias)}").fetchone()
    return int(row[0]) if row and row[0] is not None else None


# -- statistics --------------------------------------------------------------------


def write_statistics(
    con: duckdb.DuckDBPyConnection,
    rows: Iterable[StatisticRow],
    *,
    source: str,
    alias: str = LAKE_ALIAS,
) -> int:
    if not stage_statistics(con, (r.as_record() for r in rows)):
        return 0
    return insert_statistics_from_stage(con, "_stage_stats", source=source, alias=alias)


def stage_statistics(con: duckdb.DuckDBPyConnection, records: Iterable[Mapping[str, Any]]) -> int:
    """Stage statistics records (``StatisticRow.as_record`` shape) as ``_stage_stats``."""
    count = _stage(con, "_stage_stats_raw", STATISTICS_STAGE_COLUMNS, records)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _stage_stats AS SELECT statistic_id, period, "
        "make_timestamp(start_us)::TIMESTAMPTZ AS period_start, mean, min, max, state, sum, "
        "make_timestamp(last_reset_us)::TIMESTAMPTZ AS last_reset FROM _stage_stats_raw"
    )
    return count


def insert_statistics_from_stage(
    con: duckdb.DuckDBPyConnection,
    stage: str,
    *,
    source: str,
    alias: str = LAKE_ALIAS,
) -> int:
    """Insert from a staged relation with the columns statistic_id, period, period_start,
    mean, min, max, state, sum, last_reset."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _dedup_stats AS SELECT * FROM "
        f"{stage} WHERE statistic_id IS NOT NULL AND period_start IS NOT NULL "
        "QUALIFY row_number() OVER (PARTITION BY statistic_id, period, period_start) = 1"
    )
    bounds = _bounds(
        con, "SELECT min(period_start)::VARCHAR, max(period_start)::VARCHAR FROM _dedup_stats"
    )
    if bounds is None:
        return 0
    lower, upper = (sql_literal(b) for b in bounds)
    target = table(STATISTICS, alias)
    sql = (
        f"INSERT INTO {target} "
        "SELECT s.statistic_id, s.period, s.period_start, s.mean, s.min, s.max, s.state, "
        f"s.sum, s.last_reset, {sql_literal(source)}, now() FROM _dedup_stats s "
        f"ANTI JOIN (SELECT statistic_id, period, period_start FROM {target} "
        f"WHERE period_start BETWEEN {lower}::TIMESTAMPTZ AND {upper}::TIMESTAMPTZ) t "
        "ON s.statistic_id = t.statistic_id AND s.period = t.period "
        "AND s.period_start = t.period_start "
        "ORDER BY s.period, s.period_start, s.statistic_id"
    )
    return with_conflict_retry(lambda: _inserted(con.execute(sql)))


def statistics_watermark_us(
    con: duckdb.DuckDBPyConnection, period: str, alias: str = LAKE_ALIAS
) -> int | None:
    row = con.execute(
        f"SELECT epoch_us(max(period_start)) FROM {table(STATISTICS, alias)} WHERE period = ?",
        [period],
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


# -- registries --------------------------------------------------------------------


def content_hash(item: Mapping[str, Any]) -> str:
    canonical = json.dumps(_finite(item), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def write_registry(
    con: duckdb.DuckDBPyConnection,
    registry: str,
    items: Iterable[JSON],
    *,
    source: str,
    complete: bool = True,
    alias: str = LAKE_ALIAS,
) -> int:
    """Record new and changed items of one registry; returns the number of rows written.

    ``complete``: ``items`` is the whole registry, so stored items missing from it are
    recorded as removed.
    """
    key = REGISTRY_KEYS[registry]
    records = (
        {"item_id": str(item[key]), "payload": item, "content_hash": content_hash(item)}
        for item in items
        if item.get(key) is not None
    )
    _stage(con, "_stage_registry", REGISTRY_STAGE_COLUMNS, records)
    target = table(REGISTRY, alias)
    reg = sql_literal(registry)
    src = sql_literal(source)
    latest = (
        f"SELECT item_id, content_hash, removed FROM {target} WHERE registry = {reg} "
        "QUALIFY row_number() OVER (PARTITION BY item_id ORDER BY fetched_at DESC) = 1"
    )
    condition = "l.item_id IS NULL OR l.removed OR l.content_hash <> s.content_hash"
    changed = (
        f"SELECT {reg}, s.item_id, s.payload, s.content_hash, false, {src}, now() "
        f"FROM (SELECT DISTINCT ON (item_id) * FROM _stage_registry) s "
        f"LEFT JOIN ({latest}) l ON s.item_id = l.item_id WHERE {condition}"
    )
    parts = [changed]
    if complete:
        parts.append(
            f"SELECT {reg}, l.item_id, NULL, NULL, true, {src}, now() FROM ({latest}) l "
            "WHERE NOT l.removed AND l.item_id NOT IN (SELECT item_id FROM _stage_registry)"
        )
    sql = f"INSERT INTO {target} " + " UNION ALL ".join(parts)
    return with_conflict_retry(lambda: _inserted(con.execute(sql)))


def record_gap(
    con: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    states_written: int,
    *,
    alias: str = LAKE_ALIAS,
) -> None:
    """Note a caught-up range so that the silver layer gets restated for it."""
    sql = f"INSERT INTO {table(GAPS, alias)} VALUES (?::TIMESTAMPTZ, ?::TIMESTAMPTZ, ?, now())"
    params = [start.isoformat(), end.isoformat(), states_written]
    with_conflict_retry(functools.partial(con.execute, sql, params))


def gaps_since(
    con: duckdb.DuckDBPyConnection, after_us: int | None, *, alias: str = LAKE_ALIAS
) -> list[tuple[datetime, datetime, int]]:
    """Caught-up ranges recorded after ``after_us``: (start, end, recorded_at in µs)."""
    rows = con.execute(
        f"SELECT range_start, range_end, epoch_us(recorded_at) FROM {table(GAPS, alias)} "
        "WHERE epoch_us(recorded_at) > ? ORDER BY recorded_at",
        [after_us if after_us is not None else -1],
    ).fetchall()
    return [(r[0], r[1], int(r[2])) for r in rows]
