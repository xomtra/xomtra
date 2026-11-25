"""Weekly snapshots of findings (phase 1b.2, ADR 048).

``catalog.finding`` holds the current findings only: every daily run replaces the
findings of its type and window, and anomalies older than the recent window drop out.
The weekly job copies all current findings into ``catalog.finding_weekly`` under the
local date of the Monday of the week just ended (``week_start``). Comparing two
snapshots gives what is new and what is gone; the MCP tool ``get_weekly_summary``
renders that under the caller's access policy (entities a role may not see are
filtered when read, so nothing is pre-rendered here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from hacore import correlations as corr
from hacore.lake import LAKE_ALIAS

logger = logging.getLogger(__name__)

WEEKLY = "finding_weekly"
DEFAULT_KEEP_WEEKS = 104


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    week_start: date
    findings: int
    by_type: dict[str, int]
    deleted_weeks: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start.isoformat(),
            "findings": self.findings,
            "by_type": self.by_type,
            "deleted_weeks": self.deleted_weeks,
        }


def ensure_schema(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> None:
    corr.ensure_schema(con, alias)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {alias}.{corr.SCHEMA}.{WEEKLY} "
        f"(week_start DATE NOT NULL, snapshot_at TIMESTAMPTZ NOT NULL, {corr.FINDING_COLUMNS})"
    )
    for column, type_ in corr.ADDED_COLUMNS:
        con.execute(
            f"ALTER TABLE {alias}.{corr.SCHEMA}.{WEEKLY} ADD COLUMN IF NOT EXISTS {column} {type_}"
        )


def last_week_start(now: datetime, timezone: str) -> date:
    """Local Monday of the last complete week before ``now``."""
    today = now.astimezone(ZoneInfo(timezone)).date()
    return today - timedelta(days=today.weekday() + 7)


def snapshot_week(
    con: duckdb.DuckDBPyConnection,
    *,
    timezone: str = "UTC",
    now: datetime | None = None,
    keep_weeks: int = DEFAULT_KEEP_WEEKS,
    alias: str = LAKE_ALIAS,
) -> SnapshotResult:
    """Copy the current findings as the snapshot of the week just ended (replacing an
    earlier snapshot of the same week) and drop snapshots older than ``keep_weeks``."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    ensure_schema(con, alias)
    week = last_week_start(now, timezone)
    t = f"{alias}.{corr.SCHEMA}.{WEEKLY}"
    src = f"{alias}.{corr.SCHEMA}.{corr.FINDING}"
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"DELETE FROM {t} WHERE week_start = $w", {"w": week})
        con.execute(
            f"INSERT INTO {t} BY NAME SELECT $w AS week_start, $now AS snapshot_at, * FROM {src}",
            {"w": week, "now": now},
        )
        row = con.execute(
            f"SELECT count(DISTINCT week_start) FROM {t} WHERE week_start < $cut",
            {"cut": week - timedelta(weeks=keep_weeks)},
        ).fetchone()
        con.execute(
            f"DELETE FROM {t} WHERE week_start < $cut",
            {"cut": week - timedelta(weeks=keep_weeks)},
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    by_type = dict(
        con.execute(
            f"SELECT finding_type, count(*) FROM {t} WHERE week_start = $w GROUP BY ALL ORDER BY 1",
            {"w": week},
        ).fetchall()
    )
    result = SnapshotResult(
        week_start=week,
        findings=sum(by_type.values()),
        by_type={str(k): int(v) for k, v in by_type.items()},
        deleted_weeks=int(row[0]) if row else 0,
    )
    logger.info("Weekly findings snapshot: %s", result.as_dict())
    return result


# ---- dismissed findings (ADR 060 point 6) --------------------------------------------

DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class Dismissal:
    """A finding a user does not want to see again: a pair of entities (correlation,
    in either order) or an entity (anomaly, change point), across runs."""

    finding_type: str
    entity_id: str
    related_entity_id: str | None = None

    def matches(self, finding_type: str, entity_id: str, related: str | None) -> bool:
        if finding_type != self.finding_type:
            return False
        if self.related_entity_id is None:
            return entity_id == self.entity_id
        return {entity_id, related} == {self.entity_id, self.related_entity_id}


def apply_dismissals(
    con: duckdb.DuckDBPyConnection, dismissals: list[Dismissal], alias: str = LAKE_ALIAS
) -> int:
    """Flag the current findings that match a dismissal ``dismissed`` (hidden like the
    other flags unless asked for); returns how many were flagged now."""
    if not dismissals:
        return 0
    t = f"{alias}.{corr.SCHEMA}.{corr.FINDING}"
    rows = con.execute(
        f"SELECT finding_id, finding_type, entity_id, related_entity_id FROM {t} "
        "WHERE NOT list_contains(coalesce(flags, []), $d)",
        {"d": DISMISSED},
    ).fetchall()
    ids = [
        (fid, ftype)
        for fid, ftype, entity, related in rows
        if any(d.matches(ftype, entity, related) for d in dismissals)
    ]
    if ids:
        con.executemany(
            f"UPDATE {t} SET flags = list_append(coalesce(flags, []), $d), hidden = TRUE "
            "WHERE finding_id = $id AND finding_type = $type",
            [{"d": DISMISSED, "id": fid, "type": ftype} for fid, ftype in ids],
        )
    return len(ids)
