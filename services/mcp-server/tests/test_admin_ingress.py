"""Admin GUI behind Home Assistant Ingress (add-on, ADR 070)."""

import io
import json
import zipfile
from typing import Any

import httpx2
from pydantic import SecretStr

from xomtra_mcp.admin.app import AdminApp, _same_address
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.admin.html import ASSET_VERSION
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
    # Versioned, so that an upgrade does not show new pages with a cached old stylesheet.
    assert f'href="{INGRESS}/static.css?v={ASSET_VERSION}"' in resp.text
    assert f"action='{INGRESS}/tokens/issue'" in resp.text
    assert "/admin/" not in resp.text
    assert "Dagster" not in resp.text  # no Dagster UI in the add-on
    assert "Sign out" not in resp.text  # Home Assistant owns the session
    assert "jana" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert root.status_code == 200
    assert "Home Assistant backups do not include" in root.text  # the lake is excluded
    assert "its own virtual machine" in root.text
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


async def _csrf(client: Any, headers: dict[str, str]) -> str:
    page = await client.get("/tokens", headers=headers)
    assert page.status_code == 200
    return str(page.text.split("name=csrf value=", 1)[1].split(">", 1)[0])


async def test_tokens_behind_ingress() -> None:
    app, _, store = make_app()
    form = {"owner": "Petr", "description": "laptop", "role": "xomtra.Reader", "days": "90"}
    async with client_for(app) as client:
        token = await _csrf(client, HA)
        missing = await client.post(
            "/tokens/issue", data=form, headers={**HA, "origin": f"http://{HA_HOST}"}
        )
        forged = await client.post("/tokens/issue", data={**form, "csrf": "0" * 64}, headers=HA)
        issued = await client.post("/tokens/issue", data={**form, "csrf": token}, headers=HA)
        mcpb = await client.get("/connect/xomtra.mcpb", headers=HA)
    assert missing.status_code == 403
    assert forged.status_code == 403
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


async def test_forms_behind_ingress_over_plain_http() -> None:
    """HA opened over http in the LAN: the browser sends no Sec-Fetch-* (not a secure
    context) and an Origin unlike the X-Forwarded-Host that reaches the add-on. The
    form token decides; it belongs to one user and one Ingress session."""
    app, _, _ = make_app()
    form = {"owner": "Petr", "description": "laptop", "role": "xomtra.Reader", "days": "90"}
    lan = {**HA, "origin": "http://homeassistant.lan:8123"}
    other_session = {**HA, "x-ingress-path": "/api/hassio_ingress/other"}
    other_user = {**HA, "x-remote-user-id": "1d2e3f"}
    async with client_for(app) as client:
        token = await _csrf(client, HA)
        own = await client.post("/tokens/issue", data={**form, "csrf": token}, headers=lan)
        session = await client.post(
            "/tokens/issue", data={**form, "csrf": token}, headers=other_session
        )
        user = await client.post("/tokens/issue", data={**form, "csrf": token}, headers=other_user)
    assert own.status_code == 200
    assert session.status_code == 403
    assert user.status_code == 403


async def test_fetch_metadata_refuses_other_sites() -> None:
    """Where the browser sends Sec-Fetch-Site (https), another site is refused even
    with a valid form token."""
    app, _, _ = make_app()
    form = {"owner": "Petr", "description": "laptop", "role": "xomtra.Reader", "days": "90"}
    proxied = {**HA, "origin": "https://ha.example.org"}
    async with client_for(app) as client:
        data = {**form, "csrf": await _csrf(client, HA)}
        own = await client.post(
            "/tokens/issue", data=data, headers={**proxied, "sec-fetch-site": "same-origin"}
        )
        other = await client.post(
            "/tokens/issue", data=data, headers={**proxied, "sec-fetch-site": "cross-site"}
        )
        sibling = await client.post(
            "/tokens/issue", data=data, headers={**proxied, "sec-fetch-site": "same-site"}
        )
    assert own.status_code == 200
    assert other.status_code == 403
    assert sibling.status_code == 403


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


