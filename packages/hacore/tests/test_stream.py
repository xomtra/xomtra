import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from hacore import bronze
from hacore.history import StateRow, iso_to_us
from hacore.stream import StateStream, StreamSettings
from hacore.testing import VALID_TOKEN, FakeHA

FAST = StreamSettings(flush_interval=0.1, ping_interval=0.5, reconnect_min=0.05, reconnect_max=0.1)


def state_obj(entity_id: str, value: str, when: datetime) -> dict[str, Any]:
    ts = when.isoformat()
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {},
        "last_changed": ts,
        "last_updated": ts,
        "context": {"id": "ctx"},
    }


async def wait_for(check: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.02)


def stored(lake: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str]]:
    return lake.execute(
        "SELECT entity_id, state, source FROM lake.bronze.ha_states ORDER BY last_updated"
    ).fetchall()


async def test_catch_up_live_and_reconnect(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    now = datetime.now(UTC)
    old = now - timedelta(hours=1)
    gap = now - timedelta(minutes=30)
    bronze.write_states(
        lake,
        [StateRow("sensor.a", "1", None, iso_to_us(old.isoformat()), iso_to_us(old.isoformat()))],
        source="ha_live",
    )
    fake.results["get_states"] = [state_obj("sensor.a", "2", gap)]
    fake.results["config/entity_registry/list"] = [{"entity_id": "sensor.a"}]
    # The history API returns the stored state again (overlap) and the missed one.
    fake.handlers["history/history_during_period"] = lambda msg: {
        "sensor.a": [
            {"s": "1", "a": {}, "lu": old.timestamp()},
            {"s": "2", "a": {}, "lu": gap.timestamp()},
        ]
    }

    stream = StateStream(url, VALID_TOKEN, lake.cursor, FAST)
    task = asyncio.create_task(stream.run())
    await wait_for(lambda: len(stored(lake)) == 2)

    await wait_for(lambda: bool(fake.subscriptions))
    await fake.fire("state_changed", {"new_state": state_obj("sensor.a", "3", now)})
    await fake.fire("state_changed", {"new_state": None})  # removed entity: ignored
    await wait_for(lambda: len(stored(lake)) == 3)

    # Home Assistant restarts: the stream reconnects, catches up and continues.
    await fake.drop_connections()
    await wait_for(lambda: bool(fake.subscriptions))
    later = now + timedelta(seconds=1)
    await fake.fire("state_changed", {"new_state": state_obj("sensor.a", "4", later)})
    await wait_for(lambda: len(stored(lake)) == 4)

    stream.stop()
    await asyncio.wait_for(task, 5)
    assert bronze.gaps_since(lake, None) == []  # 1 h gap: covered by the silver lookback
    assert [(s, src) for _, s, src in stored(lake)] == [
        ("1", "ha_live"),
        ("2", "ha_history"),
        ("3", "ha_live"),
        ("4", "ha_live"),
    ]


async def test_no_catch_up_on_empty_lake(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    stream = StateStream(url, VALID_TOKEN, lake.cursor, FAST)
    task = asyncio.create_task(stream.run())
    await wait_for(lambda: bool(fake.subscriptions))
    await fake.fire("state_changed", {"new_state": state_obj("sensor.b", "x", datetime.now(UTC))})
    await wait_for(lambda: len(stored(lake)) == 1)
    stream.stop()
    await asyncio.wait_for(task, 5)
    assert not any(m["type"] == "history/history_during_period" for m in fake.received)


async def test_long_catch_up_records_gap(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    now = datetime.now(UTC)
    old = now - timedelta(hours=5)
    missed = now - timedelta(hours=4)
    bronze.write_states(
        lake,
        [StateRow("sensor.a", "1", None, iso_to_us(old.isoformat()), iso_to_us(old.isoformat()))],
        source="ha_live",
    )
    fake.results["get_states"] = [state_obj("sensor.a", "2", missed)]
    fake.results["config/entity_registry/list"] = [{"entity_id": "sensor.a"}]
    fake.handlers["history/history_during_period"] = lambda msg: {
        "sensor.a": [{"s": "2", "a": {}, "lu": missed.timestamp()}]
    }
    stream = StateStream(url, VALID_TOKEN, lake.cursor, FAST)
    task = asyncio.create_task(stream.run())
    await wait_for(lambda: bool(bronze.gaps_since(lake, None)))
    stream.stop()
    await asyncio.wait_for(task, 5)
    [(start, end, _)] = bronze.gaps_since(lake, None)
    assert start <= old
    assert end >= missed


async def test_labelled_entities_are_not_stored(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    now = datetime.now(UTC)
    old = now - timedelta(minutes=10)
    bronze.write_states(
        lake,
        [StateRow("sensor.a", "1", None, iso_to_us(old.isoformat()), iso_to_us(old.isoformat()))],
        source="ha_live",
    )
    fake.results["get_states"] = []
    fake.results["config/entity_registry/list"] = [
        {"entity_id": "sensor.a"},
        {"entity_id": "sensor.secret", "labels": ["xomtra_no_ingest"]},
    ]
    asked: set[str] = set()

    def history(msg: dict[str, Any]) -> dict[str, Any]:
        asked.update(msg["entity_ids"])
        return {"sensor.a": [{"s": "2", "a": {}, "lu": (old + timedelta(minutes=1)).timestamp()}]}

    fake.handlers["history/history_during_period"] = history
    stream = StateStream(url, VALID_TOKEN, lake.cursor, FAST)
    task = asyncio.create_task(stream.run())
    await wait_for(lambda: len(stored(lake)) == 2)  # catch-up done, labels known
    await fake.fire("state_changed", {"new_state": state_obj("sensor.secret", "x", now)})
    await fake.fire("state_changed", {"new_state": state_obj("sensor.a", "3", now)})
    await wait_for(lambda: len(stored(lake)) == 3)
    stream.stop()
    await asyncio.wait_for(task, 5)
    assert asked == {"sensor.a"}
    assert all(e == "sensor.a" for e, _, _ in stored(lake))


async def test_full_disk_pauses_the_stream(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    space = {"ok": False}
    settings = StreamSettings(
        flush_interval=0.1,
        ping_interval=0.5,
        reconnect_min=0.05,
        reconnect_max=0.1,
        can_write=lambda: space["ok"],
        paused_poll=0.05,
    )
    stream = StateStream(url, VALID_TOKEN, lake.cursor, settings)
    task = asyncio.create_task(stream.run())
    await asyncio.sleep(0.3)
    assert not fake.subscriptions  # paused before connecting

    space["ok"] = True
    await wait_for(lambda: bool(fake.subscriptions))
    await fake.fire("state_changed", {"new_state": state_obj("sensor.c", "1", datetime.now(UTC))})
    await wait_for(lambda: len(stored(lake)) == 1)

    space["ok"] = False
    await fake.fire("state_changed", {"new_state": state_obj("sensor.c", "2", datetime.now(UTC))})
    await wait_for(lambda: not fake.subscriptions)  # disconnected, nothing written
    assert len(stored(lake)) == 1

    stream.stop()
    await asyncio.wait_for(task, 5)
