"""Who may use the Claude connector with a Home Assistant account, and for how long (ADR 081).

* **Access list:** HA administrators (and the owner) always get the admin role. Other HA
  users get no access until an admin of xomtra gives them a role in the admin GUI. Every
  HA user who tries to sign in is recorded, so the admin sees who is waiting.
* **Sessions:** one row per sign-in of the connector, keyed by the id of the Home
  Assistant refresh token of that sign-in (the ``iss`` of HA's access tokens). xomtra
  revokes that HA token right after the sign-in, so no HA token of the user stays in
  xomtra; the session row is what keeps the connector working. It ends
  ``XOMTRA_SESSION_DAYS`` after the sign-in (default 180), when an admin disconnects it in
  the GUI, or when the user is removed, deactivated or loses access in HA.

Both live in the schema ``access`` of the audit database. The MCP server's role may only
call ``access.ha_user_seen()`` and ``access.session_touch()`` (SECURITY DEFINER); the
admin GUI's role lists, sets roles and disconnects (no DELETE: history stays).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

DEFAULT_SESSION_DAYS = 180
# server_info warns this many days before a session ends.
WARN_DAYS = 14
ROLES = ("xomtra.Reader", "xomtra.Admin")


class AccessStoreError(RuntimeError):
    """The access store is unavailable."""


@dataclass(slots=True)
class HAUserAccess:
    ha_user_id: str
    name: str
    role: str | None
    first_seen_at: datetime
    last_seen_at: datetime | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None
    is_admin: bool = False


@dataclass(slots=True)
class SessionInfo:
    id: str
    idp: str
    subject: str
    name: str | None
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    def active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.revoked_at is None and self.expires_at > now


@dataclass(slots=True)
class SessionAccess:
    """A session with the HA user's state as recorded at the last sign-in."""

    session: SessionInfo
    is_admin: bool
    role: str | None


class AccessStore(Protocol):
    # MCP server
    def ha_user_seen(self, ha_user_id: str, name: str, is_admin: bool) -> str | None: ...

    def session_touch(
        self, session_id: str, idp: str, subject: str, name: str | None, days: int
    ) -> SessionInfo: ...

    def session_check(self, session_id: str) -> SessionAccess | None: ...

    # admin GUI
    def ha_users(self) -> list[HAUserAccess]: ...

    def set_ha_user_role(
        self, ha_user_id: str, role: str | None, changed_by: str | None
    ) -> bool: ...

    def sessions(self) -> list[SessionInfo]: ...

    def revoke_session(self, session_id: str, revoked_by: str | None) -> bool: ...


@dataclass
class MemoryAccessStore:
    """In-memory store (tests, local development)."""

    users: dict[str, HAUserAccess] = field(default_factory=dict)
    rows: dict[str, SessionInfo] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def ha_user_seen(self, ha_user_id: str, name: str, is_admin: bool) -> str | None:
        now = datetime.now(UTC)
        with self._lock:
            user = self.users.get(ha_user_id)
            if user is None:
                user = HAUserAccess(ha_user_id, name, None, now)
                self.users[ha_user_id] = user
            user.name, user.last_seen_at, user.is_admin = name, now, is_admin
            return user.role

    def session_touch(
        self, session_id: str, idp: str, subject: str, name: str | None, days: int
    ) -> SessionInfo:
        now = datetime.now(UTC)
        with self._lock:
            row = self.rows.get(session_id)
            if row is None:
                row = SessionInfo(session_id, idp, subject, name, now, now + timedelta(days=days))
                self.rows[session_id] = row
            row.last_used_at = now
            return replace(row)

    def session_check(self, session_id: str) -> SessionAccess | None:
        with self._lock:
            row = self.rows.get(session_id)
            if row is None:
                return None
            row.last_used_at = datetime.now(UTC)
            user = self.users.get(row.subject)
            return SessionAccess(
                replace(row), bool(user and user.is_admin), user.role if user else None
            )

    def ha_users(self) -> list[HAUserAccess]:
        return sorted((replace(u) for u in self.users.values()), key=lambda u: u.name.casefold())

    def set_ha_user_role(self, ha_user_id: str, role: str | None, changed_by: str | None) -> bool:
        user = self.users.get(ha_user_id)
        if user is None:
            return False
        user.role, user.changed_at, user.changed_by = role, datetime.now(UTC), changed_by
        return True

    def sessions(self) -> list[SessionInfo]:
        return sorted(
            (replace(r) for r in self.rows.values()), key=lambda r: r.created_at, reverse=True
        )

    def revoke_session(self, session_id: str, revoked_by: str | None) -> bool:
        row = self.rows.get(session_id)
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at, row.revoked_by = datetime.now(UTC), revoked_by
        return True


