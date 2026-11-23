import os
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from hacore.lake import connect_local
from hammond_mcp.audit import AuditError, AuditRecord, MemoryAuditSink
from hammond_mcp.config import Settings
from hammond_mcp.data import DataAccess
from hammond_mcp.server import create_server

EXT = os.environ.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None
ADMIN = ["HAmmond.Admin"]
READER = ["HAmmond.Reader"]


def server(data: DataAccess, roles: list[str], audit: Any = None) -> Any:
    settings = Settings(auth_mode="none", host="127.0.0.1", dev_roles=roles)  # type: ignore[arg-type]
    return create_server(settings, data=data, audit=audit or MemoryAuditSink())


async def call(srv: Any, tool: str, **args: Any) -> Any:
    async with Client(srv) as client:
        return (await client.call_tool(tool, args)).data


async def test_tool_list_follows_roles(data: DataAccess) -> None:
    async with Client(server(data, READER)) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"search_catalog", "describe_entity", "get_timeseries", "query_sql"} <= names
    assert "ha_overview" not in names
    async with Client(server(data, [])) as client:
        assert {t.name for t in await client.list_tools()} == {"whoami"}


async def test_search_catalog_hides_denied_entities(data: DataAccess) -> None:
    admin = await call(server(data, ADMIN), "search_catalog")
    reader = await call(server(data, READER), "search_catalog")
    admin_ids = {e["entity_id"] for e in admin["entities"]}
    reader_ids = {e["entity_id"] for e in reader["entities"]}
    assert {"person.jana", "device_tracker.phone"} <= admin_ids
    assert reader_ids == admin_ids - {"person.jana", "device_tracker.phone"}

    found = await call(server(data, READER), "search_catalog", query="teplota obývák")
    assert [e["entity_id"] for e in found["entities"]] == ["sensor.temp"]
    assert found["entities"][0]["kind"] == "measurement"
    counters = await call(server(data, ADMIN), "search_catalog", kind="counter")
    assert [e["entity_id"] for e in counters["entities"]] == ["sensor.energy"]


async def test_reader_does_not_see_attributes(data: DataAccess) -> None:
    sql = "SELECT * FROM states LIMIT 1"
    admin = await call(server(data, ADMIN), "query_sql", sql=sql)
    reader = await call(server(data, READER), "query_sql", sql=sql)
    assert "attributes" in admin["columns"]
    assert "attributes" not in reader["columns"]
    assert "state_value" in reader["columns"]
    with pytest.raises(ToolError, match="attributes"):
        await call(server(data, READER), "query_sql", sql="SELECT attributes FROM states")
    views = await call(server(data, READER), "list_views")
    states = next(v for v in views["views"] if v["name"] == "states")
    assert "attributes" not in {c["name"] for c in states["columns"]}


async def test_describe_entity(data: DataAccess) -> None:
    result = await call(server(data, READER), "describe_entity", entity_id="sensor.temp")
    assert result["entity"]["value_max"] == 23.0
    assert result["statistics"]["unit_of_measurement"] == "°C"
    assert result["latest_state"]["state_value"] is not None


async def test_denied_entity_is_refused_and_audited(data: DataAccess) -> None:
    audit = MemoryAuditSink()
    srv = server(data, READER, audit)
    with pytest.raises(ToolError, match="denied"):
        await call(srv, "describe_entity", entity_id="person.jana")
    with pytest.raises(ToolError, match="denied"):
        await call(srv, "get_timeseries", entity_id="person.jana", start="2d")
    with pytest.raises(ToolError, match="do not allow"):
        await call(srv, "ha_overview")
    assert [(r.tool, r.decision) for r in audit.records] == [
        ("describe_entity", "denied"),
        ("get_timeseries", "denied"),
        ("ha_overview", "denied"),
    ]
    assert all(r.subject == "dev" and r.roles == READER for r in audit.records)


async def test_timeseries_raw_states_and_statistics(data: DataAccess) -> None:
    srv = server(data, READER)
    raw = await call(srv, "get_timeseries", entity_id="sensor.temp", start="1d")
    assert raw["resolution"] == "raw"
    assert raw["source"] == "states"
    assert 20 <= len(raw["points"]) <= 25
    hourly = await call(
        srv, "get_timeseries", entity_id="sensor.temp", start="2d", resolution="hour"
    )
    assert hourly["source"] == "statistics:hour"
    assert hourly["points"][0]["mean"] == 21.0
    daily = await call(
        srv, "get_timeseries", entity_id="binary_sensor.door", start="5d", resolution="day"
    )
    assert daily["source"] == "states"
    assert daily["timezone"] == "Europe/Prague"
    assert daily["points"][-1]["last_state"] == "on"
    with pytest.raises(ToolError, match="Invalid time"):
        await call(srv, "get_timeseries", entity_id="sensor.temp", start="yesterday")


