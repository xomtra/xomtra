"""Read access to the lake for MCP tools (ADR 039).

One DuckDB process-wide connection attaches the lake READ_ONLY and is then locked down:
no file or network access outside the lake's data path, no Python replacement scans,
configuration locked. Every tool call works on its own cursor, where the views the
caller may read are created as temporary views that already exclude the entities their
policy denies; queries can only name those views (``hammond_mcp.sqlguard``).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import duckdb

from hacore.lake import LAKE_ALIAS, LakeSettings, connect, connect_local, sql_literal
from hammond_mcp import sqlguard
from hammond_mcp.policy import Access

logger = logging.getLogger(__name__)

type Row = dict[str, Any]
Resolution = Literal["auto", "raw", "5minute", "hour", "day"]


@dataclass(frozen=True, slots=True)
class View:
    name: str
    source: str  # relative to the lake alias
    entity_columns: tuple[str, ...]  # a row is hidden if any of them is a denied entity
    description: str


VIEWS: dict[str, View] = {
    v.name: v
    for v in (
        View(
            "entities",
            "silver.entities",
            ("entity_id",),
            "Current entities: name, domain, device, area, floor, labels, unit, "
            "device_class, state_class, flags.",
        ),
        View(
            "states",
            "silver.states",
            ("entity_id",),
            "Every state change: entity_id, state (text), state_value (number or NULL), "
            "is_missing, attributes (JSON), last_changed, last_updated (UTC).",
        ),
        View(
            "statistics",
            "silver.statistics",
            ("statistic_id",),
            "Home Assistant long-term statistics per period ('5minute', 'hour'): "
            "mean/min/max for measurements, state/sum for counters; period_start in UTC.",
        ),
        View(
            "statistics_meta",
            "silver.statistics_meta",
            ("statistic_id",),
            "Statistics metadata: unit the values are stored in, has_mean, has_sum.",
        ),
        View(
            "catalog_entity",
            "catalog.entity",
            ("entity_id",),
            "Entity catalog with a profile of the last 30 days: kind, changes per day, "
            "numeric/missing share, value quantiles, top states, stale, range_shift.",
        ),
        View(
            "catalog_drift",
            "catalog.drift",
            ("entity_id",),
            "Changes between daily catalog profiles: new/removed entities, metadata "
            "changes, stopped/resumed reporting, range shifts.",
        ),
        View(
            "catalog_finding",
            "catalog.finding",
            ("entity_id", "related_entity_id"),
            "Findings of the daily analysis. finding_type 'correlation': entity_id "
            "(metric mean/delta/share of state) leads related_entity_id by lag_hours; "
            "score = correlation of hourly residuals after the daily/weekly cycle and the "
            "outside temperature; q_value = FDR; flags duplicate_suspect, same_device; "
            "window_days, period_start/end, first_detected_at, detected_at; rank 1 = strongest.",
        ),
    )
}
KNOWN_VIEWS = frozenset(VIEWS)

SEARCH_COLUMNS = (
    "entity_id, name, domain, area_name, floor_name, device_name, manufacturer, model, "
    "unit_of_measurement, device_class, state_class, kind, changes_per_day, last_seen, "
    "stale"
)
# Errors whose message helps the caller fix the query; others are reported generically.
_USER_ERRORS = (
    duckdb.ParserException,
    duckdb.BinderException,
    duckdb.CatalogException,
    duckdb.ConversionException,
    duckdb.InvalidInputException,
    duckdb.OutOfRangeException,
    duckdb.InterruptException,
)


class QueryFailed(RuntimeError):  # noqa: N818 - message is shown to the caller
    """A query failed; the message is safe to show to the caller."""


def jsonable(value: Any) -> Any:  # noqa: PLR0911 - one return per type
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def parse_time(value: str | None, *, now: datetime | None = None) -> datetime:
    """ISO date/time (UTC if no offset) or relative ``<n>d`` / ``<n>h`` before now."""
    now = now or datetime.now(UTC)
    if not value:
        return now
    v = value.strip().lower()
    if v[:-1].isdigit() and v[-1] in "dh":
        n = int(v[:-1])
        return now - (timedelta(days=n) if v[-1] == "d" else timedelta(hours=n))
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as err:
        raise ValueError(f"Invalid time {value!r}: use ISO 8601 or e.g. '7d', '12h'") from err
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DataAccess:
    """Lake queries for MCP tools, always under a caller's :class:`Access`."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        timezone: str = "UTC",
        query_timeout: float = 30.0,
    ) -> None:
        self._con = con
        self.timezone = timezone
        self.query_timeout = query_timeout

    @classmethod
    def from_settings(
        cls, settings: LakeSettings, *, timezone: str = "UTC", query_timeout: float = 30.0
    ) -> DataAccess:
        con = connect(settings, read_only=True)
        lockdown(con, [settings.data_path])
        return cls(con, timezone=timezone, query_timeout=query_timeout)

    @classmethod
    def local(
        cls, catalog: str, data_path: str, *, extension_dir: str | None = None, **kwargs: Any
    ) -> DataAccess:
        con = connect_local(catalog, data_path, extension_dir=extension_dir, read_only=True)
        lockdown(con, [data_path])
        return cls(con, **kwargs)

    def close(self) -> None:
        self._con.close()

    @contextmanager
    def trusted_cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Cursor on the whole (read-only, locked-down) lake for fixed internal SQL of the
        admin GUI. Never pass user SQL here; that goes through :meth:`query_sql`."""
        cur = self._con.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ---- per-call cursor -----------------------------------------------------------

    @contextmanager
    def _cursor(self, access: Access) -> Iterator[duckdb.DuckDBPyConnection]:
        cur = self._con.cursor()
        try:
            deny = " OR ".join(f"{{col}} GLOB {sql_literal(p)}" for p in access.deny_entities)
            # A fresh installation has no silver or catalog yet: skip what does not exist.
            existing = {
                r[0]
                for r in cur.execute(
                    "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
                    "WHERE database_name = $db UNION ALL "
                    "SELECT schema_name || '.' || view_name FROM duckdb_views() "
                    "WHERE database_name = $db",
                    {"db": LAKE_ALIAS},
                ).fetchall()
            }
            for view in VIEWS.values():
                if view.source not in existing:
                    continue
                hidden = " OR ".join(
                    f"coalesce({deny.format(col=col)}, FALSE)" for col in view.entity_columns
                )
                where = f" WHERE NOT ({hidden})" if deny else ""
                cur.execute(
                    f"CREATE TEMP VIEW {view.name} AS "
                    f"SELECT * FROM {LAKE_ALIAS}.{view.source}{where}"
                )
            yield cur
        finally:
            cur.close()

    def _timed[T](self, cur: duckdb.DuckDBPyConnection, fn: Callable[[], T]) -> T:
        timer = threading.Timer(self.query_timeout, cur.interrupt)
        timer.start()
        try:
            return fn()
        except duckdb.InterruptException as err:
            raise QueryFailed(f"Query cancelled after {self.query_timeout:g} s") from err
        except _USER_ERRORS as err:
            raise QueryFailed(str(err).splitlines()[0]) from err
        except duckdb.Error as err:
            logger.warning("Query failed: %s", err)
            raise QueryFailed("Query failed") from err
        finally:
            timer.cancel()

    @staticmethod
    def _rows(cur: duckdb.DuckDBPyConnection, limit: int | None = None) -> list[Row]:
        cols = [d[0] for d in cur.description or []]
        data = cur.fetchmany(limit) if limit is not None else cur.fetchall()
        return [{c: jsonable(v) for c, v in zip(cols, r, strict=True)} for r in data]

    # ---- tools ---------------------------------------------------------------------

    def list_views(self, access: Access) -> list[Row]:
        out = []
        with self._cursor(access) as cur:
            for name in access.allowed_views:
                try:
                    cols = cur.execute(f"DESCRIBE {name}").fetchall()
                except duckdb.CatalogException:
                    continue  # not created yet (fresh installation)
                out.append(
                    {
                        "name": name,
                        "description": VIEWS[name].description,
                        "columns": [{"name": c[0], "type": c[1]} for c in cols],
                    }
                )
        return out

    def search_catalog(
        self,
        access: Access,
        *,
        query: str = "",
        domain: str | None = None,
        area: str | None = None,
        kind: str | None = None,
        include_disabled: bool = False,
        limit: int = 50,
    ) -> Row:
        limit = max(1, min(limit, access.max_rows, 500))
        where = ["TRUE"]
        params: list[Any] = []
        haystack = (
            "lower(concat_ws(' ', entity_id, name, area_name, floor_name, device_name, "
            "manufacturer, model, device_class, unit_of_measurement, kind))"
        )
        for token in query.lower().split()[:10]:
            where.append(f"contains({haystack}, ?)")
            params.append(token)
        if domain:
            where.append("domain = ?")
            params.append(domain)
        if area:
            where.append("(lower(area_name) = lower(?) OR lower(floor_name) = lower(?))")
            params += [area, area]
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if not include_disabled:
            where.append("NOT coalesce(disabled, FALSE)")
        sql = (
            f"SELECT {SEARCH_COLUMNS} FROM catalog_entity WHERE {' AND '.join(where)} "
            f"ORDER BY changes_per_day DESC NULLS LAST, entity_id LIMIT {limit + 1}"
        )
        with self._cursor(access) as cur:
            rows = self._timed(cur, lambda: self._rows(cur.execute(sql, params)))
        return {
            "entities": rows[:limit],
            "row_count": min(len(rows), limit),
            "truncated": len(rows) > limit,
        }

    def describe_entity(self, access: Access, entity_id: str) -> Row:
        access.require_entity(entity_id)
        with self._cursor(access) as cur:

            def run() -> Row:
                cur.execute("SELECT * FROM catalog_entity WHERE entity_id = ?", [entity_id])
                found = self._rows(cur)
                if not found:
                    raise QueryFailed(f"Entity {entity_id} is not in the catalog")
                entity = found[0]
                cur.execute(
                    "SELECT state, state_value, last_changed, last_updated FROM states "
                    "WHERE entity_id = ? AND last_updated >= now() - INTERVAL 30 DAY "
                    "ORDER BY last_updated DESC LIMIT 1",
                    [entity_id],
                )
                latest = self._rows(cur)
                cur.execute(
                    "SELECT detected_at, drift_type, field, old_value, new_value "
                    "FROM catalog_drift WHERE entity_id = ? ORDER BY detected_at DESC LIMIT 20",
                    [entity_id],
                )
                drift = self._rows(cur)
                cur.execute("SELECT * FROM statistics_meta WHERE statistic_id = ?", [entity_id])
                stats = self._rows(cur)
                return {
                    "entity": entity,
                    "latest_state": latest[0] if latest else None,
                    "statistics": stats[0] if stats else None,
                    "drift": drift,
                    "row_count": 1,
                }

            return self._timed(cur, run)

    def get_timeseries(
        self,
        access: Access,
        entity_id: str,
        *,
        start: datetime,
        end: datetime,
        resolution: Resolution = "auto",
    ) -> Row:
        access.require_entity(entity_id)
        access.require_view("states")
        if end <= start:
            raise ValueError("end must be after start")
        span = end - start
        if resolution == "auto":
            if span <= timedelta(days=2):
                resolution = "raw"
            elif span <= timedelta(days=31):
                resolution = "hour"
            else:
                resolution = "day"
        limit = access.max_rows
        with self._cursor(access) as cur:

            def run() -> Row:
                has_stats = False
                if resolution != "raw" and access.can_view("statistics"):
                    period = "5minute" if resolution == "5minute" else "hour"
                    found = cur.execute(
                        "SELECT count(*) FROM statistics WHERE statistic_id = ? "
                        "AND period = ? AND period_start >= ? AND period_start < ?",
                        [entity_id, period, start, end],
                    ).fetchone()
                    has_stats = bool(found and found[0])
                if has_stats:
                    source, sql, params = self._statistics_sql(entity_id, resolution, start, end)
                else:
                    source, sql, params = self._states_sql(entity_id, resolution, start, end)
                cur.execute(sql, params)
                points = self._rows(cur, limit + 1)
                return {
                    "entity_id": entity_id,
                    "source": source,
                    "resolution": resolution,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "timezone": self.timezone if resolution == "day" else "UTC",
                    "points": points[:limit],
                    "row_count": min(len(points), limit),
                    "truncated": len(points) > limit,
                }

            return self._timed(cur, run)

    def _bucket(self, resolution: str, column: str) -> str:
        if resolution == "day":
            tz = sql_literal(self.timezone)
            return f"timezone({tz}, date_trunc('day', timezone({tz}, {column})))"
        interval = {"5minute": "5 minutes", "hour": "1 hour"}[resolution]
        return f"time_bucket(INTERVAL '{interval}', {column})"

    def _statistics_sql(
        self, entity_id: str, resolution: str, start: datetime, end: datetime
    ) -> tuple[str, str, list[Any]]:
        period = "5minute" if resolution == "5minute" else "hour"
        bucket = self._bucket(resolution, "period_start")
        sql = (
            f"SELECT {bucket} AS t, avg(mean) AS mean, min(min) AS min, max(max) AS max, "
            "arg_max(state, period_start) AS state, arg_max(sum, period_start) AS sum "
            "FROM statistics WHERE statistic_id = ? AND period = ? "
            "AND period_start >= ? AND period_start < ? GROUP BY ALL ORDER BY t"
        )
        return f"statistics:{period}", sql, [entity_id, period, start, end]

    def _states_sql(
        self, entity_id: str, resolution: str, start: datetime, end: datetime
    ) -> tuple[str, str, list[Any]]:
        if resolution == "raw":
            sql = (
                "SELECT last_updated AS t, state, state_value AS value FROM states "
                "WHERE entity_id = ? AND last_updated >= ? AND last_updated < ? "
                "ORDER BY last_updated"
            )
        else:
            bucket = self._bucket(resolution, "last_updated")
            sql = (
                f"SELECT {bucket} AS t, avg(state_value) AS mean, min(state_value) AS min, "
                "max(state_value) AS max, arg_max(state, last_updated) AS last_state, "
                "count(*) AS changes FROM states "
                "WHERE entity_id = ? AND last_updated >= ? AND last_updated < ? "
                "GROUP BY ALL ORDER BY t"
            )
        return "states", sql, [entity_id, start, end]

    def query_sql(self, access: Access, sql: str) -> Row:
        allowed = frozenset(access.allowed_views)
        guarded = sqlguard.check(sql, allowed, max_rows=access.max_rows)
        with self._cursor(access) as cur:
            started = time.monotonic()

            def run() -> tuple[list[str], list[Row]]:
                cur.execute(guarded.sql)
                cols = [d[0] for d in cur.description or []]
                return cols, self._rows(cur, access.max_rows + 1)

            columns, rows = self._timed(cur, run)
        return {
            "columns": columns,
            "rows": rows[: access.max_rows],
            "row_count": min(len(rows), access.max_rows),
            "truncated": len(rows) > access.max_rows,
            "views": sorted(guarded.views),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }


def lockdown(con: duckdb.DuckDBPyConnection, allowed_paths: list[str]) -> None:
    """Restrict a connection whose lake is already attached (read-only) for user SQL."""
    paths = ", ".join(sql_literal(p) for p in allowed_paths)
    # Cursors of this connection inherit global settings: results are always in UTC.
    con.execute("SET GLOBAL TimeZone = 'UTC'")
    con.execute(f"SET allowed_directories = [{paths}]")
    con.execute("SET enable_external_access = false")
    con.execute("SET python_enable_replacements = false")
    con.execute("SET lock_configuration = true")
