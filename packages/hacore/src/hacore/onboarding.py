"""Automated onboarding of a fresh Home Assistant instance (contract tests, dev stack).

Creates the owner account through the onboarding API, then a dedicated non-admin user
the way production uses xomtra, and returns a long-lived token for each. Only meant for
throwaway instances in containers: credentials are generated, not configured.

CLI::

    python -m hacore.onboarding http://127.0.0.1:8123 > tokens.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from hacore.client import HAClient

NON_ADMIN_USERNAME = "xomtra"


@dataclass(frozen=True, slots=True)
class OnboardedTokens:
    admin_token: str
    user_token: str


def _request(
    url: str,
    *,
    data: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    token: str | None = None,
) -> Any:
    headers = {}
    body: bytes | None = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL from caller
        raw = resp.read()
    return json.loads(raw) if raw else None


def wait_until_ready(base_url: str, timeout: float = 180.0) -> None:
    """Wait until the HTTP API answers (onboarding endpoint is public)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            _request(f"{base_url}/api/onboarding")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if time.monotonic() > deadline:
                raise
            time.sleep(2)
        else:
            return


def _token_from_code(base_url: str, client_id: str, code: str) -> str:
    reply = _request(
        f"{base_url}/auth/token",
        form={"grant_type": "authorization_code", "code": code, "client_id": client_id},
    )
    return str(reply["access_token"])


def _login(base_url: str, client_id: str, username: str, password: str) -> str:
    flow = _request(
        f"{base_url}/auth/login_flow",
        data={
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": client_id,
        },
    )
    result = _request(
        f"{base_url}/auth/login_flow/{flow['flow_id']}",
        data={"client_id": client_id, "username": username, "password": password},
    )
    if result.get("type") != "create_entry":
        raise RuntimeError(f"Login of {username!r} failed: {result.get('errors')}")
    return _token_from_code(base_url, client_id, result["result"])


async def _long_lived_token(base_url: str, access_token: str, name: str) -> str:
    async with HAClient(base_url, access_token) as ha:
        token: str = await ha.send_command(
            "auth/long_lived_access_token", client_name=name, lifespan=30
        )
        return token


async def _create_user(base_url: str, admin_token: str, username: str, password: str) -> None:
    async with HAClient(base_url, admin_token) as ha:
        user = await ha.send_command(
            "config/auth/create", name="xomtra", group_ids=["system-users"], local_only=False
        )
        await ha.send_command(
            "config/auth_provider/homeassistant/create",
            user_id=user["user"]["id"],
            username=username,
            password=password,
        )


def onboard(base_url: str) -> OnboardedTokens:
    """Onboard a fresh instance; fails if it was already onboarded."""
    base_url = base_url.rstrip("/")
    client_id = f"{base_url}/"
    wait_until_ready(base_url)

    admin_password = secrets.token_urlsafe(24)
    reply = _request(
        f"{base_url}/api/onboarding/users",
        data={
            "client_id": client_id,
            "name": "Owner",
            "username": "owner",
            "password": admin_password,
            "language": "en",
        },
    )
    admin_access = _token_from_code(base_url, client_id, reply["auth_code"])
    _request(f"{base_url}/api/onboarding/core_config", data={}, token=admin_access)
    _request(f"{base_url}/api/onboarding/analytics", data={}, token=admin_access)
    _request(
        f"{base_url}/api/onboarding/integration",
        data={"client_id": client_id, "redirect_uri": client_id},
        token=admin_access,
    )

    user_password = secrets.token_urlsafe(24)
    asyncio.run(_create_user(base_url, admin_access, NON_ADMIN_USERNAME, user_password))
    user_access = _login(base_url, client_id, NON_ADMIN_USERNAME, user_password)
    return OnboardedTokens(
        admin_token=asyncio.run(_long_lived_token(base_url, admin_access, "xomtra-admin")),
        user_token=asyncio.run(_long_lived_token(base_url, user_access, "xomtra")),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.onboarding")
    parser.add_argument("base_url")
    args = parser.parse_args(argv)
    json.dump(asdict(onboard(args.base_url)), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
