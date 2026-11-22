"""Recorder data from the Home Assistant WebSocket API: state history and statistics.

All timestamps are converted to integer microseconds since the Unix epoch (UTC). Home
Assistant keeps them as float seconds derived from microsecond datetimes, so rounding to
microseconds recovers the exact value and the same state change from the live stream,
the history API and the recorder database gets the same key (``entity_id``, ``last_updated``).

Large ranges are split into time windows and entity batches so that one response stays
well below the WebSocket message limit.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from hacore.client import HAClient

logger = logging.getLogger(__name__)

type JSON = dict[str, Any]
type Period = Literal["5minute", "hour"]

PERIODS: tuple[Period, ...] = ("5minute", "hour")
STATISTIC_TYPES = ["mean", "min", "max", "state", "sum", "last_reset"]
DEFAULT_ENTITY_BATCH = 100
DEFAULT_HISTORY_WINDOW = timedelta(days=1)
DEFAULT_STATISTICS_WINDOW: dict[Period, timedelta] = {
    "5minute": timedelta(days=1),
    "hour": timedelta(days=30),
}


def to_us(seconds: float) -> int:
    """Float epoch seconds -> integer epoch microseconds."""
    return round(seconds * 1_000_000)


def iso_to_us(value: str) -> int:
    """ISO 8601 timestamp (as in state objects) -> integer epoch microseconds."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def us_to_datetime(us: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)


@dataclass(frozen=True, slots=True)
class StateRow:
    """One recorded state of an entity."""

    entity_id: str
    state: str | None
    attributes: JSON | None
    last_changed_us: int
    last_updated_us: int
    context_id: str | None = None

    @classmethod
    def from_state_object(cls, obj: JSON) -> StateRow:
        """From a full state object (``get_states``, ``state_changed`` events)."""
        last_updated = iso_to_us(obj["last_updated"])
        last_changed = obj.get("last_changed")
        context = obj.get("context")
        return cls(
            entity_id=obj["entity_id"],
            state=obj.get("state"),
            attributes=obj.get("attributes"),
            last_changed_us=iso_to_us(last_changed) if last_changed else last_updated,
            last_updated_us=last_updated,
            context_id=context.get("id") if isinstance(context, dict) else None,
        )

    @classmethod
    def from_compressed(cls, entity_id: str, obj: JSON) -> StateRow:
        """From the compressed format of ``history/history_during_period``."""
        last_updated = to_us(obj["lu"])
        last_changed = obj.get("lc")
        return cls(
            entity_id=entity_id,
            state=obj.get("s"),
            attributes=obj.get("a"),
            last_changed_us=to_us(last_changed) if last_changed is not None else last_updated,
            last_updated_us=last_updated,
        )

    def as_record(self) -> JSON:
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": self.attributes,
            "last_changed_us": self.last_changed_us,
            "last_updated_us": self.last_updated_us,
            "context_id": self.context_id,
        }


@dataclass(frozen=True, slots=True)
class StatisticRow:
    """One statistics period (5 minutes or 1 hour) of one statistic."""

    statistic_id: str
    period: Period
    start_us: int
    mean: float | None = None
    min: float | None = None
    max: float | None = None
    state: float | None = None
    sum: float | None = None
    last_reset_us: int | None = None

    @classmethod
    def from_ha(cls, statistic_id: str, period: Period, obj: JSON) -> StatisticRow:
        # statistics_during_period returns epoch milliseconds (floats).
        last_reset = obj.get("last_reset")
        return cls(
            statistic_id=statistic_id,
            period=period,
            start_us=round(obj["start"] * 1000),
            mean=obj.get("mean"),
            min=obj.get("min"),
            max=obj.get("max"),
            state=obj.get("state"),
            sum=obj.get("sum"),
            last_reset_us=round(last_reset * 1000) if last_reset is not None else None,
        )

    def as_record(self) -> JSON:
        return {
            "statistic_id": self.statistic_id,
            "period": self.period,
            "start_us": self.start_us,
            "mean": self.mean,
            "min": self.min,
            "max": self.max,
            "state": self.state,
            "sum": self.sum,
            "last_reset_us": self.last_reset_us,
        }


def batched[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def windows(start: datetime, end: datetime, step: timedelta) -> Iterable[tuple[datetime, datetime]]:
    """Split [start, end) into consecutive windows of at most ``step``."""
    cursor = start
    while cursor < end:
        upper = min(cursor + step, end)
        yield cursor, upper
        cursor = upper


async def history_during_period(
    client: HAClient,
    start: datetime,
    end: datetime,
    entity_ids: Sequence[str],
) -> list[StateRow]:
    """All recorded states of ``entity_ids`` with ``start <= last_updated < end``.

    Every state change is returned (not only significant ones) with full attributes.
    """
    if not entity_ids:
        return []
    result: JSON = await client.send_command(
        "history/history_during_period",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        entity_ids=list(entity_ids),
        include_start_time_state=False,
        significant_changes_only=False,
        minimal_response=False,
        no_attributes=False,
    )
    rows: list[StateRow] = []
    for entity_id, states in (result or {}).items():
        rows.extend(StateRow.from_compressed(entity_id, s) for s in states)
    return rows


async def iter_history(
    client: HAClient,
    start: datetime,
    end: datetime,
    entity_ids: Sequence[str],
    *,
    window: timedelta = DEFAULT_HISTORY_WINDOW,
    entity_batch: int = DEFAULT_ENTITY_BATCH,
) -> AsyncIterator[list[StateRow]]:
    """History of a longer range, one chunk (window x entity batch) at a time."""
    ids = sorted(set(entity_ids))
    for lower, upper in windows(start, end, window):
        for batch in batched(ids, entity_batch):
            rows = await history_during_period(client, lower, upper, batch)
            if rows:
                yield rows


async def list_statistic_ids(client: HAClient) -> list[JSON]:
    result: list[JSON] = await client.send_command("recorder/list_statistic_ids")
    return result


async def statistics_during_period(
    client: HAClient,
    start: datetime,
    end: datetime,
    statistic_ids: Sequence[str],
    period: Period,
) -> list[StatisticRow]:
    """Statistics rows with ``start <= period start < end``, in statistics units."""
    if not statistic_ids:
        return []
    result: JSON = await client.send_command(
        "recorder/statistics_during_period",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        statistic_ids=list(statistic_ids),
        period=period,
        types=STATISTIC_TYPES,
        # No unit override: values come in the display unit of each statistic, which
        # ``recorder/list_statistic_ids`` reports as display_unit_of_measurement.
    )
    rows: list[StatisticRow] = []
    for statistic_id, items in (result or {}).items():
        rows.extend(StatisticRow.from_ha(statistic_id, period, item) for item in items)
    return rows


async def iter_statistics(
    client: HAClient,
    start: datetime,
    end: datetime,
    statistic_ids: Sequence[str],
    period: Period,
    *,
    window: timedelta | None = None,
    id_batch: int = DEFAULT_ENTITY_BATCH,
) -> AsyncIterator[list[StatisticRow]]:
    step = window or DEFAULT_STATISTICS_WINDOW[period]
    ids = sorted(set(statistic_ids))
    for lower, upper in windows(start, end, step):
        for batch in batched(ids, id_batch):
            rows = await statistics_during_period(client, lower, upper, batch, period)
            if rows:
                yield rows
