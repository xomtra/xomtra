"""Status data for the admin pages (trusted, fixed SQL on the read-only lake)."""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl
from collections.abc import Callable
from typing import Any

import duckdb
from websockets.exceptions import InvalidMessage, InvalidStatus, InvalidURI

from hacore import HAAuthError, HAClient, HAError, HAProtocolError, fetch_registries
from xomtra_mcp.data import DataAccess, jsonable

type Row = dict[str, Any]


async def ha_status(url: str | None, token: str | None) -> Row:
    if not (url and token):
        return {"configured": False}
    try:
        async with asyncio.timeout(15), HAClient(url, token) as ha:
            snapshot = await fetch_registries(ha)
            version = ha.ha_version
    except (HAError, TimeoutError, OSError) as err:
        return {
            "configured": True,
            "ok": False,
            "error": explain(err),
            "detail": f"{type(err).__name__}: {err}"[:300],
        }
    return {
        "configured": True,
        "ok": True,
        "version": version,
        "areas": len(snapshot.areas),
        "devices": len(snapshot.devices),
        "entities": sum(snapshot.domain_counts().values()),
    }


def _causes(err: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = err
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


# OpenSSL reasons when an https:// address reaches a plain HTTP server.
_PLAIN_HTTP = {"WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE", "PACKET_LENGTH_TOO_LONG"}


def _timed_out(c: BaseException) -> bool:
    return isinstance(c, TimeoutError) or (
        isinstance(c, OSError) and c.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH)
    )


# The first cause in the chain that matches decides the sentence (translated in the GUI).
_EXPLANATIONS: list[tuple[Callable[[BaseException], bool], str]] = [
    (
        lambda c: isinstance(c, HAAuthError),
        "Home Assistant rejected the access token. Create a new long-lived access token "
        "and paste it whole.",
    ),
    (
        lambda c: isinstance(c, ssl.SSLCertVerificationError),
        "This server does not trust the certificate of Home Assistant (self-signed, or "
        "issued for another name). Use the http:// address in your network, or the name "
        "the certificate was issued for.",
    ),
    (
        lambda c: isinstance(c, ssl.SSLError) and c.reason in _PLAIN_HTTP,
        "Home Assistant does not use HTTPS at this address. Try the same address with "
        "http:// instead of https://.",
    ),
    (
        lambda c: isinstance(c, socket.gaierror),
        "This server cannot resolve the name in the address. Use the IP address of Home "
        "Assistant instead, for example http://192.168.1.10:8123.",
    ),
    (
        lambda c: isinstance(c, ConnectionRefusedError),
        "Nothing answers on this address and port. Home Assistant normally uses the port 8123.",
    ),
    (
        lambda c: isinstance(c, InvalidURI),
        "The address is not valid. Enter it like http://192.168.1.10:8123.",
    ),
    (
        lambda c: isinstance(
            c, (InvalidStatus, InvalidMessage, HAProtocolError, ConnectionResetError)
        ),
        "Something other than Home Assistant answered at this address. Check the address "
        "and the port; if Home Assistant uses HTTPS, use https://, and a reverse proxy "
        "must pass WebSockets.",
    ),
    (
        _timed_out,
        "Home Assistant did not answer in time. Check that this server can reach it: the "
        "address, the port and a firewall between them.",
    ),
]


def explain(err: BaseException) -> str:
    """A sentence for the admin GUI on why Home Assistant cannot be reached (translated).

    The technical message stays next to it as a detail.
    """
    for cause in _causes(err):
        for matches, sentence in _EXPLANATIONS:
            if matches(cause):
                return sentence
    return "Home Assistant cannot be reached at this address."


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


def catalog_profiled(data: DataAccess) -> bool:
    """True once the daily profile has written ``catalog.entity`` (False on a new lake)."""
    with data.trusted_cursor() as cur:
        return bool(_rows(cur, "SELECT 1 AS x FROM lake.catalog.entity LIMIT 1"))


def drift_rows(data: DataAccess, days: int = 30, limit: int = 1000) -> list[Row]:
    with data.trusted_cursor() as cur:
        return _rows(
            cur,
            "SELECT detected_at, entity_id, drift_type, field, old_value, new_value "
            "FROM lake.catalog.drift WHERE detected_at >= now() - to_days(?) "
            f"ORDER BY detected_at DESC, entity_id LIMIT {int(limit)}",
            [int(days)],
        )