SESSION_COLUMNS = (
    "id, idp, subject, name, created_at, expires_at, last_used_at, revoked_at, revoked_by"
)
USER_COLUMNS = (
    "ha_user_id, name, role, first_seen_at, last_seen_at, changed_at, changed_by, is_admin"
)


class PostgresAccessStore:
    """Schema ``access`` of the audit database (psycopg2, one connection, serialised)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None
        self._lock = threading.Lock()

    def _run(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
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
                        raise AccessStoreError("Access store unavailable") from err
                except psycopg2.Error as err:
                    raise AccessStoreError("Access store error") from err
        return []  # pragma: no cover - loop always returns or raises

    def ha_user_seen(self, ha_user_id: str, name: str, is_admin: bool) -> str | None:
        rows = self._run("SELECT access.ha_user_seen(%s, %s, %s)", (ha_user_id, name, is_admin))
        return rows[0][0] if rows else None

    def session_touch(
        self, session_id: str, idp: str, subject: str, name: str | None, days: int
    ) -> SessionInfo:
        rows = self._run(
            f"SELECT {SESSION_COLUMNS} FROM access.session_touch(%s, %s, %s, %s, %s)",
            (session_id, idp, subject, name, days),
        )
        if not rows:
            raise AccessStoreError("Session not recorded")
        return SessionInfo(*rows[0])

    def session_check(self, session_id: str) -> SessionAccess | None:
        rows = self._run(
            f"SELECT {SESSION_COLUMNS}, is_admin, role FROM access.session_check(%s)",
            (session_id,),
        )
        if not rows:
            return None
        *session, is_admin, role = rows[0]
        return SessionAccess(SessionInfo(*session), bool(is_admin), role)

    def ha_users(self) -> list[HAUserAccess]:
        rows = self._run(f"SELECT {USER_COLUMNS} FROM access.ha_user ORDER BY lower(name)", ())
        return [HAUserAccess(*r) for r in rows]

    def set_ha_user_role(self, ha_user_id: str, role: str | None, changed_by: str | None) -> bool:
        rows = self._run(
            "UPDATE access.ha_user SET role = %s, changed_at = now(), changed_by = %s "
            "WHERE ha_user_id = %s RETURNING ha_user_id",
            (role, changed_by, ha_user_id),
        )
        return bool(rows)

    def sessions(self) -> list[SessionInfo]:
        rows = self._run(
            f"SELECT {SESSION_COLUMNS} FROM access.session ORDER BY created_at DESC LIMIT 500", ()
        )
        return [SessionInfo(*r) for r in rows]

    def revoke_session(self, session_id: str, revoked_by: str | None) -> bool:
        rows = self._run(
            "UPDATE access.session SET revoked_at = now(), revoked_by = %s "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (revoked_by, session_id),
        )
        return bool(rows)


def expiry_warning(expires_at: datetime, now: datetime | None = None) -> str | None:
    """Text for the assistant when the connector's sign-in ends soon (ADR 081)."""
    now = now or datetime.now(UTC)
    left = expires_at - now
    if left > timedelta(days=WARN_DAYS):
        return None
    return (
        f"The sign-in of this Claude connector to xomtra ends on {expires_at.date().isoformat()}"
        " (UTC). Tell the user once, briefly: after that date they reconnect the connector "
        "in Claude's settings and sign in to Home Assistant again, from a network where "
        "their browser reaches Home Assistant (at home or over VPN). Nothing is lost."
    )
