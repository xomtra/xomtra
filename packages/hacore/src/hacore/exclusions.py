"""Entities left out by Home Assistant labels (ADR 044).

A label on an entity or on its device decides:

``hammond_no_ingest``    the entity is not stored at all (live stream, catch-up, history
                         backfill, statistics); data stored before stays.
``hammond_no_analysis``  the entity is stored and can be queried, but derived analyses
                         (correlations, anomalies, change points) leave it out.

``no_ingest`` implies ``no_analysis``. The labels live in Home Assistant, where the
entities are managed, so HAmmond needs no write path of its own; a label on a device
covers its future entities too.

The label ids above are the defaults; deployments can choose their own
(``HAMMOND_LABEL_NO_INGEST``, ``HAMMOND_LABEL_NO_ANALYSIS``), passed in as ``labels``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import duckdb

from hacore.client import HAClient
from hacore.lake import LAKE_ALIAS
from hacore.registry import REGISTRY_COMMANDS

NO_INGEST = "hammond_no_ingest"
NO_ANALYSIS = "hammond_no_analysis"


@dataclass(frozen=True, slots=True)
class ExclusionLabels:
    """Home Assistant label ids that leave entities out (on the entity or its device)."""

    no_ingest: str = NO_INGEST
    no_analysis: str = NO_ANALYSIS

    def __post_init__(self) -> None:
        if not self.no_ingest or not self.no_analysis:
            raise ValueError("exclusion labels must not be empty")

    @property
    def analysis(self) -> list[str]:
        """Labels that leave an entity out of analyses (``no_ingest`` implies it)."""
        return sorted({self.no_ingest, self.no_analysis})


DEFAULT_LABELS = ExclusionLabels()


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


async def fetch_no_ingest(
    client: HAClient, labels: ExclusionLabels = DEFAULT_LABELS
) -> frozenset[str]:
    """Entities labelled ``labels.no_ingest`` (on the entity or its device)."""
    entities, devices = await asyncio.gather(
        client.send_command(REGISTRY_COMMANDS["entities"]),
        client.send_command(REGISTRY_COMMANDS["devices"]),
    )
    return labelled(entities or [], devices or [], {labels.no_ingest})


def load_no_analysis(
    con: duckdb.DuckDBPyConnection,
    alias: str = LAKE_ALIAS,
    labels: ExclusionLabels = DEFAULT_LABELS,
) -> frozenset[str]:
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
        {"labels": labels.analysis},
    ).fetchall()
    return frozenset(str(r[0]) for r in rows)
