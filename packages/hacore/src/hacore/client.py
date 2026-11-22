"""Async client for the Home Assistant WebSocket API.

Protocol reference: https://developers.home-assistant.io/docs/api/websocket

The client keeps one connection with a single background reader task. Commands are
correlated with responses by message id; events of active subscriptions are dispatched
to their callbacks. The client does not reconnect on its own: callers (ingest jobs)
decide how to resume, e.g. with a catch-up from the recorder.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from hacore.errors import (
    HAAuthError,
    HACommandError,
    HAConnectionError,
    HAProtocolError,
)

logger = logging.getLogger(__name__)

type JSON = dict[str, Any]
type EventCallback = Callable[[JSON], Awaitable[None] | None]

DEFAULT_TIMEOUT = 30.0
# Registry and history responses of larger installations easily exceed the websockets
# default (1 MiB).
DEFAULT_MAX_MESSAGE_SIZE = 64 * 1024 * 1024


def websocket_url(base_url: str) -> str:
    """Derive the WebSocket endpoint from a Home Assistant base URL.

    ``https://ha.example:8123`` -> ``wss://ha.example:8123/api/websocket``.
    A URL that already points to ``/api/websocket`` is returned unchanged.
    """
    url = base_url.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = "ws://" + url.removeprefix("http://")
    elif not url.startswith(("ws://", "wss://")):
        raise ValueError(f"Unsupported URL scheme: {base_url!r}")
    if not url.endswith("/api/websocket"):
        url += "/api/websocket"
    return url


class HAClient:
    """Home Assistant WebSocket client.

    Usage::

        async with HAClient(url, token) as ha:
            states = await ha.get_states()
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ) -> None:
        self._url = websocket_url(url)
        self._token = token
        self._timeout = timeout
        self._max_message_size = max_message_size
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, EventCallback] = {}
        self._background: set[asyncio.Task[None]] = set()
        self.ha_version: str | None = None

    # -- lifecycle -------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        """Open the connection and authenticate."""
        try:
            self._ws = await connect(
                self._url,
                max_size=self._max_message_size,
                open_timeout=self._timeout,
            )
        except (OSError, TimeoutError) as err:
            raise HAConnectionError(f"Cannot connect to Home Assistant: {err}") from err

        try:
            async with asyncio.timeout(self._timeout):
                await self._authenticate(self._ws)
        except BaseException:
            await self._ws.close()
            self._ws = None
            raise
        self._reader = asyncio.create_task(self._read_loop(), name="ha-ws-reader")

    async def _authenticate(self, ws: ClientConnection) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != "auth_required":
            raise HAProtocolError(f"Expected auth_required, got {hello.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await ws.recv())
        match reply.get("type"):
            case "auth_ok":
                self.ha_version = reply.get("ha_version")
                logger.info("Authenticated to Home Assistant %s", self.ha_version)
            case "auth_invalid":
                # Do not log the server message verbatim: it could echo request details.
                raise HAAuthError("Home Assistant rejected the access token")
            case other:
                raise HAProtocolError(f"Unexpected auth reply {other!r}")

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._fail_pending(HAConnectionError("Connection closed"))
        self._subscriptions.clear()

    # -- reading ---------------------------------------------------------------------

    async def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                payload = json.loads(raw)
                # HA may coalesce several messages into one JSON array.
                messages = payload if isinstance(payload, list) else [payload]
                for message in messages:
                    self._dispatch(message)
        except ConnectionClosed as err:
            logger.warning("Home Assistant connection closed: %s", err)
        finally:
            self._fail_pending(HAConnectionError("Connection to Home Assistant lost"))

    def _dispatch(self, message: JSON) -> None:
        msg_id = message.get("id")
        match message.get("type"):
            case "result":
                future = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
                if future is None or future.done():
                    return
                if message.get("success"):
                    future.set_result(message.get("result"))
                else:
                    error = message.get("error") or {}
                    future.set_exception(
                        HACommandError(error.get("code", "unknown"), error.get("message", ""))
                    )
            case "event":
                callback = self._subscriptions.get(msg_id) if isinstance(msg_id, int) else None
                if callback is not None:
                    self._run_callback(callback, message.get("event") or {})
            case "pong":
                future = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
                if future is not None and not future.done():
                    future.set_result(None)
            case other:
                logger.debug("Ignoring message of type %r", other)

    def _run_callback(self, callback: EventCallback, event: JSON) -> None:
        try:
            result = callback(event)
        except Exception:
            logger.exception("Event callback failed")
            return
        if inspect.isawaitable(result):
            task = asyncio.ensure_future(result)
            self._background.add(task)
            task.add_done_callback(self._callback_done)

    def _callback_done(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Event callback failed", exc_info=task.exception())

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # -- commands --------------------------------------------------------------------

    async def _send(
        self, message: JSON, *, subscription: EventCallback | None = None
    ) -> tuple[int, asyncio.Future[Any]]:
        if not self.connected or self._ws is None:
            raise HAConnectionError("Not connected to Home Assistant")
        msg_id = next(self._ids)
        if subscription is not None:
            # Register before sending so that no early event is lost.
            self._subscriptions[msg_id] = subscription
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._ws.send(json.dumps({**message, "id": msg_id}))
        except ConnectionClosed as err:
            self._pending.pop(msg_id, None)
            self._subscriptions.pop(msg_id, None)
            raise HAConnectionError("Connection to Home Assistant lost") from err
        return msg_id, future

    async def send_command(self, command_type: str, **payload: Any) -> Any:
        """Send a command and return its ``result`` payload."""
        _, future = await self._send({"type": command_type, **payload})
        async with asyncio.timeout(self._timeout):
            return await future

    async def ping(self) -> None:
        _, future = await self._send({"type": "ping"})
        async with asyncio.timeout(self._timeout):
            await future

    async def subscribe_events(
        self, callback: EventCallback, event_type: str | None = None
    ) -> Callable[[], Awaitable[None]]:
        """Subscribe to events (all, or of one type). Returns an async unsubscribe."""
        payload: JSON = {"type": "subscribe_events"}
        if event_type is not None:
            payload["event_type"] = event_type
        sub_id, future = await self._send(payload, subscription=callback)
        try:
            async with asyncio.timeout(self._timeout):
                await future
        except BaseException:
            self._subscriptions.pop(sub_id, None)
            raise

        async def unsubscribe() -> None:
            self._subscriptions.pop(sub_id, None)
            if self.connected:
                await self.send_command("unsubscribe_events", subscription=sub_id)

        return unsubscribe

    # -- convenience -----------------------------------------------------------------

    async def get_config(self) -> JSON:
        result: JSON = await self.send_command("get_config")
        return result

    async def get_states(self) -> list[JSON]:
        result: list[JSON] = await self.send_command("get_states")
        return result
