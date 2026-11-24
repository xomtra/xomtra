"""Entities left out by Home Assistant labels (ADR 044).

A label on an entity or on its device decides:

``hammond_no_ingest``    the entity is not stored at all (live stream, catch-up, history
                         backfill, statistics); data stored before stays.
``hammond_no_analysis``  the entity is stored and can be queried, but derived analyses
                         (correlations, anomalies, change points) leave it out.

``no_ingest`` implies ``no_analysis``. The labels live in Home Assistant, where the
entities are managed, so HAmmond needs no write path of its own; a label on a device
covers its future entities too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import duckdb

from hacore.client import HAClient
from hacore.lake import LAKE_ALIAS
from hacore.registry import REGISTRY_COMMANDS

NO_INGEST = "hammond_no_ingest"
NO_ANALYSIS = "hammond_no_analysis"


def labelled(
    entities: Iterable[dict[str, Any]], devices: Iterable[dict[str, Any]], labels: set[str]
) -> frozenset[str]:
    """Entity ids whose own labels or whose device's labels include any of ``labels``."""
    device_labels = {
        d["id"]: set(d.get("labels") or []) for d in devices if d.get("id") is not None
    }
    out = set()
    for e in entities:
        own = set(e.get("labels") or [])
        dev = device_labels.get(e.get("device_id") or "", set())
        if (own | dev) & labels:
            out.add(e["entity_id"])
    return frozenset(out)


async def fetch_no_ingest(client: HAClient) -> frozenset[str]:
    """Entities labelled ``hammond_no_ingest`` (on the entity or its device)."""
    entities, devices = await asyncio.gather(
        client.send_command(REGISTRY_COMMANDS["entities"]),
        client.send_command(REGISTRY_COMMANDS["devices"]),
    )
    return labelled(entities or [], devices or [], {NO_INGEST})


def load_no_analysis(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> frozenset[str]:
    """Entities left out of analyses, from ``silver.entities`` (entity and device labels)."""
    exists = con.execute(
        "SELECT count(*) FROM (SELECT database_name AS d, schema_name AS s, table_name AS t "
        "FROM duckdb_tables() UNION ALL SELECT database_name, schema_name, view_name "
        "FROM duckdb_views()) WHERE d = $d AND s = 'silver' AND t = 'entities'",
        {"d": alias},
    ).fetchone()
    if not exists or not exists[0]:
        return frozenset()
    rows = con.execute(
        f"SELECT entity_id FROM {alias}.silver.entities "  # noqa: S608 - alias is ours
        "WHERE list_has_any(label_ids, $labels::VARCHAR[])",
        {"labels": [NO_INGEST, NO_ANALYSIS]},
    ).fetchall()
    return frozenset(str(r[0]) for r in rows)
