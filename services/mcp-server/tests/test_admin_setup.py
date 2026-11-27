"""First-start setup of the compose edition (ADR 077): one-time code from the log,
Home Assistant URL and token stored by the admin GUI, then sign-in with HA."""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from hacore.ha_connection import HAConnection
from hacore.testing import VALID_TOKEN, FakeHA, running_fake_ha
from xomtra_mcp.admin import app as app_module
from xomtra_mcp.admin import setup
from xomtra_mcp.admin.__main__ import _announce_setup, main
from xomtra_mcp.admin.app import AdminApp, _default_ha_port
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.audit import MemoryAuditSink

BASE = "https://192.0.2.10"
SESSION_KEY = "s" * 40


class MemoryResolver:
    """Stands in for hacore.ha_connection.Resolver with a stored connection."""

    from_env = False
    storable = True

    def __init__(self) -> None:
        self.current: HAConnection | None = None
        self.invalidated = 0

    def get(self) -> HAConnection | None:
        return self.current

    def invalidate(self) -> None:
        self.invalidated += 1


@pytest.fixture
async def fake_ha() -> AsyncIterator[tuple[FakeHA, str]]:
    async with running_fake_ha() as (fake, url):
        fake.results.update(
            {
                "config/floor_registry/list": [],
                "config/area_registry/list": [],
                "config/label_registry/list": [],
                "config/device_registry/list": [],
                "config/entity_registry/list": [{"entity_id": "sensor.x"}],
            }
        )
        yield fake, url


def settings(**kwargs: Any) -> AdminSettings:
    values: dict[str, Any] = {
        "auth_mode": "ha",
        "access_mode": "local",
        "base_url": BASE,
        "admin_session_key": SecretStr(SESSION_KEY),
        "settings_key": SecretStr("k" * 40),
        "audit_pg_host": "postgres",
        "audit_admin_password": SecretStr("pw"),
    }
    values.update(kwargs)
    return AdminSettings(**values)


def make_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, MemoryResolver, MemoryAuditSink, list[dict[str, Any]]]:
    audit, saved = MemoryAuditSink(), []
    resolver = MemoryResolver()

    def save(dsn: str, key: str, url: str, token: str, **kw: Any) -> None:
        saved.append({"url": url, "token": token, **kw})
        resolver.current = HAConnection(url, token, "stored")

    monkeypatch.setattr(app_module, "save_connection", save)
    admin = AdminApp(settings(), data=None, audit=audit)
    admin.ha = resolver  # type: ignore[assignment]
    return admin.build(), resolver, audit, saved


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


def test_code_is_stable_and_compared_loosely() -> None:
    code = setup.setup_code(SESSION_KEY)
    assert code == setup.setup_code(SESSION_KEY)
    assert code != setup.setup_code("t" * 40)
    assert len(code) == 19
    assert code.count("-") == 3
    assert setup.code_matches(SESSION_KEY, code.lower())
    assert setup.code_matches(SESSION_KEY, code.replace("-", ""))
    assert not setup.code_matches(SESSION_KEY, "AAAA-BBBB-CCCC-DDDD")


def test_settings_need_no_ha_url_with_the_store() -> None:
    assert settings().ha_url is None
    with pytest.raises(ValueError, match="XOMTRA_HA_URL"):
        settings(settings_key=None)


