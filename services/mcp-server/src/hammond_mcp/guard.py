"""Policy enforcement and audit for every MCP request (ADR 038, ADR 040).

``tools/list`` shows only the tools the caller's roles allow. ``tools/call`` checks the
tool, makes the caller's :class:`~hammond_mcp.policy.Access` available to the data
layer, and writes one audit record per call, allowed or not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult

from hammond_mcp.audit import AuditError, AuditRecord, AuditSink, clip_arguments
from hammond_mcp.data import KNOWN_VIEWS, QueryFailed
from hammond_mcp.identity import Identity
from hammond_mcp.policy import (
    Access,
    AccessDenied,
    Policy,
    reset_current_access,
    set_current_access,
)
from hammond_mcp.sqlguard import SQLRejected

logger = logging.getLogger(__name__)


class PolicyMiddleware(Middleware):
    def __init__(
        self,
        policy: Policy,
        audit: AuditSink,
        identity: Callable[[], Identity],
    ) -> None:
        self.policy = policy
        self.audit = audit
        self.identity = identity

    def _access(self) -> tuple[Identity, Access]:
        ident = self.identity()
        return ident, self.policy.access_for(ident, KNOWN_VIEWS)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        _, access = self._access()
        return [t for t in tools if access.can_call(t.name)]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        ident, access = self._access()
        name = context.message.name
        record = AuditRecord(
            tool=name,
            decision="allowed",
            outcome="ok",
            arguments=clip_arguments(context.message.arguments),
            subject=ident.subject,
            username=ident.username,
            roles=list(ident.roles),
            client_id=_client_id(),
            idp=ident.idp,
        )
        started = time.monotonic()

        async def finish() -> None:
            record.duration_ms = round((time.monotonic() - started) * 1000)
            try:
                await self.audit.write(record)
            except AuditError as err:
                raise ToolError("Audit is unavailable, the call was refused") from err

        if not access.can_call(name):
            record.decision, record.outcome = "denied", "denied"
            record.error = "tool not permitted"
            await finish()
            raise ToolError(f"Access denied: your roles do not allow {name}")

        token = set_current_access(access)
        try:
            result = await call_next(context)
        except Exception as err:
            # FastMCP wraps exceptions raised by a tool into ToolError (cause kept).
            cause = err.__cause__ if isinstance(err, ToolError) and err.__cause__ else err
            if isinstance(cause, AccessDenied):
                record.decision, record.outcome, record.error = "denied", "denied", str(cause)
                await finish()
                raise ToolError(f"Access denied: {cause}") from cause
            record.outcome = "error"
            if isinstance(cause, (SQLRejected, QueryFailed, ValueError, ToolError)):
                record.error = str(cause)[:500]
                await finish()
                raise ToolError(str(cause)) from cause
            record.error = type(cause).__name__
            await finish()
            raise
        finally:
            reset_current_access(token)
        record.row_count = _row_count(result)
        await finish()
        return result


def _client_id() -> str | None:
    token = get_access_token()
    return token.client_id if token is not None else None


def _row_count(result: ToolResult) -> int | None:
    content: Any = result.structured_content
    if isinstance(content, dict):
        inner = content.get("result", content)
        if isinstance(inner, dict) and isinstance(inner.get("row_count"), int):
            return int(inner["row_count"])
    return None
