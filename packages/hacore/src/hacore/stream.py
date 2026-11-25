"""Continuous ingest of ``state_changed`` events with catch-up after outages.

On every (re)connect the stream

1. subscribes to ``state_changed`` (events are buffered from now on),
2. fills the gap since the newest stored state from the history API (catch-up),
3. flushes the buffer to bronze every ``flush_interval`` seconds or ``max_buffer`` rows.

Catch-up and events overlap on purpose; the bronze writer skips keys that already exist,
so nothing is lost and nothing is duplicated. A catch-up older than the silver lookback
(``restate_threshold``) is recorded in ``bronze.ingest_gaps``; a Dagster sensor then
restates silver for that range (ADR 031). The gap that can be closed is limited by
the recorder retention of Home Assistant (``purge_keep_days``, 10 days by default);
data of longer outages is lost (Xomtra takes data only from the running instance).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from hacore import bronze
from hacore.client import HAClient
from hacore.errors import HAAuthError, HAError
from hacore.exclusions import DEFAULT_LABELS, ExclusionLabels, fetch_no_ingest
from hacore.history import StateRow, us_to_datetime
from hacore.ingest import backfill_history
from hacore.lake import LAKE_ALIAS

logger = logging.getLogger(__name__)

SOURCE_LIVE = "ha_live"
SOURCE_CATCHUP = "ha_catchup"


@dataclass(frozen=True, slots=True)
class StreamSettings:
    flush_interval: float = 30.0
    max_buffer: int = 5_000
    ping_interval: float = 30.0
    max_catchup: timedelta = timedelta(days=10)
    catchup_overlap: timedelta = timedelta(minutes=5)
    # Silver recomputes the last 3 hours on every run; older catch-ups need a restate.
    restate_threshold: timedelta = timedelta(hours=2)
    reconnect_min: float = 2.0
    reconnect_max: float = 300.0
    heartbeat_file: Path | None = None
    # How often the no-ingest labels are re-read from the registries.
    exclusions_refresh: float = 300.0
    labels: ExclusionLabels = DEFAULT_LABELS


class StateStream:
    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        lake_factory: Callable[[], duckdb.DuckDBPyConnection],
        settings: StreamSettings | None = None,
        *,
        alias: str = LAKE_ALIAS,
    ) -> None:
        self._url = ha_url
        self._token = ha_token
        self._lake_factory = lake_factory
        self._settings = settings or StreamSettings()
        self._alias = alias
        self._buffer: list[StateRow] = []
        self._lake: duckdb.DuckDBPyConnection | None = None
        self._lake_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._exclude: frozenset[str] = frozenset()
        self.written = 0

    # -- public ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Run until ``stop()``; reconnects with exponential backoff and jitter."""
        delay = self._settings.reconnect_min
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.run_once()
            except HAAuthError:
                # A wrong token does not heal by retrying quickly.
                logger.exception("Home Assistant rejected the token")
                delay = self._settings.reconnect_max
            except (HAError, OSError, TimeoutError, duckdb.Error) as err:
                logger.warning("Stream interrupted: %s", err)
            if self._stop.is_set():
                break
            if time.monotonic() - started > 60:
                delay = self._settings.reconnect_min
            sleep = delay * (0.5 + random.random())  # noqa: S311 - jitter, not crypto
            logger.info("Reconnecting in %.0f s", sleep)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=sleep)
            delay = min(delay * 2, self._settings.reconnect_max)
        await self._flush()
        self._close_lake()

    async def run_once(self) -> None:
        """One connection lifetime: subscribe, catch up, flush until disconnect/stop."""
        async with HAClient(self._url, self._token) as ha:
            unsubscribe = await ha.subscribe_events(self._on_event, "state_changed")
            try:
                await self._refresh_exclusions(ha)
                await self._catch_up(ha)
                await self._pump(ha)
            finally:
                with contextlib.suppress(HAError, OSError, TimeoutError):
                    await unsubscribe()
                await self._flush()

    # -- internals ---------------------------------------------------------------

    def _on_event(self, event: dict[str, Any]) -> None:
        new_state = (event.get("data") or {}).get("new_state")
        if not new_state:  # entity removed
            return
        if new_state.get("entity_id") in self._exclude:
            return
        try:
            self._buffer.append(StateRow.from_state_object(new_state))
        except (KeyError, ValueError, TypeError):
            logger.warning("Ignoring malformed state_changed event")

    async def _refresh_exclusions(self, ha: HAClient) -> None:
        exclude = await fetch_no_ingest(ha, self._settings.labels)
        if exclude != self._exclude:
            logger.info("Entities labelled %s: %d", self._settings.labels.no_ingest, len(exclude))
        self._exclude = exclude
        # Drop buffered states of entities that were just labelled.
        self._buffer = [r for r in self._buffer if r.entity_id not in exclude]

    def _connection(self) -> duckdb.DuckDBPyConnection:
        if self._lake is None:
            self._lake = self._lake_factory()
            bronze.ensure_schema(self._lake, self._alias)
        return self._lake

    def _close_lake(self) -> None:
        if self._lake is not None:
            self._lake.close()
            self._lake = None

    async def _catch_up(self, ha: HAClient) -> None:
        async with self._lake_lock:
            watermark = await asyncio.to_thread(
                lambda: bronze.state_watermark_us(self._connection(), self._alias)
            )
            now = datetime.now(UTC)
            if watermark is None:
                logger.info("No stored states yet: no catch-up (run the history backfill)")
                return
            start = us_to_datetime(watermark) - self._settings.catchup_overlap
            if now - start > self._settings.max_catchup:
                logger.warning(
                    "Gap since %s exceeds the recorder retention; catching up only %s.",
                    start.isoformat(),
                    self._settings.max_catchup,
                )
                start = now - self._settings.max_catchup
            lake = self._connection()
        result = await backfill_history(
            ha, lake, start, now, exclude=self._exclude, alias=self._alias
        )
        written = result.written.get("states", 0)
        self.written += written
        logger.info("Catch-up from %s: %d states written", start.isoformat(), written)
        if written and now - start > self._settings.restate_threshold:
            async with self._lake_lock:
                await asyncio.to_thread(
                    bronze.record_gap, self._connection(), start, now, written, alias=self._alias
                )
            logger.info("Gap recorded; silver will be restated from %s", start.isoformat())

    async def _pump(self, ha: HAClient) -> None:
        s = self._settings
        last_flush = last_ping = last_refresh = time.monotonic()
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            if not ha.connected:
                raise HAError("Connection to Home Assistant lost")
            now = time.monotonic()
            if now - last_ping >= s.ping_interval:
                await ha.ping()
                last_ping = now
            if now - last_refresh >= s.exclusions_refresh:
                await self._refresh_exclusions(ha)
                last_refresh = now
            if len(self._buffer) >= s.max_buffer or now - last_flush >= s.flush_interval:
                await self._flush()
                last_flush = now
                self._heartbeat()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        async with self._lake_lock:
            try:
                written = await asyncio.to_thread(
                    lambda: bronze.write_states(
                        self._connection(), rows, source=SOURCE_LIVE, alias=self._alias
                    )
                )
            except BaseException:
                # Keep the rows for the next attempt; a reconnect also re-reads history.
                self._buffer[:0] = rows
                self._close_lake()
                raise
        self.written += written
        logger.debug("Flushed %d states (%d new)", len(rows), written)

    def _heartbeat(self) -> None:
        path = self._settings.heartbeat_file
        if path is not None:
            with contextlib.suppress(OSError):
                path.touch()
