"""Analysis settings of the admin GUI (ADR 060): store and impact preview.

Changes are appended to ``analysis.setting_change`` in the audit database (the admin
role may read and append, never update or delete, so the history stays). The latest
row of a key is its value; ``NULL`` hands the key back to the ingest's environment or
default. The ingest reads them at the start of every analysis run.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
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


class SettingsStore(Protocol):
    def current(self) -> dict[str, Change]: ...
    def history(self, limit: int = 50) -> list[Change]: ...
    def set(self, key: str, value: Value | None, *, by: str | None, reason: str | None) -> None: ...


class MemorySettingsStore:
    """For tests and the development mode without a database."""

    def __init__(self) -> None:
        self.changes: list[Change] = []

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
