from typing import Any

import httpx2
import pytest

from xomtra_mcp.admin.analysis import MemorySettingsStore, preview
from xomtra_mcp.admin.app import AdminApp
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.data import DataAccess

BASE = "https://xomtra.lan"


def settings() -> AdminSettings:
    return AdminSettings(auth_mode="none", base_url=BASE)


def client_for(app: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


def dev_app(data: DataAccess | None, store: MemorySettingsStore, audit: MemoryAuditSink) -> Any:
    return AdminApp(settings(), data=data, audit=audit, analysis=store).build()


async def test_analysis_settings_save_validate_reset() -> None:
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    async with client_for(dev_app(None, store, audit)) as client:
        page = await client.get("/admin/analysis")
        assert page.status_code == 200
        assert "XOMTRA_CORRELATION_MAX_Q" in page.text
        assert "XOMTRA_ANOMALY_Z_CAP" in page.text

        post = {
            "key": "correlation_max_q",
            "value": "0,005",
            "reason": "too many",
            "action": "save",
        }
        done = await client.post("/admin/analysis", data=post, headers={"origin": BASE})
        assert done.status_code == 303
        assert store.current()["correlation_max_q"].value == pytest.approx(0.005)
        assert store.current()["correlation_max_q"].reason == "too many"

        bad = await client.post(
            "/admin/analysis",
            data={"key": "correlation_max_q", "value": "1.5", "action": "save"},
            headers={"origin": BASE},
        )
        assert bad.status_code == 400
        assert "out of range" in bad.text
        unknown = await client.post(
            "/admin/analysis",
            data={"key": "ha_token", "value": "x", "action": "save"},
            headers={"origin": BASE},
        )
        assert unknown.status_code == 400

        flag = await client.post(
            "/admin/analysis",
            data={"key": "anomalies_enabled", "value": "off", "action": "save"},
            headers={"origin": BASE},
        )
        assert flag.status_code == 303
        assert store.current()["anomalies_enabled"].value is False

        reset = await client.post(
            "/admin/analysis",
            data={"key": "correlation_max_q", "action": "reset"},
            headers={"origin": BASE},
        )
        assert reset.status_code == 303
        assert "correlation_max_q" not in store.current()
        history = (await client.get("/admin/analysis")).text
        assert "back to default" in history

        cross = await client.post(
            "/admin/analysis",
            data={"key": "correlation_max_q", "value": "0.02", "action": "save"},
            headers={"origin": "https://evil.example"},
        )
        assert cross.status_code == 403
    changes = [r for r in audit.records if r.tool == "admin:analysis_setting"]
    assert [c.arguments["key"] for c in changes] == [
        "correlation_max_q",
        "anomalies_enabled",
        "correlation_max_q",
    ]


async def test_reset_all_settings_to_defaults() -> None:
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    store.set("correlation_max_q", 0.2, by="someone", reason=None)
    store.set("anomalies_enabled", False, by="someone", reason=None)
    origin = {"origin": BASE}
    async with client_for(dev_app(None, store, audit)) as client:
        page = await client.get("/admin/analysis")
        assert "Reset all to defaults" in page.text
        assert "Don't change it, if it works." in page.text
        unconfirmed = await client.post(
            "/admin/analysis", data={"action": "reset_all"}, headers=origin
        )
        assert unconfirmed.status_code == 400
        assert len(store.current()) == 2
        done = await client.post(
            "/admin/analysis",
            data={"action": "reset_all", "confirm": "1", "reason": "messed up"},
            headers=origin,
        )
        assert done.status_code == 303
        assert store.current() == {}
        page = await client.get("/admin/analysis")
        assert "Reset all to defaults" not in page.text  # nothing left to reset
        cross = await client.post(
            "/admin/analysis",
            data={"action": "reset_all", "confirm": "1"},
            headers={"origin": "https://evil.example"},
        )
        assert cross.status_code == 403
    assert [c.reason for c in store.history()[:2]] == ["messed up", "messed up"]
    (record,) = [r for r in audit.records if r.tool == "admin:analysis_setting_reset_all"]
    assert record.arguments["keys"] == ["anomalies_enabled", "correlation_max_q"]


async def test_analysis_preview_without_findings_saves_nothing() -> None:
    store = MemorySettingsStore()
    async with client_for(dev_app(None, store, MemoryAuditSink())) as client:
        resp = await client.post(
            "/admin/analysis",
            data={"key": "correlation_min_r", "value": "0.5", "action": "preview"},
            headers={"origin": BASE},
        )
    assert resp.status_code == 200
    assert "No findings to preview" in resp.text
    assert store.changes == []


def test_preview_counts_stored_findings(data: DataAccess) -> None:
    out = preview(data, "correlation_min_r", 0.55)
    assert out == {"now": 3, "kept": 2, "unflagged": 2, "kept_unflagged": 1}
    assert preview(data, "correlation_top", 5) is None


async def test_analysis_page_without_store() -> None:
    app = AdminApp(settings(), data=None, audit=MemoryAuditSink()).build()
    async with client_for(app) as c:
        resp = await c.get("/admin/analysis")
    assert "not configured" in resp.text


async def test_findings_dismiss_and_undo() -> None:
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    async with client_for(dev_app(None, store, audit)) as client:
        assert (await client.get("/admin/findings")).status_code == 200
        bad = await client.post(
            "/admin/findings/dismiss",
            data={"type": "correlation", "entity": "sensor.a", "days": "30"},
            headers={"origin": BASE},
        )
        assert bad.status_code == 400  # a correlation needs the pair
        ok = await client.post(
            "/admin/findings/dismiss",
            data={
                "type": "anomaly",
                "entity": "sensor.a",
                "related": "ignored",
                "days": "30",
                "reason": "known",
            },
            headers={"origin": BASE},
        )
        assert ok.status_code == 303
        (d,) = store.dismissed
        assert (d.finding_type, d.entity_id, d.related_entity_id, d.reason) == (
            "anomaly",
            "sensor.a",
            None,
            "known",
        )
        assert d.until is not None
        assert d.active()
        page = (await client.get("/admin/findings")).text
        assert "Undo" in page
        undo = await client.post(
            "/admin/findings/undo", data={"id": str(d.id)}, headers={"origin": BASE}
        )
        assert undo.status_code == 303
        assert not store.dismissed[0].active()
        assert "undone by" in (await client.get("/admin/findings")).text
    tools = [r.tool for r in audit.records]
    assert "admin:finding_dismiss" in tools
    assert "admin:finding_undismiss" in tools


async def test_findings_page_lists_current_findings(data: DataAccess) -> None:
    store = MemorySettingsStore()
    async with client_for(dev_app(data, store, MemoryAuditSink())) as client:
        page = (await client.get("/admin/findings?type=correlation")).text
        assert "binary_sensor.door" in page
        assert "same_device" not in page  # flagged ones only on request
        assert "Confidence of the correlations" in page
        assert "Low power" in page
        assert "3 analysed entities without an area" in page
        flagged = (await client.get("/admin/findings?type=correlation&flagged=1")).text
        assert "same_device" in flagged


async def test_overview_shows_free_disk(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    store, audit = MemorySettingsStore(), MemoryAuditSink()
    app = AdminApp(
        AdminSettings(auth_mode="none", base_url=BASE, lake_data_path=str(tmp_path)),
        data=None,
        audit=audit,
        analysis=store,
    ).build()
    async with client_for(app) as client:
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _p: usage(100 * 2**30, 98 * 2**30, 2 * 2**30)
        )
        full = await client.get("/admin/")
        monkeypatch.setattr(
            shutil, "disk_usage", lambda _p: usage(100 * 2**30, 50 * 2**30, 50 * 2**30)
        )
        roomy = await client.get("/admin/")
    assert "Free disk" in full.text
    assert "Backups" not in full.text  # the note is for the add-on only
    assert "collecting is paused" in full.text
    assert "2.0 GiB · 2%" in full.text
    assert "collecting is paused" not in roomy.text
    assert "50.0 GiB · 50%" in roomy.text
