"""What HAmmond relies on in the Home Assistant WebSocket API, as a non-admin user."""

import asyncio
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from hacore import HAClient, HACommandError, bronze, fetch_registries, ingest
from hacore.history import StateRow, history_during_period, statistics_during_period
from hacore.onboarding import OnboardedTokens
from hacore.recorder import import_recorder_db

SETPOINT = "input_number.hammond_test_setpoint"
SENSOR = "sensor.test_temperature"


async def set_setpoint(ha_url: str, admin_token: str, value: float) -> None:
    async with HAClient(ha_url, admin_token) as admin:
        await admin.send_command(
            "call_service",
            domain="input_number",
            service="set_value",
            service_data={"value": value},
            target={"entity_id": SETPOINT},
        )


async def test_registries_readable_by_non_admin(ha_url: str, tokens: OnboardedTokens) -> None:
    async with HAClient(ha_url, tokens.user_token) as ha:
        snapshot = await fetch_registries(ha)
        states = await ha.get_states()
    assert SENSOR in snapshot.entities
    assert snapshot.entities[SENSOR].platform == "template"
    assert snapshot.areas  # onboarding creates default areas
    assert {s["entity_id"] for s in states} >= {SENSOR, SETPOINT}


async def test_admin_only_commands_are_refused(ha_url: str, tokens: OnboardedTokens) -> None:
    # The ingest token must not be able to change anything.
    async with HAClient(ha_url, tokens.user_token) as ha:
        with pytest.raises(HACommandError):
            await ha.send_command("config/area_registry/create", name="Nope")


async def test_live_event_and_history_share_the_key(ha_url: str, tokens: OnboardedTokens) -> None:
    received: list[StateRow] = []
    async with HAClient(ha_url, tokens.user_token) as ha:

        def on_event(event: dict[str, Any]) -> None:
            new = event["data"].get("new_state")
            if new and new["entity_id"] == SETPOINT:
                received.append(StateRow.from_state_object(new))

        await ha.subscribe_events(on_event, "state_changed")
        start = datetime.now(UTC) - timedelta(seconds=5)
        current = next(s for s in await ha.get_states() if s["entity_id"] == SETPOINT)
        value = 12.3 if current["state"] != "12.3" else 13.3  # must be a real change
        await set_setpoint(ha_url, tokens.admin_token, value)
        async with asyncio.timeout(10):
            while not received:
                await asyncio.sleep(0.1)
        event_row = received[0]
        history: list[StateRow] = []
        async with asyncio.timeout(15):
            while not any(r.last_updated_us == event_row.last_updated_us for r in history):
                await asyncio.sleep(0.5)  # recorder commits asynchronously
                history = await history_during_period(
                    ha, start, datetime.now(UTC) + timedelta(seconds=1), [SETPOINT]
                )
    match = next(r for r in history if r.last_updated_us == event_row.last_updated_us)
    assert match.state == event_row.state == str(value)
    # The recorder drops "unrecorded" attributes of some integrations (input_number:
    # min/max/step/mode), so history carries a subset of what the live event has.
    assert match.attributes is not None
    assert event_row.attributes is not None
    assert match.attributes.items() <= event_row.attributes.items()


async def test_statistics_api(ha_url: str, tokens: OnboardedTokens) -> None:
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    async with HAClient(ha_url, tokens.admin_token) as admin:
        await admin.send_command(
            "recorder/import_statistics",
            metadata={
                "has_mean": True,
                "has_sum": False,
                "name": "Imported",
                "source": "recorder",
                "statistic_id": SENSOR,
                "unit_of_measurement": "°C",
                "mean_type": 1,
            },
            stats=[
                {
                    "start": (hour + timedelta(hours=i)).isoformat(),
                    "mean": 20.0 + i,
                    "min": 19.0,
                    "max": 25.0,
                }
                for i in range(2)
            ],
        )
    async with HAClient(ha_url, tokens.user_token) as ha:
        rows: list[Any] = []
        async with asyncio.timeout(20):
            while len(rows) < 2:
                await asyncio.sleep(0.5)
                rows = await statistics_during_period(
                    ha, hour, hour + timedelta(hours=2), [SENSOR], "hour"
                )
        meta = await ha.send_command("recorder/list_statistic_ids")
        await statistics_during_period(ha, hour, hour + timedelta(hours=1), [SENSOR], "5minute")
    assert [r.mean for r in rows] == [20.0, 21.0]
    assert rows[0].start_us == round(hour.timestamp() * 1_000_000)
    ids = {m["statistic_id"]: m for m in meta}
    assert ids[SENSOR]["has_mean"] is True


async def test_ingest_into_bronze(
    ha_url: str, tokens: OnboardedTokens, lake: duckdb.DuckDBPyConnection
) -> None:
    now = datetime.now(UTC)
    async with HAClient(ha_url, tokens.user_token) as ha:
        registries = await ingest.sync_registries(ha, lake)
        await ingest.sync_statistics(ha, lake)
        first = await ingest.backfill_history(ha, lake, now - timedelta(hours=1), now)
        again = await ingest.backfill_history(ha, lake, now - timedelta(hours=1), now)
    assert registries.written["entities"] > 0
    assert first.written["states"] > 0
    assert again.written.get("states", 0) == 0


def test_recorder_database_import(
    ha_url: str, tokens: OnboardedTokens, lake: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    config_dir = os.environ.get("HAMMOND_CONTRACT_HA_CONFIG")
    if not config_dir:
        pytest.skip("HAMMOND_CONTRACT_HA_CONFIG not set")
    for suffix in ("", "-wal", "-shm"):
        src = Path(config_dir) / f"home-assistant_v2.db{suffix}"
        if src.exists():
            shutil.copy(src, tmp_path / src.name)
    now = datetime.now(UTC)

    async def backfill() -> int:
        async with HAClient(ha_url, tokens.user_token) as ha:
            result = await ingest.backfill_history(ha, lake, now - timedelta(days=1), now)
            return result.written.get("states", 0)

    asyncio.run(backfill())
    result = import_recorder_db(tmp_path / "home-assistant_v2.db", lake, end=now)
    assert result.states_read > 0
    # Everything the history API returned is recognised as the same rows.
    duplicates = lake.execute(
        f"SELECT count(*) FROM (SELECT entity_id, last_updated FROM {bronze.table(bronze.STATES)} "
        "GROUP BY ALL HAVING count(*) > 1)"
    ).fetchone()
    assert duplicates == (0,)
