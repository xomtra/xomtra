"""Admin GUI behind Home Assistant Ingress (add-on, ADR 070)."""

import io
import json
import zipfile
from typing import Any

import httpx2
from pydantic import SecretStr

from xomtra_mcp.admin.app import AdminApp, _same_address
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.tokens import MemoryTokenStore, token_hash

PROXY = "127.0.0.1"  # httpx's ASGITransport calls from here
INGRESS = "/api/hassio_ingress/AbC-123_xyz"
HA_HOST = "homeassistant.local:8123"
HA = {
    "x-ingress-path": INGRESS,
    "x-forwarded-host": HA_HOST,
    "x-remote-user-id": "8f0c2a",
    "x-remote-user-name": "jana",
    "x-remote-user-display-name": "Jana <Admin>",
}


def make_app(**kwargs: Any) -> tuple[Any, MemoryAuditSink, MemoryTokenStore]:
    settings = AdminSettings(
        auth_mode="ingress",
        access_mode="local",
        ingress_proxy=kwargs.pop("ingress_proxy", PROXY),
        **kwargs,
    )
    audit, store = MemoryAuditSink(), MemoryTokenStore()
    return AdminApp(settings, data=None, audit=audit, tokens=store).build(), audit, store


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://172.30.33.5:8100"
    )


async def test_ingress_page_uses_the_ingress_path() -> None:
    app, audit, _ = make_app()
    async with client_for(app) as client:
        resp = await client.get("/tokens", headers=HA)
        root = await client.get("/", headers=HA)
    assert resp.status_code == 200
    assert f'href="{INGRESS}/catalog"' in resp.text
    assert f'href="{INGRESS}/static.css"' in resp.text
    assert f"action='{INGRESS}/tokens/issue'" in resp.text
    assert "/admin/" not in resp.text
    assert "Dagster" not in resp.text  # no Dagster UI in the add-on
    assert "Sign out" not in resp.text  # Home Assistant owns the session
    assert "jana" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert root.status_code == 200
    record = next(r for r in audit.records if r.tool == "admin:tokens")
    assert (record.subject, record.username, record.idp) == ("8f0c2a", "jana", "ha-ingress")


async def test_only_the_ingress_proxy_may_call() -> None:
    app, audit, _ = make_app(ingress_proxy="172.30.32.2")
    async with client_for(app) as client:
        resp = await client.get("/tokens", headers=HA)
        health = await client.get("/healthz")
    assert resp.status_code == 403
    assert health.status_code == 200
    assert audit.records == []


async def test_ingress_path_is_required_and_checked() -> None:
    app, _, _ = make_app()
    async with client_for(app) as client:
        missing = await client.get("/tokens", headers={**HA, "x-ingress-path": ""})
        bad = await client.get(
            "/tokens", headers={**HA, "x-ingress-path": "https://evil.example/x"}
        )
    assert missing.status_code == 400
    assert bad.status_code == 400


async def test_without_a_named_user_access_is_refused() -> None:
    app, audit, _ = make_app()
    headers = {k: v for k, v in HA.items() if not k.startswith("x-remote-user")}
    async with client_for(app) as client:
        resp = await client.get("/tokens", headers=headers)
        login = await client.get("/login", headers=headers)
    assert resp.status_code == 403
    assert "administrator" in resp.text
    assert login.status_code == 303
    assert login.headers["location"] == f"{INGRESS}/"
    assert audit.records == []


async def test_groups_must_include_the_admin_group_when_sent() -> None:
    app, _, _ = make_app()
    async with client_for(app) as client:
        user = await client.get("/tokens", headers={**HA, "x-remote-user-groups": "system-users"})
        admin = await client.get(
            "/tokens", headers={**HA, "x-remote-user-groups": "system-users, system-admin"}
        )
    assert user.status_code == 403
    assert admin.status_code == 200


async def test_allowed_users_apply_to_ingress() -> None:
    app, _, _ = make_app(ha_allowed_users=["petr"])
    async with client_for(app) as client:
        jana = await client.get("/tokens", headers=HA)
        petr = await client.get("/tokens", headers={**HA, "x-remote-user-name": "Petr"})
    assert jana.status_code == 403
    assert petr.status_code == 200


async def test_tokens_behind_ingress() -> None:
    app, _, store = make_app()
    form = {"owner": "Petr", "description": "laptop", "role": "xomtra.Reader", "days": "90"}
    async with client_for(app) as client:
        cross = await client.post(
            "/tokens/issue", data=form, headers={**HA, "origin": "https://evil.example"}
        )
        issued = await client.post(
            "/tokens/issue", data=form, headers={**HA, "origin": f"http://{HA_HOST}"}
        )
        mcpb = await client.get("/connect/xomtra.mcpb", headers=HA)
    assert cross.status_code == 403
    assert issued.status_code == 200
    token = issued.text.split("value='", 1)[1].split("'", 1)[0]
    info = store.verify(token_hash(token))
    assert info is not None
    assert info.created_by == "jana"
    endpoint = "http://homeassistant.local:8000/mcp"
    assert f"claude mcp add --transport http xomtra {endpoint}" in issued.text
    assert "xomtra-ca.crt" not in issued.text  # no TLS in the LAN
    with zipfile.ZipFile(io.BytesIO(mcpb.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["user_config"]["url"]["default"] == endpoint


async def test_mcp_url_setting_wins_over_the_guess() -> None:
    app, _, _ = make_app(mcp_url="https://ha.example.org:8443/mcp")
    async with client_for(app) as client:
        page = await client.get("/connect", headers=HA)
    assert "https://ha.example.org:8443/mcp" in page.text
    assert "homeassistant.local:8000" not in page.text


async def test_remote_user_headers_are_ignored_without_ingress() -> None:
    settings = AdminSettings(
        auth_mode="ha",
        base_url="https://xomtra.lan",  # type: ignore[arg-type]
        ha_url="http://ha",
        admin_session_key=SecretStr("s" * 40),
    )
    app = AdminApp(settings, data=None, audit=MemoryAuditSink()).build()
    async with client_for(app) as client:
        resp = await client.get("/admin/tokens", headers=HA)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_peer_address_comparison() -> None:
    assert _same_address("172.30.32.2", "172.30.32.2")
    assert _same_address("::ffff:172.30.32.2", "172.30.32.2")
    assert not _same_address("172.30.32.20", "172.30.32.2")
    assert not _same_address("::ffff:172.30.32.9", "172.30.32.2")
    assert not _same_address("172.30.32.2", "not-an-address")
    assert _same_address("testclient", "testclient")
