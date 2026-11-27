"""Stores of the answer guidelines sets (ADR 090), schema ``guidance`` of the audit DB.

The admin GUI lists, writes and activates custom sets (:class:`PostgresAnswerSetStore`,
admin role: SELECT and INSERT, deleting only marks a set). The MCP server only reads the
active set through ``guidance.active_set()`` (:class:`PostgresActiveSource`).
Factory sets are not stored: they live in :mod:`xomtra_mcp.guidelines`.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from xomtra_mcp.guidelines import FACTORY_BY_ID, AnswerSet


class AnswerSetStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredSet:
    """A custom set with its latest version."""

    set: AnswerSet
    changed_at: datetime
    changed_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Version:
    id: int
    set_id: str
    name: str
    description: str
    text: str
    changed_at: datetime
    changed_by: str | None


@dataclass(frozen=True, slots=True)
class Activation:
    set_id: str
    changed_at: datetime
    changed_by: str | None


def new_set_id() -> str:
    return f"custom:{secrets.token_hex(6)}"


def resolve(set_id: str, name: str | None, text: str | None) -> AnswerSet | None:
    """The set of a row from ``guidance.active_set()`` (None: unknown factory id)."""
    if set_id in FACTORY_BY_ID:
        return FACTORY_BY_ID[set_id]
    if set_id.startswith("factory:") or name is None:
        return None
    return AnswerSet(set_id, name, text or "")


class AnswerSetStore(Protocol):
    def custom(self) -> list[StoredSet]: ...
    def get(self, set_id: str) -> StoredSet | None: ...
    def create(
        self,
        name: str,
        description: str,
        text: str,
        *,
        by: str | None,
        based_on: str | None = None,
        based_on_version: int | None = None,
    ) -> str: ...
    def update(
        self, set_id: str, name: str, description: str, text: str, *, by: str | None
    ) -> bool: ...
    def delete(self, set_id: str, by: str | None) -> bool: ...
    def versions(self, set_id: str, limit: int = 50) -> list[Version]: ...
    def active(self) -> Activation | None: ...
    def activate(self, set_id: str, by: str | None) -> None: ...
    def activations(self, limit: int = 20) -> list[Activation]: ...


@dataclass
class _Row:
    id: str
    based_on: str | None
    based_on_version: int | None
    created_at: datetime
    created_by: str | None
    deleted_at: datetime | None = None
    versions: list[Version] = field(default_factory=list)


class MemoryAnswerSetStore:
    """In memory, for tests and development without the audit database."""

    def __init__(self) -> None:
        self._sets: dict[str, _Row] = {}
        self._activations: list[Activation] = []
        self._next = 1

    def _version(self, row: _Row, name: str, description: str, text: str, by: str | None) -> None:
        now = datetime.now(UTC)
        row.versions.append(Version(self._next, row.id, name, description, text, now, by))
        self._next += 1

    def _stored(self, row: _Row) -> StoredSet:
        v = row.versions[-1]
        answer_set = AnswerSet(
            row.id,
            v.name,
            v.text,
            v.description,
            based_on=row.based_on,
            based_on_version=row.based_on_version,
        )
        return StoredSet(answer_set, v.changed_at, v.changed_by, row.created_at)

    def custom(self) -> list[StoredSet]:
        rows = [r for r in self._sets.values() if r.deleted_at is None]
        return sorted((self._stored(r) for r in rows), key=lambda s: s.set.name.lower())

    def get(self, set_id: str) -> StoredSet | None:
        row = self._sets.get(set_id)
        return self._stored(row) if row and row.deleted_at is None else None

    def create(
        self,
        name: str,
        description: str,
        text: str,
        *,
        by: str | None,
        based_on: str | None = None,
        based_on_version: int | None = None,
    ) -> str:
        row = _Row(new_set_id(), based_on, based_on_version, datetime.now(UTC), by)
        self._sets[row.id] = row
        self._version(row, name, description, text, by)
        return row.id

    def update(
        self, set_id: str, name: str, description: str, text: str, *, by: str | None
    ) -> bool:
        row = self._sets.get(set_id)
        if row is None or row.deleted_at is not None:
            return False
        self._version(row, name, description, text, by)
        return True

    def delete(self, set_id: str, by: str | None) -> bool:
        row = self._sets.get(set_id)
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.now(UTC)
        return True

    def versions(self, set_id: str, limit: int = 50) -> list[Version]:
        row = self._sets.get(set_id)
        return list(reversed(row.versions))[:limit] if row else []

    def active(self) -> Activation | None:
        return self._activations[-1] if self._activations else None

    def activate(self, set_id: str, by: str | None) -> None:
        self._activations.append(Activation(set_id, datetime.now(UTC), by))

    def activations(self, limit: int = 20) -> list[Activation]:
        return list(reversed(self._activations))[:limit]

    def active_set(self) -> AnswerSet | None:
        """What ``guidance.active_set()`` returns, for the MCP server in tests."""
        act = self.active()
        if act is None:
            return None
        stored = self.get(act.set_id)
        if stored is not None:
            return stored.set
        return resolve(act.set_id, None, None)


class _Postgres:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None
        self._lock = threading.Lock()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        import psycopg2  # noqa: PLC0415 - only needed with the database store

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = psycopg2.connect(self._dsn, connect_timeout=5)
                        self._conn.autocommit = True
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        return list(cur.fetchall()) if cur.description else []
                except psycopg2.OperationalError as err:
                    self._conn = None
                    if attempt == 2:
                        raise AnswerSetStoreError("Answer sets store unavailable") from err
                except psycopg2.Error as err:
                    raise AnswerSetStoreError("Answer sets store error") from err
        return []  # pragma: no cover - loop always returns or raises


_LATEST = (
    "SELECT s.id, v.name, v.description, v.body, s.based_on, s.based_on_version, "
    "v.changed_at, v.changed_by, s.created_at FROM guidance.answer_set s "
    "JOIN LATERAL (SELECT * FROM guidance.answer_set_version x WHERE x.set_id = s.id "
    "ORDER BY x.id DESC LIMIT 1) v ON true WHERE s.deleted_at IS NULL"
)


def _stored(r: tuple[Any, ...]) -> StoredSet:
    answer_set = AnswerSet(r[0], r[1], r[3], r[2], based_on=r[4], based_on_version=r[5])
    return StoredSet(answer_set, r[6], r[7], r[8])


class PostgresAnswerSetStore(_Postgres):
    """The admin GUI's view of ``guidance`` (admin role)."""

    def custom(self) -> list[StoredSet]:
        return [_stored(r) for r in self._run(f"{_LATEST} ORDER BY lower(v.name)")]

    def get(self, set_id: str) -> StoredSet | None:
        rows = self._run(f"{_LATEST} AND s.id = %s", (set_id,))
        return _stored(rows[0]) if rows else None

    def create(
        self,
        name: str,
        description: str,
        text: str,
        *,
        by: str | None,
        based_on: str | None = None,
        based_on_version: int | None = None,
    ) -> str:
        set_id = new_set_id()
        self._run(
            "WITH s AS (INSERT INTO guidance.answer_set (id, based_on, based_on_version, "
            "created_by) VALUES (%s, %s, %s, %s) RETURNING id) "
            "INSERT INTO guidance.answer_set_version (set_id, name, description, body, "
            "changed_by) SELECT id, %s, %s, %s, %s FROM s",
            (set_id, based_on, based_on_version, by, name, description, text, by),
        )
        return set_id

    def update(
        self, set_id: str, name: str, description: str, text: str, *, by: str | None
    ) -> bool:
        rows = self._run(
            "INSERT INTO guidance.answer_set_version (set_id, name, description, body, "
            "changed_by) SELECT id, %s, %s, %s, %s FROM guidance.answer_set "
            "WHERE id = %s AND deleted_at IS NULL RETURNING id",
            (name, description, text, by, set_id),
        )
        return bool(rows)

    def delete(self, set_id: str, by: str | None) -> bool:
        rows = self._run(
            "UPDATE guidance.answer_set SET deleted_at = now(), deleted_by = %s "
            "WHERE id = %s AND deleted_at IS NULL RETURNING id",
            (by, set_id),
        )
        return bool(rows)

    def versions(self, set_id: str, limit: int = 50) -> list[Version]:
        rows = self._run(
            "SELECT id, set_id, name, description, body, changed_at, changed_by "
            "FROM guidance.answer_set_version WHERE set_id = %s ORDER BY id DESC LIMIT %s",
            (set_id, limit),
        )
        return [Version(*r) for r in rows]

    def active(self) -> Activation | None:
        rows = self.activations(1)
        return rows[0] if rows else None

    def activate(self, set_id: str, by: str | None) -> None:
        self._run(
            "INSERT INTO guidance.active_change (set_id, changed_by) VALUES (%s, %s)",
            (set_id, by),
        )

    def activations(self, limit: int = 20) -> list[Activation]:
        rows = self._run(
            "SELECT set_id, changed_at, changed_by FROM guidance.active_change "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        return [Activation(*r) for r in rows]


class PostgresActiveSource(_Postgres):
    """The MCP server's only access: the active set (MCP role, EXECUTE on one function)."""

    def __call__(self) -> AnswerSet | None:
        rows = self._run("SELECT set_id, name, body FROM guidance.active_set()")
        return resolve(*rows[0]) if rows else None