async def test_ingress_page_has_the_xomtra_look() -> None:
    app, _, _ = make_app()
    async with client_for(app) as client:
        resp = await client.get("/tokens", headers=HA)
        css = await client.get("/static.css", headers=HA)
        font = await client.get("/fonts/Geist-Variable.woff2", headers=HA)
        theme = await client.get("/ui.js", headers=HA)
        tips = await client.get("/tips.json", headers=HA)
        icon = await client.get("/favicon.svg", headers=HA)
        missing = await client.get("/fonts/..%2Fapp.py", headers=HA)
    assert f'<script src="{INGRESS}/ui.js?v={ASSET_VERSION}"></script>' in resp.text
    assert f'href="{INGRESS}/favicon.svg?v={ASSET_VERSION}"' in resp.text
    assert '<a href="' + INGRESS + '/tokens" aria-current=page>Tokens</a>' in resp.text
    assert "<b>xomtra</b><small>admin</small>" in resp.text
    # Fonts resolve relative to the stylesheet, so they work under the Ingress path too.
    assert 'url("fonts/Geist-Variable.woff2")' in css.text
    assert "prefers-color-scheme" not in css.text  # dark unless Home Assistant is light
    assert ":root[data-theme=light]" in css.text
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"
    assert font.content[:4] == b"wOF2"
    assert theme.status_code == 200
    assert "darkMode" in theme.text
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/svg+xml"
    assert missing.status_code == 404
    assert set(tips.json()) == {"en", "cs", "de", "pl", "nl"}


async def test_tokens_page_explains_the_flow_with_actions() -> None:
    app, _, _ = make_app()
    async with client_for(app) as client:
        resp = await client.get("/tokens", headers=HA)
    assert "<figure class='flow'>" in resp.text
    assert f"href='{INGRESS}/connect/xomtra.mcpb' download" in resp.text
    assert "href='#issue'" in resp.text
    assert "<form id=issue " in resp.text
    # Readable without the stylesheet too (a stale cache): every shape has its own
    # colours and only the wide picture shows.
    assert "<svg class='tall' display='none'" in resp.text
    assert "<rect class='box' fill='none'" in resp.text
    assert "<path class='ln req' fill='none'" in resp.text


def test_values_are_formatted_for_the_locale() -> None:
    from datetime import UTC, datetime

    from xomtra_mcp.admin.html import TIPS, fmt, table

    assert fmt(31204118) == '<data class="n" value="31204118">31,204,118</data>'
    assert fmt(0.9812, pct=True) == '<data class="p" value="0.9812">98.1%</data>'
    assert fmt(datetime(2026, 11, 26, 7, 20, tzinfo=UTC)) == (
        '<time datetime="2026-11-26T07:20:00Z">2026-11-26 07:20 UTC</time>'
    )
    assert 'datetime="2026-11-26T07:20:00Z"' in fmt("2026-11-26 07:20:00")  # naive = UTC
    assert fmt(True) == "True"
    html = table(["missing_share", "nothing"], [[0.5, "x"]], {0})
    assert '<data class="p" value="0.5">50.0%</data>' in html
    assert 'data-tip="missing_share"' in html
    assert "data-tip" not in html.split("nothing")[0].rsplit("<th", 1)[-1]
    assert all(set(TIPS[lang]) == set(TIPS["en"]) for lang in TIPS)


async def test_gui_follows_the_language_cookie_and_accept_language() -> None:
    app, _, _ = make_app()
    async with client_for(app) as client:
        cs = await client.get("/", headers={**HA, "cookie": "xomtra_lang=cs"})
        tokens = await client.get("/tokens", headers={**HA, "cookie": "xomtra_lang=cs"})
        de = await client.get("/", headers={**HA, "accept-language": "de-AT,de;q=0.9"})
        en = await client.get("/", headers={**HA, "accept-language": "fr-FR"})
    assert "<html lang=cs>" in cs.text
    assert "<h1>Přehled</h1>" in cs.text
    assert "Zálohy Home Assistantu neobsahují analytiku xomtra" in cs.text
    assert "Home Assistant backups do not include" not in cs.text
    assert ">Vydat token</button>" in tokens.text
    assert "placeholder='pro koho'" in tokens.text
    assert "<title>Tokeny · xomtra admin</title>" in tokens.text
    assert "<html lang=de>" in de.text
    assert "<h1>Übersicht</h1>" in de.text
    assert "<html lang=en>" in en.text
    assert "<h1>Overview</h1>" in en.text
