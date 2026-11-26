"""Connection to Home Assistant: from the environment, or stored by the admin GUI.

Compose edition (ADR 077): the URL and the long-lived token of Home Assistant are
entered in the admin GUI after the first start, not in ``.env``. The admin GUI appends
them to ``setup.ha_connection_change`` in the audit database, the token encrypted with
``XOMTRA_SETTINGS_KEY`` from the secrets volume (Fernet: AES-128-CBC + HMAC-SHA256).
The MCP server and the ingest may only call ``setup.ha_connection()``, which returns
the latest row.

``XOMTRA_HA_URL`` and ``XOMTRA_HA_TOKEN`` in the environment still win (the add-on, a
non-interactive install with ``init-env.sh``, installations before ADR 077).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

CURRENT_SQL = "SELECT url, token_enc, changed_at, changed_by FROM setup.ha_connection()"
INSERT_SQL = (
    "INSERT INTO setup.ha_connection_change (url, token_enc, ha_version, changed_by) "
    "VALUES (%s, %s, %s, %s)"
)


class StoreError(Exception):
    """The stored connection cannot be read or written."""


@dataclass(frozen=True, slots=True)
class HAConnection:
    url: str
    token: str
    source: Literal["env", "stored"]
    changed_at: datetime | None = None
    changed_by: str | None = None


def _fernet(key: str) -> Any:
    from cryptography.fernet import Fernet  # noqa: PLC0415 - only with a stored token

    if len(key) < 32:
        raise StoreError("XOMTRA_SETTINGS_KEY must have at least 32 characters")
    raw = hashlib.sha256(b"xomtra-settings:" + key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_token(key: str, token: str) -> str:
    return str(_fernet(key).encrypt(token.encode()).decode())


def decrypt_token(key: str, blob: str) -> str:
    from cryptography.fernet import InvalidToken  # noqa: PLC0415

    try:
        return str(_fernet(key).decrypt(blob.encode()).decode())
    except InvalidToken as err:
        raise StoreError(
            "The stored Home Assistant token cannot be decrypted (XOMTRA_SETTINGS_KEY "
            "changed?); enter it again in the admin GUI."
        ) from err


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("The Home Assistant URL must start with http:// or https://")
    return url


def load_stored(dsn: str, key: str) -> HAConnection | None:
    """The stored connection, None if there is none yet; StoreError if unreadable."""
    import psycopg2  # noqa: PLC0415 - only needed with a database

    try:
        with closing(psycopg2.connect(dsn, connect_timeout=5)) as conn, conn, conn.cursor() as cur:
            cur.execute(CURRENT_SQL)
            row = cur.fetchone()
    except psycopg2.Error as err:
        raise StoreError(f"Stored Home Assistant connection not read: {err}") from err
    if row is None:
        return None
    url, token_enc, changed_at, changed_by = row
    return HAConnection(
        str(url), decrypt_token(key, str(token_enc)), "stored", changed_at, changed_by
    )


def save(dsn: str, key: str, url: str, token: str, *, ha_version: str | None, by: str) -> None:
    import psycopg2  # noqa: PLC0415

    blob = encrypt_token(key, token)
    try:
        with closing(psycopg2.connect(dsn, connect_timeout=5)) as conn, conn, conn.cursor() as cur:
            cur.execute(INSERT_SQL, (normalize_url(url), blob, ha_version, by))
    except psycopg2.Error as err:
        raise StoreError(f"Home Assistant connection not saved: {err}") from err


class Resolver:
    """The current connection: environment first, else the store (cached briefly).

    ``dsn`` and ``key`` may be None: then only the environment counts.
    """

    def __init__(
        self,
        env_url: str | None,
        env_token: str | None,
        dsn: str | None = None,
        key: str | None = None,
        *,
        ttl: float = 30.0,
    ) -> None:
        self._env = None
        if env_url and env_token:
            self._env = HAConnection(env_url.rstrip("/"), env_token, "env")
        self._dsn, self._key, self._ttl = dsn, key, ttl
        self._cached: HAConnection | None = None
        self._at = float("-inf")
        self._lock = threading.Lock()

    @property
    def from_env(self) -> bool:
        return self._env is not None

    @property
    def storable(self) -> bool:
        """True if the admin GUI can store a connection here."""
        return self._env is None and bool(self._dsn and self._key)

    def invalidate(self) -> None:
        with self._lock:
            self._at = float("-inf")

    def get(self) -> HAConnection | None:
        if self._env is not None:
            return self._env
        if not (self._dsn and self._key):
            return None
        with self._lock:
            if time.monotonic() - self._at < self._ttl:
                return self._cached
        try:
            found = load_stored(self._dsn, self._key)
        except StoreError as err:
            logger.warning("%s", err)
            found = None
        with self._lock:
            self._cached, self._at = found, time.monotonic()
        return found