async def test_query_sql_row_level_policy(data: DataAccess) -> None:
    sql = "SELECT domain, count(*) AS n FROM states GROUP BY domain ORDER BY domain"
    admin = await call(server(data, ADMIN), "query_sql", sql=sql)
    reader = await call(server(data, READER), "query_sql", sql=sql)
    assert {r["domain"] for r in admin["rows"]} == {"binary_sensor", "person", "sensor"}
    assert {r["domain"] for r in reader["rows"]} == {"binary_sensor", "sensor"}
    assert reader["columns"] == ["domain", "n"]


async def test_findings_hide_pairs_with_a_denied_entity(data: DataAccess) -> None:
    sql = "SELECT finding_id, related_entity_id FROM catalog_finding ORDER BY rank"
    admin = await call(server(data, ADMIN), "query_sql", sql=sql)
    reader = await call(server(data, READER), "query_sql", sql=sql)
    assert [r["finding_id"] for r in admin["rows"]] == ["f1", "f2"]
    assert [r["finding_id"] for r in reader["rows"]] == ["f1"]


async def test_query_sql_rejections_are_audited(data: DataAccess) -> None:
    audit = MemoryAuditSink()
    srv = server(data, READER, audit)
    for sql, message in [
        ("SELECT * FROM range(5000)", "Table functions"),
        ("SELECT * FROM catalog_drift", "not permitted"),
        ("SELECT nope FROM states", "nope"),
        ("SELECT * FROM read_parquet('/etc/passwd')", "Table functions"),
    ]:
        with pytest.raises(ToolError, match=message):
            await call(srv, "query_sql", sql=sql)
    assert [r.outcome for r in audit.records] == ["error"] * 4
    assert audit.records[0].arguments == {"sql": "SELECT * FROM range(5000)"}

    big = await call(
        srv, "query_sql", sql="SELECT s.* FROM states s, states t, states u LIMIT 5000"
    )
    assert big["truncated"] is True
    assert big["row_count"] == 1000
    assert audit.records[-1].row_count == 1000


async def test_list_views(data: DataAccess) -> None:
    res = await call(server(data, READER), "list_views")
    names = [v["name"] for v in res["views"]]
    assert names == [
        "catalog_entity",
        "catalog_finding",
        "entities",
        "states",
        "statistics",
        "statistics_meta",
    ]
    states = next(v for v in res["views"] if v["name"] == "states")
    assert {"name": "state_value", "type": "DOUBLE"} in states["columns"]


async def test_engine_is_locked_down(data: DataAccess) -> None:
    from hammond_mcp.policy import Access

    access = Access(
        roles=("x",),
        tools=frozenset({"*"}),
        views=frozenset({"*"}),
        max_rows=10,
        known_views=frozenset({"states"}),
    )
    with data._cursor(access) as cur:  # bypass the guard: the engine must still refuse
        for sql in (
            "SELECT * FROM read_csv('/etc/passwd')",
            "SET enable_external_access = true",
            "INSERT INTO lake.silver.states (entity_id) VALUES ('x')",
        ):
            with pytest.raises(Exception):  # noqa: B017, PT011
                cur.execute(sql)


class FailingAudit(MemoryAuditSink):
    async def write(self, record: AuditRecord) -> None:
        raise AuditError("down")


async def test_audit_failure_refuses_the_call(data: DataAccess) -> None:
    with pytest.raises(ToolError, match="Audit is unavailable"):
        await call(server(data, ADMIN, FailingAudit()), "list_views")


async def test_empty_lake(tmp_path: Path) -> None:
    catalog, data_path = str(tmp_path / "c.ducklake"), str(tmp_path / "d") + "/"
    connect_local(catalog, data_path, extension_dir=EXT).close()
    empty = DataAccess.local(catalog, data_path, extension_dir=EXT)
    try:
        srv = server(empty, ADMIN)
        assert (await call(srv, "list_views"))["views"] == []
        with pytest.raises(ToolError, match="catalog_entity"):
            await call(srv, "search_catalog")
    finally:
        empty.close()
