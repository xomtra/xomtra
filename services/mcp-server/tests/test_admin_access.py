"""Admin GUI: sign-in with Home Assistant (ADR 053), personal tokens and the Claude
Desktop extension download (ADR 056/059)."""

import io
import json
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from pydantic import SecretStr

from hacore.testing import VALID_TOKEN, FakeHA, running_fake_ha
from xomtra_mcp.admin.app import SESSION_COOKIE, AdminApp
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.ha_auth import HomeAssistantLogin
from xomtra_mcp.tokens import MemoryTokenStore, token_hash

BASE = "https://xomtra.lan"


class FakeHAHttp:
    """/auth/token and /auth/revoke of Home Assistant."""

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.client_ids: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/auth/token":
            self.client_ids.append(form["client_id"])
            if form.get("code") != "good":
                return httpx2.Response(400, json={"error": "invalid_request"})
            return httpx2.Response(
                200,
                json={
                    "access_token": VALID_TOKEN,
                    "refresh_token": "refresh-1",
                    "expires_in": 1800,
                    "token_type": "Bearer",
                },
            )
        if request.url.path == "/auth/revoke":
            self.revoked.append(form["token"])
            return httpx2.Response(200)
        return httpx2.Response(404)


@pytest.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as running:
        yield running


def make_app(
    ha_url: str, http: FakeHAHttp, **kwargs: Any
) -> tuple[Any, MemoryAuditSink, MemoryTokenStore]:
    settings = AdminSettings(
        auth_mode="ha",
        access_mode=kwargs.pop("access_mode", "local"),
        base_url=BASE,  # type: ignore[arg-type]
        ha_url=ha_url,
        ha_auth_url="https://ha.example.org",
        admin_session_key=SecretStr("s" * 40),
        **kwargs,
    )
    login = HomeAssistantLogin(
        ha_url=ha_url,
        ha_auth_url="https://ha.example.org",
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(http.handler)),
    )
    audit, store = MemoryAuditSink(), MemoryTokenStore()
    app = AdminApp(settings, data=None, audit=audit, ha_login=login, tokens=store).build()
    return app, audit, store


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


async def sign_in(client: httpx2.AsyncClient, code: str = "good") -> httpx2.Response:
    start = await client.get("/admin/login")
    assert start.status_code == 303
    location = urlparse(start.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        "https://ha.example.org/auth/authorize"
    )
    query = parse_qs(location.query)
    assert query["client_id"] == [f"{BASE}/"]
    assert query["redirect_uri"] == [f"{BASE}/admin/auth/callback"]
    return await client.get(
        "/admin/auth/callback", params={"code": code, "state": query["state"][0]}
    )


