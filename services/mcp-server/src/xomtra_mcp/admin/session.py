"""Signed, expiring cookie values (HMAC-SHA256 over compact JSON)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, key: str, purpose: str) -> None:
        # One key, separate derived keys per purpose (session vs. login transaction).
        self._key = hashlib.sha256(f"{purpose}\0{key}".encode()).digest()

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def dumps(self, data: dict[str, Any], max_age: int, *, now: float | None = None) -> str:
        body = dict(data, exp=int((now or time.time()) + max_age))
        payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        return f"{payload}.{self._mac(payload)}"

    def loads(self, value: str | None, *, now: float | None = None) -> dict[str, Any] | None:
        if not value or value.count(".") != 1:
            return None
        payload, mac = value.split(".")
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or int(data.get("exp", 0)) < (now or time.time()):
            return None
        return data
