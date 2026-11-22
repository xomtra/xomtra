"""Import of a Home Assistant recorder database (SQLite) from a backup.

Home Assistant keeps the full state history only for ``purge_keep_days`` (10 days by
default) and 5-minute statistics for 10 days, so older history exists only in backups,
for example the HA VM backups in Proxmox Backup Server. This module reads a copy of
``home-assistant_v2.db`` and writes its states, statistics and statistics metadata into
the bronze layer. Overlaps with data that is already stored (other backups, the history
API, the live stream) are skipped by key, so backups can be imported in any order.

Supported: recorder schema 43 and newer (Home Assistant 2023.4+, ``states_meta`` and
float timestamps). The file is opened read-only; the directory must be writable if the
database has a ``-wal`` file next to it (SQLite needs to create the ``-shm`` index).

The import uses Python's sqlite3 module with indexed range queries, so memory use is
bounded by ``batch_rows`` and no DuckDB extension is needed.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from hacore import bronze
from hacore.history import to_us, us_to_datetime
from hacore.lake import LAKE_ALIAS, sql_literal

logger = logging.getLogger(__name__)

MIN_SCHEMA_VERSION = 43
SOURCE = "recorder_import"
DEFAULT_BATCH_ROWS = 200_000

_STATES_SQL = """
SELECT s.state_id, coalesce(m.entity_id, s.entity_id), s.state, a.shared_attrs,
       coalesce(s.last_changed_ts, s.last_updated_ts), s.last_updated_ts
