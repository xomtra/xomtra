"""Analysis settings of the admin GUI (ADR 060): store and impact preview.

Changes are appended to ``analysis.setting_change`` in the audit database (the admin
role may read and append, never update or delete, so the history stays). The latest
row of a key is its value; ``NULL`` hands the key back to the ingest's environment or
default. The ingest reads them at the start of every analysis run.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import duckdb

from hacore.analysis_settings import BY_KEY, Value
from hammond_mcp.data import DataAccess


class SettingsStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Change:
    key: str
    value: Value | None
    changed_at: datetime
    changed_by: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class DismissalRow:
    id: int
    finding_type: str
    entity_id: str
    related_entity_id: str | None
    reason: str | None
    until: datetime | None
    dismissed_at: datetime
    dismissed_by: str | None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    def active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.revoked_at is None and (self.until is None or self.until > now)


FINDING_TYPES = ("correlation", "anomaly", "change_point")


class SettingsStore(Protocol):
    def current(self) -> dict[str, Change]: ...
    def history(self, limit: int = 50) -> list[Change]: ...
    def set(self, key: str, value: Value | None, *, by: str | None, reason: str | None) -> None: ...
    def dismissals(self, limit: int = 200) -> list[DismissalRow]: ...
    def dismiss(
        self,
        finding_type: str,
        entity_id: str,
        related_entity_id: str | None,
        *,
        by: str | None,
        reason: str | None,
        until: datetime | None,
    ) -> int: ...
    def undo(self, dismissal_id: int, by: str | None) -> bool: ...


class MemorySettingsStore:
    """For tests and the development mode without a database."""

    def __init__(self) -> None:
        self.changes: list[Change] = []
        self.dismissed: list[DismissalRow] = []

    def dismissals(self, limit: int = 200) -> list[DismissalRow]:
        return list(reversed(self.dismissed))[:limit]

    def dismiss(
        self,
        finding_type: str,
        entity_id: str,
        related_entity_id: str | None,
        *,
        by: str | None,
        reason: str | None,
        until: datetime | None,
    ) -> int:
        row = DismissalRow(
            len(self.dismissed) + 1,
            finding_type,
            entity_id,
            related_entity_id,
            reason,
            until,
            datetime.now(UTC),
            by,
        )
        self.dismissed.append(row)
        return row.id

    def undo(self, dismissal_id: int, by: str | None) -> bool:
        for i, d in enumerate(self.dismissed):
            if d.id == dismissal_id and d.revoked_at is None:
                self.dismissed[i] = replace(d, revoked_at=datetime.now(UTC), revoked_by=by)
                return True
        return False

    def current(self) -> dict[str, Change]:
        out: dict[str, Change] = {}
        for c in self.changes:
            out[c.key] = c
        return {k: c for k, c in out.items() if c.value is not None}

    def history(self, limit: int = 50) -> list[Change]:
        return list(reversed(self.changes))[:limit]

    def set(self, key: str, value: Value | None, *, by: str | None, reason: str | None) -> None:
        self.changes.append(Change(key, value, datetime.now(UTC), by, reason))


class PostgresSettingsStore:
    """``analysis.setting_change`` in the audit database (psycopg2, one connection)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None
        self._lock = threading.Lock()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        import psycopg2  # noqa: PLC0415 - only needed with the database store

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = psycopg2.connect(self._dsn, connect_timeout=5)
                        self._conn.autocommit = True
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        return list(cur.fetchall()) if cur.description else []
                except psycopg2.OperationalError as err:
                    self._conn = None
                    if attempt == 2:
                        raise SettingsStoreError("Settings store unavailable") from err
                except psycopg2.Error as err:
                    raise SettingsStoreError("Settings store error") from err
        return []  # pragma: no cover - loop always returns or raises

    def current(self) -> dict[str, Change]:
        rows = self._run(
            "SELECT DISTINCT ON (key) key, value, changed_at, changed_by, reason "
            "FROM analysis.setting_change ORDER BY key, id DESC"
        )
        return {r[0]: Change(*r) for r in rows if r[1] is not None}

    def history(self, limit: int = 50) -> list[Change]:
        rows = self._run(
            "SELECT key, value, changed_at, changed_by, reason FROM analysis.setting_change "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return [Change(*r) for r in rows]

    def set(self, key: str, value: Value | None, *, by: str | None, reason: str | None) -> None:
        from psycopg2.extras import Json  # noqa: PLC0415

        if key not in BY_KEY:
            raise SettingsStoreError(f"unknown setting {key}")
        self._run(
            "INSERT INTO analysis.setting_change (key, value, changed_by, reason) "
            "VALUES (%s, %s, %s, %s)",
            (key, None if value is None else Json(value), by, reason),
        )

    def dismissals(self, limit: int = 200) -> list[DismissalRow]:
        rows = self._run(
            "SELECT id, finding_type, entity_id, related_entity_id, reason, until, "
            "dismissed_at, dismissed_by, revoked_at, revoked_by "
            "FROM analysis.finding_dismissal ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return [DismissalRow(*r) for r in rows]

    def dismiss(
        self,
        finding_type: str,
        entity_id: str,
        related_entity_id: str | None,
        *,
        by: str | None,
        reason: str | None,
        until: datetime | None,
    ) -> int:
        rows = self._run(
            "INSERT INTO analysis.finding_dismissal (finding_type, entity_id, "
            "related_entity_id, reason, until, dismissed_by) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (finding_type, entity_id, related_entity_id, reason, until, by),
        )
        return int(rows[0][0])

    def undo(self, dismissal_id: int, by: str | None) -> bool:
        rows = self._run(
            "UPDATE analysis.finding_dismissal SET revoked_at = now(), revoked_by = %s "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (by, dismissal_id),
        )
        return bool(rows)


# ---- impact preview (ADR 060 point 4) ------------------------------------------------

# Keys whose effect on the stored findings is a filter: tightening is exact, loosening
# only a lower bound (weaker candidates are not stored).
PREVIEW_KEYS = ("correlation_max_q", "correlation_min_r", "anomaly_max_q")


def preview(data: DataAccess, key: str, value: Value) -> dict[str, Any] | None:
    """Findings of the last runs now and with ``key`` = ``value``."""
    if key not in PREVIEW_KEYS:
        return None
    if key == "anomaly_max_q":
        where, keep = "finding_type IN ('anomaly', 'change_point')", "q_value <= $v"
    elif key == "correlation_max_q":
        where, keep = "finding_type = 'correlation'", "q_value <= $v"
    else:
        where, keep = "finding_type = 'correlation'", "abs(score) >= $v"
    sql = (
        "SELECT count(*) AS now, count(*) FILTER (WHERE "  # noqa: S608 - fixed fragments
        f"{keep}) AS kept, count(*) FILTER (WHERE {keep} AND len(flags) = 0) AS kept_unflagged, "
        f"count(*) FILTER (WHERE len(flags) = 0) AS unflagged FROM lake.catalog.finding "
        f"WHERE {where}"
    )
    with data.trusted_cursor() as cur:
        try:
            cur.execute(sql, {"v": float(value)})
        except duckdb.Error:  # no findings table yet
            return None
        row = cur.fetchone()
    if row is None:
        return None
    now, kept, kept_unflagged, unflagged = (int(x or 0) for x in row)
    return {
        "now": now,
        "kept": kept,
        "unflagged": unflagged,
        "kept_unflagged": kept_unflagged,
    }


def current_findings(
    data: DataAccess, finding_type: str = "", include_flagged: bool = False, limit: int = 300
) -> list[dict[str, Any]]:
    """Current findings for the dismiss page (trusted, fixed SQL)."""
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit}
    if finding_type in FINDING_TYPES:
        where.append("finding_type = $type")
        params["type"] = finding_type
    if not include_flagged:
        where.append("len(coalesce(flags, [])) = 0")
    sql = (
        "SELECT finding_type, entity_id, related_entity_id, window_days, "  # noqa: S608
        "round(score, 3) AS score, q_value, flags, period_start FROM lake.catalog.finding "
        f"WHERE {' AND '.join(where)} ORDER BY finding_type, window_days DESC, rank LIMIT $limit"
    )
    with data.trusted_cursor() as cur:
        try:
            cur.execute(sql, params)
        except duckdb.Error:  # no findings table yet
            return []
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
