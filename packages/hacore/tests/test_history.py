from datetime import UTC, datetime, timedelta
from typing import Any

from hacore import HAClient
from hacore.history import (
    StateRow,
    StatisticRow,
    iso_to_us,
    iter_history,
    iter_statistics,
    to_us,
    us_to_datetime,
    windows,
)
from hacore.testing import VALID_TOKEN, FakeHA

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_timestamp_conversions_agree() -> None:
    # The same instant from an event (ISO), the history API and the recorder (float).
    iso = "2026-11-22T22:53:58.523396+00:00"
    seconds = datetime.fromisoformat(iso).timestamp()
    assert iso_to_us(iso) == to_us(seconds)
    assert us_to_datetime(iso_to_us(iso)) == datetime.fromisoformat(iso)
    assert iso_to_us("2026-01-01T00:00:00") == iso_to_us("2026-01-01T00:00:00+00:00")


def test_state_row_from_compressed_defaults_last_changed() -> None:
    row = StateRow.from_compressed("sensor.a", {"s": "1", "a": {"x": 1}, "lu": 1.5})
    assert row.last_changed_us == row.last_updated_us == 1_500_000
    row = StateRow.from_compressed("sensor.a", {"s": "1", "lu": 2.0, "lc": 1.0})
    assert (row.last_changed_us, row.last_updated_us, row.attributes) == (
        1_000_000,
        2_000_000,
        None,
    )


def test_state_row_from_state_object() -> None:
    row = StateRow.from_state_object(
        {
            "entity_id": "light.x",
            "state": "on",
            "attributes": {"brightness": 3},
            "last_changed": "2026-01-01T00:00:00+00:00",
            "last_updated": "2026-01-01T00:00:01+00:00",
            "context": {"id": "01ABC"},
        }
    )
    assert row.last_updated_us - row.last_changed_us == 1_000_000
    assert row.context_id == "01ABC"


def test_statistic_row_from_ha_uses_milliseconds() -> None:
    row = StatisticRow.from_ha(
        "sensor.e", "hour", {"start": 1_000.0, "sum": 2.0, "last_reset": 500}
    )
    assert (row.start_us, row.last_reset_us, row.sum) == (1_000_000, 500_000, 2.0)


def test_windows_cover_range_exactly() -> None:
    parts = list(windows(T0, T0 + timedelta(hours=50), timedelta(days=1)))
    assert parts[0] == (T0, T0 + timedelta(days=1))
    assert parts[-1][1] == T0 + timedelta(hours=50)
    assert len(parts) == 3


async def test_iter_history_batches_entities_and_windows(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    calls: list[dict[str, Any]] = []

    def history(msg: dict[str, Any]) -> dict[str, Any]:
        calls.append(msg)
        start = datetime.fromisoformat(msg["start_time"]).timestamp()
        return {e: [{"s": "1", "a": {}, "lu": start}] for e in msg["entity_ids"]}

    fake.handlers["history/history_during_period"] = history
    ids = [f"sensor.s{i}" for i in range(5)]
    async with HAClient(url, VALID_TOKEN) as ha:
        chunks = [
            c
            async for c in iter_history(
                ha, T0, T0 + timedelta(days=2), ids, window=timedelta(days=1), entity_batch=2
            )
        ]
    assert len(calls) == 6  # 2 windows x 3 entity batches
    assert all(not c["significant_changes_only"] and not c["minimal_response"] for c in calls)
    assert sum(len(c) for c in chunks) == 10


async def test_iter_statistics(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.handlers["recorder/statistics_during_period"] = lambda msg: {
        sid: [{"start": T0.timestamp() * 1000, "mean": 1.0}] for sid in msg["statistic_ids"]
    }
    async with HAClient(url, VALID_TOKEN) as ha:
        rows = [
            r
            async for chunk in iter_statistics(
                ha, T0, T0 + timedelta(days=1), ["sensor.a", "sensor.b"], "5minute"
            )
            for r in chunk
        ]
    assert {r.statistic_id for r in rows} == {"sensor.a", "sensor.b"}
    assert fake.received[0]["period"] == "5minute"
