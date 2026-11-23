"""Ingest jobs over the Home Assistant WebSocket API into the bronze layer.

``sync_registries``  registries + statistics metadata (change log)
``sync_statistics``  5-minute and hourly statistics from the last stored period on
``backfill_history`` full state history of a time range

All of them are idempotent and safe to repeat. The DuckDB work runs in a worker thread
so the WebSocket connection stays responsive (pings, events) during large writes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from hacore import bronze
from hacore.exclusions import fetch_no_ingest
from hacore.history import (
    DEFAULT_HISTORY_WINDOW,
    PERIODS,
    Period,
    iter_history,
    iter_statistics,
    list_statistic_ids,
    us_to_datetime,
)
from hacore.lake import LAKE_ALIAS
from hacore.registry import REGISTRY_COMMANDS

if TYPE_CHECKING:
    from collections.abc import Collection

    import duckdb

    from hacore.client import HAClient

logger = logging.getLogger(__name__)

type JSON = dict[str, Any]

SOURCE_API = "ha_api"
SOURCE_HISTORY = "ha_history"

# Home Assistant keeps 5-minute statistics for 10 days by default (recorder
# purge_keep_days); hourly statistics are kept forever.
DEFAULT_LOOKBACK: dict[Period, timedelta] = {
    "5minute": timedelta(days=10),
    "hour": timedelta(days=5 * 365),
}
# Re-read the last stored periods: the newest one may have been compiled again.
STATISTICS_OVERLAP: dict[Period, timedelta] = {
    "5minute": timedelta(minutes=15),
    "hour": timedelta(hours=3),
}


@dataclass(slots=True)
class SyncResult:
    read: dict[str, int] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, read: int, written: int) -> None:
        self.read[key] = self.read.get(key, 0) + read
        self.written[key] = self.written.get(key, 0) + written


async def sync_registries(
    client: HAClient, lake: duckdb.DuckDBPyConnection, *, alias: str = LAKE_ALIAS
) -> SyncResult:
    result = SyncResult()
    commands = dict(REGISTRY_COMMANDS)
    raw = await asyncio.gather(*(client.send_command(cmd) for cmd in commands.values()))
    meta, states = await asyncio.gather(list_statistic_ids(client), client.get_states())
    extra = [("statistics_meta", meta), ("entity_attributes", entity_attributes(states))]
    for name, items in [*zip(commands, raw, strict=True), *extra]:
        written = await asyncio.to_thread(
            bronze.write_registry, lake, name, items or [], source=SOURCE_API, alias=alias
        )
        result.add(name, len(items or []), written)
    logger.info("Registries synced: %s", result.written)
    return result


# Attributes that describe an entity (not its current value). Kept as a change log next
# to the registries because the entity registry does not carry them.
DESCRIPTIVE_ATTRIBUTES = ("friendly_name", "unit_of_measurement", "device_class", "state_class")


def entity_attributes(states: list[JSON]) -> list[JSON]:
    items = []
    for s in states:
        attrs = s.get("attributes") or {}
        items.append(
            {"entity_id": s["entity_id"], **{k: attrs.get(k) for k in DESCRIPTIVE_ATTRIBUTES}}
        )
    return items


async def sync_statistics(
    client: HAClient,
    lake: duckdb.DuckDBPyConnection,
    *,
    periods: tuple[Period, ...] = PERIODS,
    start: datetime | None = None,
    end: datetime | None = None,
    lookback: dict[Period, timedelta] | None = None,
    exclude: Collection[str] | None = None,
    alias: str = LAKE_ALIAS,
) -> SyncResult:
    """Fetch statistics newer than what is stored (or ``start``..``end`` if given).

    Statistics of entities in ``exclude`` (by default those labelled
    ``hammond_no_ingest``) are skipped."""
    result = SyncResult()
    now = end or datetime.now(UTC)
    skip = await fetch_no_ingest(client) if exclude is None else exclude
    ids = [
        m["statistic_id"] for m in await list_statistic_ids(client) if m["statistic_id"] not in skip
    ]
    for period in periods:
        lower = start
        if lower is None:
            watermark = await asyncio.to_thread(bronze.statistics_watermark_us, lake, period, alias)
            if watermark is None:
                lower = now - (lookback or DEFAULT_LOOKBACK)[period]
            else:
                lower = us_to_datetime(watermark) - STATISTICS_OVERLAP[period]
        async for rows in iter_statistics(client, lower, now, ids, period):
            written = await asyncio.to_thread(
                bronze.write_statistics, lake, rows, source=SOURCE_API, alias=alias
            )
            result.add(period, len(rows), written)
        logger.info(
            "Statistics %s from %s: read %d, written %d",
            period,
            lower.isoformat(),
            result.read.get(period, 0),
            result.written.get(period, 0),
        )
    return result


async def known_entity_ids(client: HAClient) -> list[str]:
    """Entities with a current state plus all registry entities (incl. unavailable)."""
    states, entities = await asyncio.gather(
        client.get_states(), client.send_command(REGISTRY_COMMANDS["entities"])
    )
    ids = {s["entity_id"] for s in states} | {e["entity_id"] for e in entities or []}
    return sorted(ids)


async def backfill_history(
    client: HAClient,
    lake: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    *,
    entity_ids: list[str] | None = None,
    window: timedelta = DEFAULT_HISTORY_WINDOW,
    exclude: Collection[str] | None = None,
    alias: str = LAKE_ALIAS,
) -> SyncResult:
    """Copy the recorded state history of ``start``..``end`` into bronze, without the
    entities in ``exclude`` (by default those labelled ``hammond_no_ingest``)."""
    result = SyncResult()
    skip = await fetch_no_ingest(client) if exclude is None else exclude
    ids = entity_ids if entity_ids is not None else await known_entity_ids(client)
    ids = [i for i in ids if i not in skip]
    if not ids:
        return result
    async for rows in iter_history(client, start, end, ids, window=window):
        written = await asyncio.to_thread(
            bronze.write_states, lake, rows, source=SOURCE_HISTORY, alias=alias
        )
        result.add("states", len(rows), written)
    logger.info(
        "History %s .. %s for %d entities: read %d, written %d",
        start.isoformat(),
        end.isoformat(),
        len(ids),
        result.read.get("states", 0),
        result.written.get("states", 0),
    )
    return result
