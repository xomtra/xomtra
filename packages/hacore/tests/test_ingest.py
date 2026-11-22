from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from hacore import HAClient, ingest
from hacore.testing import VALID_TOKEN, FakeHA

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


def setup_registries(fake: FakeHA) -> None:
    fake.results.update(
        {
            "config/floor_registry/list": [{"floor_id": "ground", "name": "Ground"}],
            "config/area_registry/list": [{"area_id": "kitchen", "name": "Kitchen"}],
            "config/label_registry/list": [],
            "config/device_registry/list": [{"id": "d1", "name": "Thermo", "area_id": "kitchen"}],
            "config/entity_registry/list": [
                {"id": "e1", "entity_id": "sensor.t", "device_id": "d1"}
            ],
            "recorder/list_statistic_ids": [
                {"statistic_id": "sensor.t", "display_unit_of_measurement": "°C"}
            ],
            "get_states": [
                {
                    "entity_id": "sensor.t",
                    "state": "21",
                    "attributes": {"unit_of_measurement": "°C", "state_class": "measurement"},
                    "last_changed": NOW.isoformat(),
                    "last_updated": NOW.isoformat(),
                }
            ],
        }
    )


async def test_sync_registries(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    setup_registries(fake)
    async with HAClient(url, VALID_TOKEN) as ha:
        first = await ingest.sync_registries(ha, lake)
        second = await ingest.sync_registries(ha, lake)
    assert first.written == {
        "floors": 1,
        "areas": 1,
        "labels": 0,
        "devices": 1,
        "entities": 1,
        "statistics_meta": 1,
        "entity_attributes": 1,
    }
    assert set(second.written.values()) == {0}
    row = lake.execute(
        "SELECT payload->>'state_class' FROM lake.bronze.ha_registry "
        "WHERE registry = 'entity_attributes'"
    ).fetchone()
    assert row == ("measurement",)


async def test_sync_statistics_resumes_from_watermark(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    setup_registries(fake)
    requests: list[dict[str, Any]] = []

    def stats(msg: dict[str, Any]) -> dict[str, Any]:
        requests.append(msg)
        start = datetime.fromisoformat(msg["start_time"])
        step = timedelta(minutes=5) if msg["period"] == "5minute" else timedelta(hours=1)
        first = start.replace(minute=0, second=0, microsecond=0) + step
        return {"sensor.t": [{"start": (first + i * step).timestamp() * 1000} for i in range(3)]}

    fake.handlers["recorder/statistics_during_period"] = stats
    async with HAClient(url, VALID_TOKEN) as ha:
        first = await ingest.sync_statistics(ha, lake, periods=("hour",), end=NOW)
        requests.clear()
        await ingest.sync_statistics(ha, lake, periods=("hour",), end=NOW)
    assert first.written["hour"] > 0
    # The second run starts shortly before the newest stored period, not a year back.
    assert datetime.fromisoformat(requests[0]["start_time"]) > NOW - timedelta(days=30)


async def test_backfill_uses_states_and_registry_ids(
    fake_ha: tuple[FakeHA, str], lake: duckdb.DuckDBPyConnection
) -> None:
    fake, url = fake_ha
    setup_registries(fake)
    fake.results["config/entity_registry/list"].append({"id": "e2", "entity_id": "sensor.gone"})
    seen: set[str] = set()

    def history(msg: dict[str, Any]) -> dict[str, Any]:
        seen.update(msg["entity_ids"])
        return {"sensor.t": [{"s": "1", "a": {}, "lu": NOW.timestamp()}]}

    fake.handlers["history/history_during_period"] = history
    async with HAClient(url, VALID_TOKEN) as ha:
        result = await ingest.backfill_history(ha, lake, NOW - timedelta(days=2), NOW)
    assert seen == {"sensor.t", "sensor.gone"}
    assert result.written == {"states": 1}