async def test_ha_admin_signs_in_and_ha_token_is_revoked(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = {"id": "u1", "name": "Jana", "is_admin": True}
    http = FakeHAHttp()
    app, audit, _ = make_app(url, http)
    async with client_for(app) as client:
        done = await sign_in(client)
        assert done.status_code == 303
        assert SESSION_COOKIE in client.cookies
        page = await client.get("/admin/tokens")
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        form_action = next(d for d in csp.split(";") if d.strip().startswith("form-action"))
        assert form_action.split()[1:] == ["'self'", "https://ha.example.org"]
    assert http.revoked == ["refresh-1"]
    assert http.client_ids == [f"{BASE}/"]
    login = audit.records[0]
    assert (login.tool, login.subject, login.idp) == ("admin:login", "u1", "ha")


async def test_non_admin_ha_user_is_refused(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = {"id": "u2", "name": "Petr", "is_admin": False}
    http = FakeHAHttp()
    app, audit, _ = make_app(url, http)
    async with client_for(app) as client:
        resp = await sign_in(client)
        assert resp.status_code == 403
        assert SESSION_COOKIE not in client.cookies
    assert http.revoked == ["refresh-1"]  # revoked also when refused
    assert [(r.tool, r.decision) for r in audit.records] == [("admin:login", "denied")]


async def test_ha_code_rejected(fake_ha: tuple[FakeHA, str]) -> None:
    _, url = fake_ha
    app, _, _ = make_app(url, FakeHAHttp())
    async with client_for(app) as client:
        resp = await sign_in(client, code="bad")
    assert resp.status_code == 400
    assert "did not issue a token" in resp.text


async def test_tokens_issue_list_revoke_and_mcpb(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = {"id": "u1", "name": "Jana", "is_owner": True}
    app, audit, store = make_app(url, FakeHAHttp(), mcp_url="https://xomtra.lan/mcp")
    async with client_for(app) as client:
        await sign_in(client)
        origin = {"origin": BASE}
        issued = await client.post(
            "/admin/tokens/issue",
            data={
                "owner": "Petr",
                "description": "Claude Desktop <laptop>",
                "role": "Xomtra.Reader",
                "days": "90",
            },
            headers=origin,
        )
        assert issued.status_code == 200
        token = issued.text.split("value='", 1)[1].split("'", 1)[0]
        assert token.startswith("xmt_")
        assert "claude mcp add --transport http xomtra https://xomtra.lan/mcp" in issued.text
        assert "&lt;laptop&gt;" in issued.text
        assert issued.headers["cache-control"] == "no-store"
        info = store.verify(token_hash(token))
        assert info is not None
        assert (info.owner, info.role, info.created_by) == ("Petr", "Xomtra.Reader", "Jana")

        listing = await client.get("/admin/tokens")
        assert info.id in listing.text
        assert token not in listing.text  # never shown again

        cross = await client.post(
            "/admin/tokens/revoke", data={"id": info.id}, headers={"origin": "https://evil.example"}
        )
        assert cross.status_code == 403
        assert store.verify(token_hash(token)) is not None

        bad = await client.post(
            "/admin/tokens/issue",
            data={"owner": "x", "description": "y", "role": "Root", "days": "30"},
            headers=origin,
        )
        assert bad.status_code == 400

        revoked = await client.post("/admin/tokens/revoke", data={"id": info.id}, headers=origin)
        assert revoked.status_code == 303
        assert store.verify(token_hash(token)) is None

        mcpb = await client.get("/admin/connect/xomtra.mcpb")
        assert mcpb.status_code == 200
        with zipfile.ZipFile(io.BytesIO(mcpb.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["user_config"]["url"]["default"] == "https://xomtra.lan/mcp"
        assert (await client.get("/admin/connect")).status_code == 200
    tools = [r.tool for r in audit.records]
    assert "admin:token_issue" in tools
    assert "admin:token_revoke" in tools
    issue = next(r for r in audit.records if r.tool == "admin:token_issue")
    assert token not in json.dumps(issue.arguments)
    assert issue.arguments["id"] == info.id


async def test_tokens_only_in_local_mode(fake_ha: tuple[FakeHA, str]) -> None:
    fake, url = fake_ha
    fake.results["auth/current_user"] = {"id": "u1", "name": "Jana", "is_admin": True}
    app, _, store = make_app(url, FakeHAHttp(), access_mode="public")
    async with client_for(app) as client:
        await sign_in(client)
        page = await client.get("/admin/tokens")
        assert "only in the local access mode" in page.text
        resp = await client.post(
            "/admin/tokens/issue",
            data={"owner": "x", "description": "y", "role": "Xomtra.Reader", "days": "30"},
            headers={"origin": BASE},
        )
        assert "only in the local access mode" in resp.text
    assert store.list() == []


def test_admin_settings_for_ha() -> None:
    with pytest.raises(ValueError, match="XOMTRA_HA_URL"):
        AdminSettings(
            auth_mode="ha",
            base_url=BASE,  # type: ignore[arg-type]
            admin_session_key=SecretStr("s" * 40),
        )
    s = AdminSettings(
        auth_mode="ha",
        base_url=BASE,  # type: ignore[arg-type]
        ha_url="http://ha:8123",
        admin_session_key=SecretStr("s" * 40),
        ha_allowed_users="a, b",  # type: ignore[arg-type]
    )
    assert s.endpoint == f"{BASE}/mcp"
    assert s.client_id == f"{BASE}/"
    assert s.ha_allowed_users == ["a", "b"]


def test_admin_settings_empty_optional_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # compose passes unset variables as empty strings (homelab .env without new keys)
    for key, value in {
        "XOMTRA_BASE_URL": BASE,
        "XOMTRA_ADMIN_SESSION_KEY": "s" * 40,
        "XOMTRA_ENTRA_TENANT_ID": "t",
        "XOMTRA_ENTRA_CLIENT_ID": "c",
        "XOMTRA_ENTRA_CLIENT_SECRET": "x",
        "XOMTRA_MCP_URL": "",
        "XOMTRA_HA_AUTH_URL": "",
        "XOMTRA_HA_ALLOWED_USERS": "",
        "XOMTRA_ACCESS_MODE": "public",
    }.items():
        monkeypatch.setenv(key, value)
    s = AdminSettings()
    assert s.mcp_url is None
    assert s.ha_auth_url is None
    assert s.ha_allowed_users == []
