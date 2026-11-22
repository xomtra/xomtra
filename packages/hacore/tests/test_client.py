import asyncio
from typing import Any

import pytest

from hacore import (
    HAAuthError,
    HAClient,
    HACommandError,
    HAConnectionError,
    websocket_url,
)
from hacore.testing import VALID_TOKEN, FakeHA


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://ha.local:8123", "ws://ha.local:8123/api/websocket"),
        ("https://ha.example/", "wss://ha.example/api/websocket"),
        ("wss://ha.example/api/websocket", "wss://ha.example/api/websocket"),
    ],
)
def test_websocket_url(base: str, expected: str) -> None:
    assert websocket_url(base) == expected


def test_websocket_url_rejects_other_schemes() -> None:
    with pytest.raises(ValueError, match="scheme"):
        websocket_url("ftp://ha")


async def test_auth_and_command(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["get_states"] = [{"entity_id": "sensor.a", "state": "1"}]
    async with HAClient(url, VALID_TOKEN) as ha:
        assert ha.ha_version == "2026.11.0"
        assert await ha.get_states() == [{"entity_id": "sensor.a", "state": "1"}]
        await ha.ping()
    assert [m["type"] for m in fake.received] == ["get_states", "ping"]


async def test_invalid_token(fake_ha: tuple[FakeHA, str]) -> None:
    _, url = fake_ha
    with pytest.raises(HAAuthError):
        async with HAClient(url, "wrong"):
            pass


async def test_connection_refused() -> None:
    with pytest.raises(HAConnectionError):
        await HAClient("http://127.0.0.1:9", VALID_TOKEN, timeout=2).connect()


async def test_command_error(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.errors["config/entity_registry/list"] = ("unauthorized", "Unauthorized")
    async with HAClient(url, VALID_TOKEN) as ha:
        with pytest.raises(HACommandError) as exc:
            await ha.send_command("config/entity_registry/list")
    assert exc.value.code == "unauthorized"


async def test_coalesced_messages(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.coalesce = True
    fake.results["get_config"] = {"version": "2026.11.0"}
    async with HAClient(url, VALID_TOKEN) as ha:
        assert await ha.get_config() == {"version": "2026.11.0"}


async def test_concurrent_commands(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["get_config"] = {"a": 1}
    async with HAClient(url, VALID_TOKEN) as ha:
        results = await asyncio.gather(*(ha.get_config() for _ in range(20)))
    assert results == [{"a": 1}] * 20


async def test_subscribe_and_unsubscribe(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    received: list[dict[str, Any]] = []
    got_event = asyncio.Event()

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)
        got_event.set()

    async with HAClient(url, VALID_TOKEN) as ha:
        unsubscribe = await ha.subscribe_events(on_event, "state_changed")
        await fake.fire("state_changed", {"entity_id": "sensor.a"})
        await asyncio.wait_for(got_event.wait(), 5)
        await unsubscribe()
        assert fake.subscriptions == {}

    assert received[0]["data"] == {"entity_id": "sensor.a"}


async def test_pending_commands_fail_on_disconnect(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    async with HAClient(url, VALID_TOKEN) as ha:
        await fake.connections[0].close()
        await asyncio.sleep(0.1)
        assert not ha.connected
        with pytest.raises(HAConnectionError):
            await ha.get_states()
