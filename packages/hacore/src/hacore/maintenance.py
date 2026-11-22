"""Retention and DuckLake housekeeping.

Retention deletes rows older than the configured period from bronze and from the
physical silver tables. In DuckLake a DELETE only writes delete files; the data is gone
from object storage only after the snapshots that still reference it are expired and
the files are cleaned up (``compact_and_expire``). Retention is therefore effective
after ``snapshot_retention`` at the latest, which also bounds time travel.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import duckdb

from hacore import bronze
from hacore.lake import LAKE_ALIAS, sql_literal

logger = logging.getLogger(__name__)

# (schema, table name prefix, time column). Silver physical tables are created by
# SQLMesh as sqlmesh__silver.silver__<model>__<fingerprint>.
RETENTION_TARGETS: tuple[tuple[str, str, str], ...] = (
    (bronze.SCHEMA, bronze.STATES, "last_updated"),
    (bronze.SCHEMA, bronze.STATISTICS, "period_start"),
    ("sqlmesh__silver", "silver__states__", "last_updated"),
    ("sqlmesh__silver", "silver__statistics__", "period_start"),
)


@dataclass(slots=True)
class RetentionResult:
    cutoff: datetime
    deleted: dict[str, int] = field(default_factory=dict)


def _tables(con: duckdb.DuckDBPyConnection, alias: str, schema: str, prefix: str) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = ? AND schema_name = ? AND starts_with(table_name, ?)",
        [alias, schema, prefix],
    ).fetchall()
    return sorted(r[0] for r in rows)


def apply_retention(
    con: duckdb.DuckDBPyConnection,
    retention: timedelta,
    *,
    now: datetime | None = None,
    alias: str = LAKE_ALIAS,
) -> RetentionResult:
    cutoff = (now or datetime.now(UTC)) - retention
    result = RetentionResult(cutoff=cutoff)
    literal = sql_literal(cutoff.isoformat())
    for schema, prefix, column in RETENTION_TARGETS:
        for name in _tables(con, alias, schema, prefix):
            target = f"{alias}.{schema}.{name}"
            sql = f"DELETE FROM {target} WHERE {column} < {literal}::TIMESTAMPTZ"
            deleted = bronze.with_conflict_retry(functools.partial(_execute_count, con, sql))
            result.deleted[f"{schema}.{name}"] = deleted
            if deleted:
                logger.info(
                    "Retention: deleted %d rows older than %s from %s", deleted, cutoff, target
                )
    return result


def _execute_count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    return int(row[0]) if row else 0


def compact_and_expire(
    con: duckdb.DuckDBPyConnection,
    snapshot_retention: timedelta,
    *,
    alias: str = LAKE_ALIAS,
) -> None:
    """Merge small files (the live stream writes many), drop old snapshots, delete files."""
    lit = sql_literal(alias)
    con.execute(f"CALL ducklake_merge_adjacent_files({lit})")
    # Rewrite files with many deleted rows (retention) so deleted data really goes away.
    con.execute(f"CALL ducklake_rewrite_data_files({lit})")
    interval = f"INTERVAL '{int(snapshot_retention.total_seconds())} seconds'"
    con.execute(f"CALL ducklake_expire_snapshots({lit}, older_than => now() - {interval})")
    con.execute(f"CALL ducklake_cleanup_old_files({lit}, older_than => now() - {interval})")
    logger.info("DuckLake maintenance done (snapshots older than %s expired)", snapshot_retention)
