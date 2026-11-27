import json
from typing import Any

import httpx2

from xomtra_mcp.admin.app import AdminApp
from xomtra_mcp.admin.config import AdminSettings
from xomtra_mcp.answer_sets import MemoryAnswerSetStore
from xomtra_mcp.audit import MemoryAuditSink
from xomtra_mcp.guidelines import CORE_ONLY_SET_ID, CUSTOM_SETS_MAX, DEFAULT_SET_ID, TEXT_MAX

BASE = "https://xomtra.lan"
ORIGIN = {"origin": BASE}


def client_for(store: MemoryAnswerSetStore, audit: MemoryAuditSink) -> httpx2.AsyncClient:
    app = AdminApp(
        AdminSettings(auth_mode="none", base_url=BASE), data=None, audit=audit, answer_sets=store
    ).build()
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE)


async def post(client: httpx2.AsyncClient, **data: str) -> Any:
    return await client.post("/admin/answers", data=data, headers=ORIGIN)


async def test_factory_sets_core_rules_and_activation() -> None:
    store, audit = MemoryAnswerSetStore(), MemoryAuditSink()
    async with client_for(store, audit) as client:
        page = await client.get("/admin/answers")
        assert page.status_code == 200
        for name in ("Default", "Concise", "Core only"):
            assert name in page.text
        assert "Pub" not in page.text
        assert "pub" not in page.text
        assert "Core rules (always apply, cannot be changed)" in page.text
        assert "threatens life or health" in page.text
        assert "Answer first" in page.text  # the default set is shown when none is chosen

        done = await post(client, action="activate", set=CORE_ONLY_SET_ID)
        assert done.status_code == 303
        assert store.active_set() is not None
        assert store.active_set().id == CORE_ONLY_SET_ID  # type: ignore[union-attr]
        assert (await post(client, action="activate", set="factory:nope")).status_code == 400
        # Factory sets are read only.
        refused = await post(client, action="save", set=DEFAULT_SET_ID, name="Default", text="x")
        assert refused.status_code == 400
        assert "cannot be changed" in refused.text
    assert any(r.tool == "admin:answers_activate" for r in audit.records)


async def test_duplicate_edit_preview_restore_delete() -> None:
    store, audit = MemoryAnswerSetStore(), MemoryAuditSink()
    async with client_for(store, audit) as client:
        dup = await post(client, action="duplicate", set=DEFAULT_SET_ID, name="Moje")
        assert dup.status_code == 303
        (mine,) = store.custom()
        assert mine.set.based_on == DEFAULT_SET_ID
        assert "Answer first" in mine.set.text

        text = "Answer in Czech.\r\n\r\n\r\n<script>alert(1)</script>  "
        saved = await post(client, action="save", set=mine.set.id, name="Moje", text=text)
        assert saved.status_code == 303
        stored = store.get(mine.set.id)
        assert stored is not None
        assert stored.set.text == "Answer in Czech.\n\n<script>alert(1)</script>"
        page = await client.get(f"/admin/answers?set={mine.set.id}")
        assert "<script>alert(1)</script>" not in page.text
        assert "&lt;script&gt;" in page.text
        assert "History of this set" in page.text

        preview = await post(client, action="preview", set=mine.set.id, name="Moje", text="Hi")
        assert preview.status_code == 200
        assert "Answer guidelines set &quot;Moje&quot;" in preview.text
        assert store.get(mine.set.id).set.text.startswith("Answer in Czech")  # type: ignore[union-attr]

        too_long = await post(
            client, action="save", set=mine.set.id, name="Moje", text="x" * (TEXT_MAX + 1)
        )
        assert too_long.status_code == 400
        assert "at most" in too_long.text
        clash = await post(client, action="save", set=mine.set.id, name="default", text="x")
        assert clash.status_code == 400

        first = store.versions(mine.set.id)[-1]
        restored = await post(client, action="restore", set=mine.set.id, version=str(first.id))
        assert restored.status_code == 303
        assert "Answer first" in store.get(mine.set.id).set.text  # type: ignore[union-attr]

        await post(client, action="activate", set=mine.set.id)
        active = await post(client, action="delete", set=mine.set.id, confirm="1")
        assert active.status_code == 400
        await post(client, action="activate", set=DEFAULT_SET_ID)
        gone = await post(client, action="delete", set=mine.set.id, confirm="1")
        assert gone.status_code == 303
        assert store.custom() == []


async def test_export_import_and_limits() -> None:
    store, audit = MemoryAnswerSetStore(), MemoryAuditSink()
    store.create("Moje", "popis", "Text sady.", by="admin")
    async with client_for(store, audit) as client:
        export = await client.get("/admin/answers/export")
        assert export.status_code == 200
        data = export.json()
        assert data["xomtra_answer_sets"] == 1
        assert data["sets"] == [{"name": "Moje", "description": "popis", "text": "Text sady."}]
        assert data["active"] == "Default"

        imported = await post(client, action="import", json=json.dumps(data))
        assert imported.status_code == 303
        assert sorted(s.set.name for s in store.custom()) == ["Moje", "Moje (2)"]
        bad = await post(client, action="import", json="[1]")
        assert bad.status_code == 400

        for i in range(CUSTOM_SETS_MAX - 2):
            store.create(f"s{i}", "", "", by="admin")
        full = await post(client, action="create", name="one more")
        assert full.status_code == 400


async def test_user_text_is_not_translated() -> None:
    store, audit = MemoryAnswerSetStore(), MemoryAuditSink()
    set_id = store.create("Overview", "", "Overview", by="admin")
    async with client_for(store, audit) as client:
        page = await client.get(
            f"/admin/answers?set={set_id}", headers={"cookie": "xomtra_lang=cs"}
        )
    assert "<bdi>Overview</bdi>" in page.text
    assert ">Overview</textarea>" in page.text
    assert "Výchozí" in page.text  # factory names are part of the GUI
