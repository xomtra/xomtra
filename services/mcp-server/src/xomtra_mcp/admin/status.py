"""Status data for the admin pages (trusted, fixed SQL on the read-only lake)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import duckdb

from hacore import HAClient, HAError, fetch_registries
from xomtra_mcp.data import DataAccess, jsonable

type Row = dict[str, Any]


async def ha_status(url: str | None, token: str | None) -> Row:
    if not (url and token):
        return {"configured": False}
    started = time.monotonic()
    try:
        async with asyncio.timeout(15), HAClient(url, token) as ha:
            snapshot = await fetch_registries(ha)
            version = ha.ha_version
    except (HAError, TimeoutError, OSError) as err:
        return {"configured": True, "ok": False, "error": f"{type(err).__name__}: {err}"[:300]}
    return {
        "configured": True,
        "ok": True,
        "version": version,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "areas": len(snapshot.areas),
        "devices": len(snapshot.devices),
        "entities": sum(snapshot.domain_counts().values()),
    }


def _rows(cur: duckdb.DuckDBPyConnection, sql: str, params: Any = None) -> list[Row]:
    try:
        cur.execute(sql, params)
    except duckdb.CatalogException:
        return []  # not created yet (fresh installation)
    cols = [d[0] for d in cur.description or []]
    return [{c: jsonable(v) for c, v in zip(cols, r, strict=True)} for r in cur.fetchall()]


def lake_overview(data: DataAccess) -> Row:
    with data.trusted_cursor() as cur:
        fresh = _rows(
            cur,
            "SELECT max(last_updated) AS last_state FROM lake.bronze.ha_states "
            "WHERE last_updated >= now() - INTERVAL 2 DAY",
        )
        stats = _rows(
            cur,
            "SELECT max(period_start) AS last_statistic FROM lake.bronze.ha_statistics "
            "WHERE period_start >= now() - INTERVAL 3 DAY",
        )
        kinds = _rows(
            cur,
            "SELECT kind, count(*) AS entities, count(*) FILTER (WHERE stale) AS stale, "
            "max(profiled_at) AS profiled_at FROM lake.catalog.entity GROUP BY ALL ORDER BY 2 DESC",
        )
        drift = _rows(
            cur,
            "SELECT drift_type, count(*) AS n FROM lake.catalog.drift "
            "WHERE detected_at >= now() - INTERVAL 7 DAY GROUP BY ALL ORDER BY 2 DESC",
        )
    return {
        "last_state": (fresh[0]["last_state"] if fresh else None),
        "last_statistic": (stats[0]["last_statistic"] if stats else None),
        "kinds": kinds,
        "drift_7d": drift,
    }


def table_sizes(data: DataAccess) -> list[Row]:
    with data.trusted_cursor() as cur:
        return _rows(
            cur,
            "SELECT t.schema_name AS schema, t.table_name AS table, "
            "t.estimated_size AS rows, i.file_count AS files, "
            "round(i.file_size_bytes / 1048576, 1) AS size_mib, "
            "i.delete_file_count AS delete_files "
            "FROM duckdb_tables() AS t "
            "JOIN ducklake_table_info('lake') AS i ON i.table_name = t.table_name "
            "WHERE t.database_name = 'lake' ORDER BY i.file_size_bytes DESC",
        )


def catalog_rows(
    data: DataAccess, query: str = "", kind: str = "", only_stale: bool = False, limit: int = 500
) -> list[Row]:
    where, params = ["TRUE"], []
    for token in query.lower().split()[:10]:
        where.append(
            "contains(lower(concat_ws(' ', entity_id, name, area_name, device_name, "
            "device_class, unit_of_measurement)), ?)"
        )
        params.append(token)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if only_stale:
        where.append("stale")
    with data.trusted_cursor() as cur:
        return _rows(
            cur,
            "SELECT entity_id, name, area_name, kind, unit_of_measurement, changes_per_day, "
            "numeric_share, missing_share, value_min, value_p50, value_max, last_seen, "
            f"stale, range_shift FROM lake.catalog.entity WHERE {' AND '.join(where)} "
            f"ORDER BY entity_id LIMIT {int(limit)}",
            params,
        )


def drift_rows(data: DataAccess, days: int = 30, limit: int = 1000) -> list[Row]:
    with data.trusted_cursor() as cur:
        return _rows(
            cur,
            "SELECT detected_at, entity_id, drift_type, field, old_value, new_value "
            "FROM lake.catalog.drift WHERE detected_at >= now() - to_days(?) "
            f"ORDER BY detected_at DESC, entity_id LIMIT {int(limit)}",
            [int(days)],
        )
