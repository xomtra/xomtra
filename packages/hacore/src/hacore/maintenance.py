"""Retention and DuckLake housekeeping.

Retention deletes rows older than the configured period from bronze and from the
physical silver tables. States, 5-minute and hourly statistics have separate periods
(ADR 034). In DuckLake a DELETE only writes delete files; the data is gone
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
from hacore.history import PERIODS, Period
from hacore.lake import LAKE_ALIAS, sql_literal

logger = logging.getLogger(__name__)

DEFAULT_STATES_RETENTION = timedelta(days=365)
DEFAULT_STATISTICS_RETENTION: dict[Period, timedelta] = {
    "5minute": timedelta(days=365),
    "hour": timedelta(days=5 * 365),
}

# (schema, table name prefix). Silver physical tables are created by SQLMesh as
# sqlmesh__silver.silver__<model>__<fingerprint>.
STATE_TABLES: tuple[tuple[str, str], ...] = (
    (bronze.SCHEMA, bronze.STATES),
    ("sqlmesh__silver", "silver__states__"),
)
STATISTICS_TABLES: tuple[tuple[str, str], ...] = (
    (bronze.SCHEMA, bronze.STATISTICS),
    ("sqlmesh__silver", "silver__statistics__"),
)


@dataclass(frozen=True, slots=True)
class Retention:
    """How long rows are kept: states and each statistics period separately."""

    states: timedelta = DEFAULT_STATES_RETENTION
    statistics: dict[Period, timedelta] = field(
        default_factory=lambda: dict(DEFAULT_STATISTICS_RETENTION)
    )


@dataclass(slots=True)
class RetentionResult:
    cutoffs: dict[str, datetime] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)


def _tables(con: duckdb.DuckDBPyConnection, alias: str, schema: str, prefix: str) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() "
        "WHERE database_name = ? AND schema_name = ? AND starts_with(table_name, ?)",
        [alias, schema, prefix],
    ).fetchall()
    return sorted(r[0] for r in rows)


def _ts(value: datetime) -> str:
    return f"{sql_literal(value.isoformat())}::TIMESTAMPTZ"


def apply_retention(
    con: duckdb.DuckDBPyConnection,
    retention: Retention | None = None,
    *,
    now: datetime | None = None,
    alias: str = LAKE_ALIAS,
) -> RetentionResult:
    r = retention or Retention()
    current = now or datetime.now(UTC)
    result = RetentionResult(cutoffs={"states": current - r.states})
    for period in PERIODS:
        result.cutoffs[f"statistics_{period}"] = current - r.statistics[period]

    states_where = f"last_updated < {_ts(result.cutoffs['states'])}"
    statistics_where = " OR ".join(
        f"(period = {sql_literal(p)} AND period_start < {_ts(result.cutoffs[f'statistics_{p}'])})"
        for p in PERIODS
    )
    for tables, where in ((STATE_TABLES, states_where), (STATISTICS_TABLES, statistics_where)):
        for schema, prefix in tables:
            for name in _tables(con, alias, schema, prefix):
                target = f"{alias}.{schema}.{name}"
                sql = f"DELETE FROM {target} WHERE {where}"
                deleted = bronze.with_conflict_retry(functools.partial(_execute_count, con, sql))
                result.deleted[f"{schema}.{name}"] = deleted
                if deleted:
                    logger.info("Retention: deleted %d rows from %s", deleted, target)
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
