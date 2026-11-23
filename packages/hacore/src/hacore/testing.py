"""In-process fake of the Home Assistant WebSocket API, for tests only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

VALID_TOKEN = "test-token"  # noqa: S105

type Handler = Callable[[dict[str, Any]], Any]


class FakeHA:
    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        # Computed results: command type -> function of the request message.
        self.handlers: dict[str, Handler] = {}
        self.errors: dict[str, tuple[str, str]] = {}
        self.received: list[dict[str, Any]] = []
        self.connections: list[ServerConnection] = []
        self.subscriptions: dict[int, str | None] = {}
        self.coalesce = False

    async def handler(self, ws: ServerConnection) -> None:
        self.connections.append(ws)
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.11.0"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != VALID_TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid password"}))
            await ws.close()
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.11.0"}))
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            await ws.send(json.dumps(self.reply(msg)))

    def reply(self, msg: dict[str, Any]) -> Any:
        msg_id, kind = msg["id"], msg["type"]
        if kind == "ping":
            return {"id": msg_id, "type": "pong"}
        if kind == "subscribe_events":
            self.subscriptions[msg_id] = msg.get("event_type")
        if kind == "unsubscribe_events":
            self.subscriptions.pop(msg["subscription"], None)
        if kind in self.errors:
            code, message = self.errors[kind]
            result = {
                "id": msg_id,
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }
        else:
            result = {
                "id": msg_id,
                "type": "result",
                "success": True,
                "result": self.handlers[kind](msg)
                if kind in self.handlers
                else self.results.get(kind),
            }
        return [result] if self.coalesce else result

    async def drop_connections(self) -> None:
        """Simulate a Home Assistant restart: close all client connections."""
        for ws in self.connections:
            await ws.close()
        self.connections.clear()
        self.subscriptions.clear()

    async def fire(self, event_type: str, data: dict[str, Any]) -> None:
        for sub_id, wanted in list(self.subscriptions.items()):
            if wanted in (None, event_type):
                event = {"event_type": event_type, "data": data, "origin": "LOCAL"}
                for ws in self.connections:
                    await ws.send(json.dumps({"id": sub_id, "type": "event", "event": event}))


@asynccontextmanager
async def running_fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    fake = FakeHA()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield fake, f"http://127.0.0.1:{port}"
