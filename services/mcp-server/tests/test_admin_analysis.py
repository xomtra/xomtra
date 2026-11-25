from typing import Any

import httpx2
import pytest

from hammond_mcp.admin.analysis import MemorySettingsStore, preview
from hammond_mcp.admin.app import AdminApp
from hammond_mcp.admin.config import AdminSettings
from hammond_mcp.audit import MemoryAuditSink
from hammond_mcp.data import DataAccess

BASE = "https://hammond.lan"


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
        assert "HAMMOND_CORRELATION_MAX_Q" in page.text
        assert "HAMMOND_ANOMALY_Z_CAP" in page.text

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
