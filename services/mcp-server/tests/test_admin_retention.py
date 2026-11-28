"""Retention periods set in the admin GUI (ADR 094)."""

from datetime import date
from typing import Any

import httpx2

from xomtra_mcp.admin import html, view
from xomtra_mcp.admin.analysis import MemorySettingsStore
from xomtra_mcp.admin.app import AdminApp
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.audit import MemoryAuditSink

BASE = "https://xomtra.lan"
ORIGIN = {"origin": BASE}


def app_with(store: MemorySettingsStore | None, audit: MemoryAuditSink) -> Any:
    s = AdminSettings(auth_mode="none", base_url=BASE, states_retention_days=90)
    return AdminApp(s, data=None, audit=audit, analysis=store).build()


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


async def test_retention_page_has_no_env_instructions() -> None:
    async with client_for(app_with(MemorySettingsStore(), MemoryAuditSink())) as client:
        page = (await client.get("/admin/retention")).text
    assert "Hourly statistics" in page
    assert ".env" not in page
    assert "XOMTRA_" not in page
    assert "restart the ingest" not in page
    assert "name=value type=number" in page


async def test_retention_heading_names_the_xomtra_data_lake() -> None:
    """The heading says whose retention it is, so nobody takes it for the HA recorder's."""
    async with client_for(app_with(MemorySettingsStore(), MemoryAuditSink())) as client:
        en = (await client.get("/admin/retention")).text
        cs = (await client.get("/admin/retention", headers={"cookie": "xomtra_lang=cs"})).text
    assert "<h1>Data retention in the xomtra data lake</h1>" in en
    assert "aria-current=page>Retention</a>" in en
    assert "<h1>Retence dat v data lake xomtry</h1>" in cs
    assert "aria-current=page>Uchovávání</a>" in cs


async def test_retention_save_longer_validate_reset() -> None:
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    async with client_for(app_with(store, audit)) as client:
        done = await client.post(
            "/admin/retention",
            data={"key": "states_retention_days", "value": "180", "reason": "more history"},
            headers=ORIGIN,
        )
        assert done.status_code == 303
        assert store.current()["states_retention_days"].value == 180
        for bad in ("0", "3651", "1.5", "x"):
            resp = await client.post(
                "/admin/retention",
                data={"key": "states_retention_days", "value": bad},
                headers=ORIGIN,
            )
            assert resp.status_code == 400, bad
        unknown = await client.post(
            "/admin/retention", data={"key": "correlation_max_q", "value": "1"}, headers=ORIGIN
        )
        assert unknown.status_code == 400
        page = (await client.get("/admin/retention")).text
        assert "more history" in page  # history
        # Reset shortens back to the configured 90 days: needs a confirmation.
        reset = await client.post(
            "/admin/retention",
            data={"key": "states_retention_days", "action": "reset"},
            headers=ORIGIN,
        )
        assert reset.status_code == 409
        assert store.current()["states_retention_days"].value == 180
        confirmed = await client.post(
            "/admin/retention",
            data={"key": "states_retention_days", "action": "reset", "confirm": "1"},
            headers=ORIGIN,
        )
        assert confirmed.status_code == 303
        assert "states_retention_days" not in store.current()
        assert "back to configuration" in (await client.get("/admin/retention")).text
        # Retention changes stay off the analysis page.
        assert "more history" not in (await client.get("/admin/analysis")).text
    changes = [r for r in audit.records if r.tool == "admin:retention_setting"]
    assert [(c.arguments["value"], c.arguments["previous"]) for c in changes] == [
        (180, 90),
        (None, 180),
    ]


async def test_retention_shortening_needs_confirmation() -> None:
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    async with client_for(app_with(store, audit)) as client:
        ask = await client.post(
            "/admin/retention",
            data={"key": "statistics_hour_retention_days", "value": "365"},
            headers=ORIGIN,
        )
        assert ask.status_code == 409
        assert "name=confirm" in ask.text
        assert store.current() == {}
        done = await client.post(
            "/admin/retention",
            data={"key": "statistics_hour_retention_days", "value": "365", "confirm": "1"},
            headers=ORIGIN,
        )
        assert done.status_code == 303
        assert store.current()["statistics_hour_retention_days"].value == 365


async def test_retention_without_store_is_read_only() -> None:
    async with client_for(app_with(None, MemoryAuditSink())) as client:
        page = await client.get("/admin/retention")
        assert page.status_code == 200
        assert "cannot be changed here" in page.text
        resp = await client.post(
            "/admin/retention", data={"key": "states_retention_days", "value": "30"}, headers=ORIGIN
        )
        assert resp.status_code == 400


def test_dates_follow_the_language() -> None:
    day = date(2025, 11, 27)
    assert html.local_date(day, "cs") == "27. 11. 2025"
    assert html.local_date(day, "de") == "27.11.2025"
    assert html.local_date(day, "pl") == "27.11.2025"
    assert html.local_date(day, "nl") == "27-11-2025"
    assert html.local_date(day, "en") == "11/27/2025"
    token = view.current.set(view.View(lang="cs"))
    try:
        assert ">27. 11. 2025<" in html.fmt(day)
        assert 'datetime="2025-11-27"' in html.fmt(day)
    finally:
        view.current.reset(token)
