"""Sign-in with a Home Assistant account (ADR 053) against a real Home Assistant.

Lives with the hacore contract tests because they share the onboarded instance; the
code under test is in xomtra_mcp.ha_auth. Checks what the implementation relies on:
HA accepts a client_id on another host than HA itself (xomtra's URL), with the redirect
URI on that host; the code exchange without secret or PKCE; auth/current_user; refresh;
and revocation of the whole session.
"""

import secrets
from typing import Any

import httpx2
import pytest

from hacore import HAClient, HAError
from hacore.onboarding import OnboardedTokens
from xomtra_mcp.ha_auth import (
    HomeAssistantLogin,
    HomeAssistantTokenVerifier,
    RoleMap,
    fetch_current_user,
)

CLIENT_ID = "https://xomtra.example.org/"
REDIRECT = "https://xomtra.example.org/admin/auth/callback"


async def create_user(ha_url: str, admin_token: str, admin: bool) -> tuple[str, str]:
    username = f"login-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(16)
    async with HAClient(ha_url, admin_token) as ha:
        user = await ha.send_command(
            "config/auth/create",
            name=username,
            group_ids=["system-admin" if admin else "system-users"],
            local_only=False,
        )
        await ha.send_command(
            "config/auth_provider/homeassistant/create",
            user_id=user["user"]["id"],
            username=username,
            password=password,
        )
    return username, password


async def login_code(ha_url: str, username: str, password: str) -> str:
    async with httpx2.AsyncClient(base_url=ha_url, timeout=30) as http:
        flow = (
            await http.post(
                "/auth/login_flow",
                json={
                    "client_id": CLIENT_ID,
                    "handler": ["homeassistant", None],
                    "redirect_uri": REDIRECT,
                },
            )
        ).json()
        assert "flow_id" in flow, flow
        result = (
            await http.post(
                f"/auth/login_flow/{flow['flow_id']}",
                json={"client_id": CLIENT_ID, "username": username, "password": password},
            )
        ).json()
    assert result.get("type") == "create_entry", result
    return str(result["result"])


async def token_request(ha_url: str, data: dict[str, str]) -> httpx2.Response:
    async with httpx2.AsyncClient(base_url=ha_url, timeout=30) as http:
        return await http.post("/auth/token", data=data)


@pytest.mark.parametrize("admin", [True, False])
async def test_admin_gui_login(ha_url: str, tokens: OnboardedTokens, admin: bool) -> None:
    username, password = await create_user(ha_url, tokens.admin_token, admin)
    code = await login_code(ha_url, username, password)
    login = HomeAssistantLogin(
        ha_url=ha_url, ha_auth_url=ha_url, client_id=CLIENT_ID, redirect_uri=REDIRECT
    )
    assert login.authorize_url("s").startswith(f"{ha_url}/auth/authorize?")
    user = await login.exchange(code)
    assert user.name == username
    assert user.is_admin is admin


async def test_mcp_tokens_refresh_and_revoke(ha_url: str, tokens: OnboardedTokens) -> None:
    username, password = await create_user(ha_url, tokens.admin_token, admin=False)
    code = await login_code(ha_url, username, password)
    reply = await token_request(
        ha_url, {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}
    )
    assert reply.status_code == 200, reply.text
    issued: dict[str, Any] = reply.json()
    assert issued["expires_in"] > 0
    assert issued["refresh_token"]

    verifier = HomeAssistantTokenVerifier(ha_url=ha_url, role_map=RoleMap(user="xomtra.Reader"))
    access = await verifier.verify_token(issued["access_token"])
    assert access is not None
    assert access.claims["roles"] == ["xomtra.Reader"]
    assert access.claims["preferred_username"] == username

    refreshed = await token_request(
        ha_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": CLIENT_ID,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new_access = refreshed.json()["access_token"]
    assert (await fetch_current_user(ha_url, new_access)).name == username

    login = HomeAssistantLogin(
        ha_url=ha_url, ha_auth_url=ha_url, client_id=CLIENT_ID, redirect_uri=REDIRECT
    )
    await login.revoke(issued["refresh_token"])
    gone = await token_request(
        ha_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": issued["refresh_token"],
            "client_id": CLIENT_ID,
        },
    )
    assert gone.status_code == 400
    with pytest.raises(HAError):
        await fetch_current_user(ha_url, new_access)
