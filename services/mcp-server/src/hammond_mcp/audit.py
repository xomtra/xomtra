"""Audit of every MCP tool call (ADR 040).

Each call, allowed or denied, becomes one record: who (Entra object id, username,
roles, OAuth client), what (tool and arguments), the policy decision, the outcome, the
number of rows returned and the duration. Records go to

* the log as one JSON line (``hammond.audit`` logger, collected with ``docker logs``;
  exported to Wazuh in phase 2), and
* the Postgres table ``audit.tool_call`` (database ``HAMMOND_AUDIT_PG_DB``), where the
  MCP server's role may only INSERT.

Writing is fail-closed: if the record cannot be stored, the caller gets an error
instead of the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger("hammond.audit")

MAX_ARGUMENT_CHARS = 8_000

Decision = Literal["allowed", "denied"]
Outcome = Literal["ok", "denied", "error"]


@dataclass(slots=True)
class AuditRecord:
    tool: str
    decision: Decision
    outcome: Outcome
    arguments: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None
    username: str | None = None
    roles: list[str] = field(default_factory=list)
    client_id: str | None = None
    error: str | None = None
    row_count: int | None = None
    duration_ms: int | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


def clip_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Arguments as stored: long strings shortened, the whole record bounded."""
    out: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > MAX_ARGUMENT_CHARS:
            out[str(key)] = value[:MAX_ARGUMENT_CHARS] + "…"
        else:
            out[str(key)] = value
    text = json.dumps(out, default=str)
    if len(text) > 2 * MAX_ARGUMENT_CHARS:
        return {"_truncated": text[: 2 * MAX_ARGUMENT_CHARS]}
    return out


class AuditError(RuntimeError):
    """The audit record could not be stored."""


class AuditSink(Protocol):
    async def write(self, record: AuditRecord) -> None: ...


class LogAuditSink:
    """JSON line per call; always on, also in front of the database sink."""

    async def write(self, record: AuditRecord) -> None:
        data = asdict(record)
        data["ts"] = record.ts.isoformat()
        logger.info(json.dumps(data, default=str, ensure_ascii=False))


class MemoryAuditSink(LogAuditSink):
    """Keeps records in memory (tests, local development)."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        await super().write(record)
        self.records.append(record)


INSERT_SQL = """
INSERT INTO audit.tool_call
  (ts, subject, username, roles, client_id, tool, arguments, decision, outcome, error,
   row_count, duration_ms)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
"""


class PostgresAuditSink(LogAuditSink):
    """INSERT-only writer to ``audit.tool_call`` (psycopg2, one connection, serialised)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None
        self._lock = threading.Lock()

    def _insert(self, record: AuditRecord) -> None:
        import psycopg2  # noqa: PLC0415 - only needed when the database sink is used

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = psycopg2.connect(self._dsn, connect_timeout=5)
                        self._conn.autocommit = True
                    with self._conn.cursor() as cur:
                        cur.execute(
                            INSERT_SQL,
                            (
                                record.ts,
                                record.subject,
                                record.username,
                                record.roles,
                                record.client_id,
                                record.tool,
                                json.dumps(record.arguments, default=str),
                                record.decision,
                                record.outcome,
                                record.error,
                                record.row_count,
                                record.duration_ms,
                            ),
                        )
                except psycopg2.Error as err:
                    self._conn = None
                    if attempt == 2:
                        raise AuditError("Audit database unavailable") from err
                else:
                    return

    async def write(self, record: AuditRecord) -> None:
        await super().write(record)
        try:
            await asyncio.to_thread(self._insert, record)
        except AuditError:
            logger.exception("Audit record not stored")
            raise