async def test_setup_with_code_then_sign_in(
    fake_ha: tuple[FakeHA, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ha_url = fake_ha
    app, resolver, audit, saved = make_app(monkeypatch)
    code = setup.setup_code(SESSION_KEY)
    async with client_for(app) as client:
        # Nobody can sign in yet: the sign-in leads to the setup page.
        login = await client.get("/admin/login")
        assert login.status_code == 303
        assert login.headers["location"].endswith("/admin/setup")
        ask = await client.get("/admin/setup")
        assert ask.status_code == 200
        assert "xomtra-admin setup-link" in ask.text
        assert "name=url" not in ask.text
        bad = await client.get("/admin/setup", params={"code": "AAAA-BBBB-CCCC-DDDD"})
        assert bad.status_code == 403
        form = await client.get("/admin/setup", params={"code": code})
        assert form.status_code == 200
        assert "name=url" in form.text
        assert "name=token" in form.text
        # A token Home Assistant refuses is not stored.
        refused = await client.post(
            "/admin/setup", data={"code": code, "url": ha_url, "token": "wrong"}
        )
        assert refused.status_code == 400
        assert saved == []
        # A password manager must not fill in a saved password.
        assert "autocomplete=new-password" in form.text
        # Whitespace copied with the token (a wrapped line, a trailing newline) is dropped.
        pasted = f" {VALID_TOKEN[:10]}\n{VALID_TOKEN[10:]} \n"
        done = await client.post(
            "/admin/setup", data={"code": code, "url": f" {ha_url}/ ", "token": pasted}
        )
        assert done.status_code == 200, done.text
        assert "is connected" in done.text
        assert VALID_TOKEN not in done.text
        assert len(saved) == 1
        assert (saved[0]["url"], saved[0]["token"], saved[0]["by"]) == (
            ha_url,
            VALID_TOKEN,
            "setup",
        )
        assert resolver.invalidated == 1
        # The code is used up: the page now needs a signed-in administrator.
        again = await client.get("/admin/setup", params={"code": code})
        assert again.status_code == 303
        assert again.headers["location"].endswith("/admin/login")
        login = await client.get("/admin/login")
        assert login.status_code == 303
        assert login.headers["location"].startswith(ha_url + "/auth/authorize?")
        page = await client.get("/admin/setup")  # a page of the app after the sign-in URL
        assert page.status_code == 303
    record = audit.records[-1]
    assert (record.tool, record.subject, record.idp) == ("admin:setup", "setup-code", "setup")
    assert record.arguments["url"] == ha_url


async def test_setup_refuses_cross_site_post(
    fake_ha: tuple[FakeHA, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ha_url = fake_ha
    app, _, _, saved = make_app(monkeypatch)
    code = setup.setup_code(SESSION_KEY)
    async with client_for(app) as client:
        resp = await client.post(
            "/admin/setup",
            data={"code": code, "url": ha_url, "token": VALID_TOKEN},
            headers={"sec-fetch-site": "cross-site"},
        )
    assert resp.status_code == 403
    assert saved == []


async def test_setup_page_with_ha_from_the_environment() -> None:
    admin = AdminApp(settings(ha_url="http://ha:8123", ha_token=SecretStr("t")), data=None)
    async with client_for(admin.build()) as client:
        resp = await client.get("/admin/setup")
    assert resp.status_code == 200
    assert "XOMTRA_HA_URL" in resp.text
    assert "name=url" not in resp.text


def test_setup_link_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Empty(MemoryResolver):
        pass

    monkeypatch.setattr(AdminSettings, "ha_resolver", lambda self: Empty())
    with caplog.at_level(logging.WARNING):
        _announce_setup(settings())
    assert f"{BASE}/admin/setup?code={setup.setup_code(SESSION_KEY)}" in caplog.text


def test_setup_link_command_prints_the_link(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(AdminSettings, "ha_resolver", lambda self: MemoryResolver())
    monkeypatch.setattr("xomtra_mcp.admin.__main__.AdminSettings", settings)
    with pytest.raises(SystemExit) as exit_info:
        main(["setup-link"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert f"{BASE}/admin/setup?code={setup.setup_code(SESSION_KEY)}" in out


def test_setup_link_command_once_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    resolver = MemoryResolver()
    resolver.current = HAConnection("http://ha:8123", "t", "stored")
    monkeypatch.setattr(AdminSettings, "ha_resolver", lambda self: resolver)
    monkeypatch.setattr("xomtra_mcp.admin.__main__.AdminSettings", settings)
    with pytest.raises(SystemExit) as exit_info:
        main(["setup-link"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "code=" not in out
    assert f"{BASE}/admin/" in out


def test_unknown_argument_is_refused() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["nonsense"])
    assert "usage" in str(exit_info.value.code)


def test_default_ha_port() -> None:
    assert _default_ha_port("http://192.0.2.5") == "http://192.0.2.5:8123"
    assert _default_ha_port("https://ha.example/") == "https://ha.example:8123/"
    assert _default_ha_port("http://[2001:db8::1]") == "http://[2001:db8::1]:8123"
    assert _default_ha_port("http://192.0.2.5:8123") is None
    assert _default_ha_port("https://ha.example:443") is None


async def test_setup_without_port_tries_8123_then_hints(
    fake_ha: tuple[FakeHA, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ha_url = fake_ha
    app, _, _, saved = make_app(monkeypatch)
    code = setup.setup_code(SESSION_KEY)
    tried: list[str] = []
    real = app_module.status.ha_status

    async def ha_status(url: str | None, token: str | None) -> dict[str, Any]:
        tried.append(str(url))
        # The address without a port reaches something else; :8123 is Home Assistant.
        return await real(ha_url if str(url).endswith(":8123") else "http://127.0.0.1:9", token)

    monkeypatch.setattr(app_module.status, "ha_status", ha_status)
    async with client_for(app) as client:
        done = await client.post(
            "/admin/setup", data={"code": code, "url": "http://192.0.2.5", "token": VALID_TOKEN}
        )
        assert done.status_code == 200, done.text
        assert tried == ["http://192.0.2.5", "http://192.0.2.5:8123"]
        assert saved[0]["url"] == "http://192.0.2.5:8123"

    async def unreachable(url: str | None, token: str | None) -> dict[str, Any]:
        return await real("http://127.0.0.1:9", token)

    monkeypatch.setattr(app_module.status, "ha_status", unreachable)
    app, _, _, saved = make_app(monkeypatch)
    async with client_for(app) as client:
        failed = await client.post(
            "/admin/setup", data={"code": code, "url": "http://192.0.2.5", "token": VALID_TOKEN}
        )
    assert failed.status_code == 400
    assert "The address has no port." in failed.text
    assert saved == []