FROM states s
LEFT JOIN states_meta m ON s.metadata_id = m.metadata_id
LEFT JOIN state_attributes a ON s.attributes_id = a.attributes_id
WHERE s.last_updated_ts >= ? AND s.last_updated_ts < ? AND s.state_id > ?
ORDER BY s.state_id
LIMIT ?
"""

_STATISTICS_SQL = """
SELECT s.id, m.statistic_id, s.start_ts, s.mean, s.min, s.max, s.state, s.sum, s.last_reset_ts
FROM {table} s JOIN statistics_meta m ON s.metadata_id = m.id
WHERE s.start_ts >= ? AND s.start_ts < ? AND s.id > ?
ORDER BY s.id
LIMIT ?
"""


class RecorderSchemaError(RuntimeError):
    """The database is not a supported Home Assistant recorder database."""


@dataclass(slots=True)
class ImportResult:
    states_read: int = 0
    states_inserted: int = 0
    statistics_read: dict[str, int] = field(default_factory=dict)
    statistics_inserted: dict[str, int] = field(default_factory=dict)
    metadata_inserted: int = 0
    first_state: datetime | None = None
    last_state: datetime | None = None


def open_recorder(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    try:
        version = schema_version(con)
    except sqlite3.DatabaseError as err:
        con.close()
        raise RecorderSchemaError(f"{p} is not a recorder database: {err}") from err
    if version < MIN_SCHEMA_VERSION:
        con.close()
        raise RecorderSchemaError(
            f"Recorder schema {version} is too old (need >= {MIN_SCHEMA_VERSION}, HA 2023.4+)"
        )
    return con


def schema_version(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT max(schema_version) FROM schema_changes").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _ts_bounds(start: datetime | None, end: datetime | None) -> tuple[float, float]:
    lower = start.timestamp() if start else 0.0
    upper = end.timestamp() if end else float("inf")
    return lower, upper


def _state_lines(rows: list[tuple[Any, ...]]) -> Iterator[str]:
    for _, entity_id, state, shared_attrs, last_changed, last_updated in rows:
        # shared_attrs is the JSON text Home Assistant wrote; it is embedded verbatim.
        attrs = shared_attrs if shared_attrs else "null"
        yield (
            f'{{"entity_id":{json.dumps(entity_id)},"state":{json.dumps(state)},'
            f'"attributes":{attrs},"last_changed_us":{to_us(last_changed)},'
            f'"last_updated_us":{to_us(last_updated)},"context_id":null}}\n'
        )


def _stage_lines(con: duckdb.DuckDBPyConnection, name: str, lines: Iterator[str]) -> None:
    fd, path = tempfile.mkstemp(prefix=f"hammond-{name}-", suffix=".ndjson")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        cols = ", ".join(
            f"{sql_literal(k)}: {sql_literal(v)}" for k, v in bronze.STATES_STAGE_COLUMNS.items()
        )
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {name} AS SELECT entity_id, state, attributes, "
            "make_timestamp(last_changed_us)::TIMESTAMPTZ AS last_changed, "
            "make_timestamp(last_updated_us)::TIMESTAMPTZ AS last_updated, context_id "
            f"FROM read_json({sql_literal(path)}, format = 'newline_delimited', "
            f"columns = {{{cols}}})"
        )
    finally:
        os.unlink(path)


def import_states(
    rec: sqlite3.Connection,
    lake: duckdb.DuckDBPyConnection,
    result: ImportResult,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    alias: str = LAKE_ALIAS,
) -> None:
    lower, upper = _ts_bounds(start, end)
    last_id = -1
    while True:
        rows = rec.execute(_STATES_SQL, (lower, upper, last_id, batch_rows)).fetchall()
        if not rows:
            break
        last_id = int(rows[-1][0])
        valid = [r for r in rows if r[1] and r[5] is not None]
        _stage_lines(lake, "_rec_states", _state_lines(valid))
        inserted = bronze.insert_states_from_stage(lake, "_rec_states", source=SOURCE, alias=alias)
        result.states_read += len(rows)
        result.states_inserted += inserted
        if valid:
            first = us_to_datetime(to_us(min(r[5] for r in valid)))
            last = us_to_datetime(to_us(max(r[5] for r in valid)))
            result.first_state = min(result.first_state or first, first)
            result.last_state = max(result.last_state or last, last)
        logger.info(
            "States: read %d, inserted %d (up to state_id %d)",
            result.states_read,
            result.states_inserted,
            last_id,
        )


def import_statistics(
    rec: sqlite3.Connection,
    lake: duckdb.DuckDBPyConnection,
    result: ImportResult,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    alias: str = LAKE_ALIAS,
) -> None:
    lower, upper = _ts_bounds(start, end)
    for period, table in (("hour", "statistics"), ("5minute", "statistics_short_term")):
        last_id = -1
        read = inserted = 0
        sql = _STATISTICS_SQL.format(table=table)
        while True:
            rows = rec.execute(sql, (lower, upper, last_id, batch_rows)).fetchall()
            if not rows:
                break
            last_id = int(rows[-1][0])
            records = (
                {
                    "statistic_id": r[1],
                    "period": period,
                    "start_us": to_us(r[2]),
                    "mean": r[3],
                    "min": r[4],
                    "max": r[5],
                    "state": r[6],
                    "sum": r[7],
                    "last_reset_us": to_us(r[8]) if r[8] is not None else None,
                }
                for r in rows
                if r[2] is not None
            )
            bronze.stage_statistics(lake, records)
            inserted += bronze.insert_statistics_from_stage(
                lake, "_stage_stats", source=SOURCE, alias=alias
            )
            read += len(rows)
        result.statistics_read[period] = read
        result.statistics_inserted[period] = inserted
        logger.info("Statistics %s: read %d, inserted %d", period, read, inserted)


def import_statistics_metadata(
    rec: sqlite3.Connection,
    lake: duckdb.DuckDBPyConnection,
    result: ImportResult,
    *,
    alias: str = LAKE_ALIAS,
) -> None:
    """Metadata of statistics that the live instance no longer knows (removed sensors).

    Items already recorded from the API are never overridden by older backup values.
    """
    rec.row_factory = sqlite3.Row
    try:
        rows = rec.execute("SELECT * FROM statistics_meta").fetchall()
    finally:
        rec.row_factory = None
    items = [
        {
            "statistic_id": r["statistic_id"],
            "source": r["source"],
            "statistics_unit_of_measurement": r["unit_of_measurement"],
            "display_unit_of_measurement": r["unit_of_measurement"],
            "unit_class": r["unit_class"] if "unit_class" in r.keys() else None,  # noqa: SIM118
            "has_mean": bool(r["has_mean"]),
            "has_sum": bool(r["has_sum"]),
            "mean_type": r["mean_type"] if "mean_type" in r.keys() else None,  # noqa: SIM118
            "name": r["name"],
        }
        for r in rows
    ]
    result.metadata_inserted = bronze.write_registry(
        lake, "statistics_meta", items, source=SOURCE, complete=False, only_new=True, alias=alias
    )


def import_recorder_db(
    path: str | Path,
    lake: duckdb.DuckDBPyConnection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    alias: str = LAKE_ALIAS,
) -> ImportResult:
    """Import states, statistics and statistics metadata of one recorder database."""
    bronze.ensure_schema(lake, alias)
    result = ImportResult()
    rec = open_recorder(path)
    try:
        import_statistics_metadata(rec, lake, result, alias=alias)
        import_statistics(
            rec, lake, result, start=start, end=end, batch_rows=batch_rows, alias=alias
        )
        import_states(rec, lake, result, start=start, end=end, batch_rows=batch_rows, alias=alias)
    finally:
        rec.close()
    return result
