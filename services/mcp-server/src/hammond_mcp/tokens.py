"""Personal access tokens for the local access mode (ADR 056, ADR 059).

An admin issues a token in the admin GUI (description, role, expiry) and sees it once.
Only its SHA-256 hash is stored, in the schema ``access`` of the audit database:

* the MCP server's role may only call ``access.verify_token(hash)`` (SECURITY DEFINER),
  which returns the token's id, owner and role if it is valid and records the last use;
* the admin GUI's role may list, insert and revoke tokens, never delete them.

Tokens look like ``hmd_<id>_<secret>``: the prefix lets secret scanners recognise them,
the id appears in the audit and the GUI, the secret has 256 bits.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fastmcp.server.auth import TokenVerificationError, TokenVerifier
from fastmcp.server.auth.auth import AccessToken

PREFIX = "hmd_"
IDP = "token"
VERIFY_CACHE_SECONDS = 30


def new_token() -> tuple[str, str]:
    """(token id, full token)."""
    token_id = secrets.token_hex(6)
    return token_id, f"{PREFIX}{token_id}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass(slots=True)
class TokenInfo:
    id: str
    owner: str
    description: str
    role: str
    created_at: datetime
    created_by: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    last_used_at: datetime | None = None

    def active(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


class TokenStoreError(RuntimeError):
    """The token store is unavailable."""


class TokenStore(Protocol):
    def verify(self, digest: bytes) -> TokenInfo | None: ...

    def issue(
        self,
        *,
        owner: str,
        description: str,
        role: str,
        created_by: str | None,
        expires_at: datetime | None,
    ) -> tuple[TokenInfo, str]: ...

    def list(self) -> list[TokenInfo]: ...

    def revoke(self, token_id: str, revoked_by: str | None) -> bool: ...


@dataclass
class MemoryTokenStore:
    """In-memory store (tests, local development)."""

    tokens: dict[bytes, TokenInfo] = field(default_factory=dict)

    def verify(self, digest: bytes) -> TokenInfo | None:
        info = self.tokens.get(digest)
        if info is None or not info.active():
            return None
        info.last_used_at = datetime.now(UTC)
        return replace(info)

    def issue(
        self,
        *,
        owner: str,
        description: str,
        role: str,
        created_by: str | None,
        expires_at: datetime | None,
    ) -> tuple[TokenInfo, str]:
        token_id, token = new_token()
        info = TokenInfo(
            id=token_id,
            owner=owner,
            description=description,
            role=role,
            created_at=datetime.now(UTC),
            created_by=created_by,
            expires_at=expires_at,
        )
        self.tokens[token_hash(token)] = info
        return replace(info), token

    def list(self) -> list[TokenInfo]:
        return sorted(
            (replace(i) for i in self.tokens.values()), key=lambda i: i.created_at, reverse=True
        )

    def revoke(self, token_id: str, revoked_by: str | None) -> bool:
        for info in self.tokens.values():
            if info.id == token_id and info.revoked_at is None:
                info.revoked_at, info.revoked_by = datetime.now(UTC), revoked_by
                return True
        return False


COLUMNS = (
    "id, owner, description, role, created_at, created_by, expires_at, revoked_at, "
    "revoked_by, last_used_at"
)


class PostgresTokenStore:
    """``access.token`` in the audit database (psycopg2, one connection, serialised)."""

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
                        raise TokenStoreError("Token store unavailable") from err
                except psycopg2.Error as err:
                    raise TokenStoreError("Token store error") from err
        return []  # pragma: no cover - loop always returns or raises

    @staticmethod
    def _info(row: tuple[Any, ...]) -> TokenInfo:
        return TokenInfo(*row)

    def verify(self, digest: bytes) -> TokenInfo | None:
        rows = self._run(f"SELECT {COLUMNS} FROM access.verify_token(%s)", (digest,))
        return self._info(rows[0]) if rows else None

    def issue(
        self,
        *,
        owner: str,
        description: str,
        role: str,
        created_by: str | None,
        expires_at: datetime | None,
    ) -> tuple[TokenInfo, str]:
        token_id, token = new_token()
        rows = self._run(
            "INSERT INTO access.token (id, token_hash, owner, description, role, created_by, "
            f"expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}",
            (token_id, token_hash(token), owner, description, role, created_by, expires_at),
        )
        return self._info(rows[0]), token

    def list(self) -> list[TokenInfo]:
        rows = self._run(f"SELECT {COLUMNS} FROM access.token ORDER BY created_at DESC", ())
        return [self._info(r) for r in rows]

    def revoke(self, token_id: str, revoked_by: str | None) -> bool:
        rows = self._run(
            "UPDATE access.token SET revoked_at = now(), revoked_by = %s "
            "WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (revoked_by, token_id),
        )
        return bool(rows)


def expiry(days: int | None, now: datetime | None = None) -> datetime | None:
    return None if not days else (now or datetime.now(UTC)) + timedelta(days=days)


class PersonalTokenVerifier(TokenVerifier):
    """Bearer tokens of the local access mode, checked against the store (cached 30 s)."""

    def __init__(self, store: TokenStore, cache_seconds: int = VERIFY_CACHE_SECONDS) -> None:
        super().__init__()
        self.store = store
        self.cache_seconds = cache_seconds
        self._cache: dict[bytes, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith(PREFIX):
            return None
        digest = token_hash(token)
        now = time.monotonic()
        cached = self._cache.get(digest)
        if cached and cached[0] > now:
            return cached[1]
        try:
            info = await asyncio.to_thread(self.store.verify, digest)
        except TokenStoreError as err:
            raise TokenVerificationError("Token store unavailable") from err
        result = None
        if info is not None:
            result = AccessToken(
                token=token,
                client_id=f"token:{info.id}",
                scopes=[],
                expires_at=int(info.expires_at.timestamp()) if info.expires_at else None,
                subject=f"token:{info.id}",
                claims={
                    "sub": f"token:{info.id}",
                    "name": info.description,
                    "preferred_username": info.owner,
                    "roles": [info.role],
                    "idp": IDP,
                },
            )
        if len(self._cache) > 1000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[digest] = (now + self.cache_seconds, result)
        return result
