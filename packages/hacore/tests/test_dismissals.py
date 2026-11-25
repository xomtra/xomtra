from datetime import UTC, datetime

import duckdb

from hacore import correlations as corr
from hacore import findings
from hacore.findings import Dismissal

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def lake() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lake")
    con.execute(f"CREATE SCHEMA lake.{corr.SCHEMA}")
    base = {"metric": "mean", "state": None, "lag_hours": 0, "score": 0.6, "q_value": 1e-4}
    corr.write_findings(
        con,
        [
            {
                **base,
                "finding_id": "c1",
                "entity_id": "sensor.a",
                "related_entity_id": "sensor.b",
                "rank": 1,
                "flags": [],
            },
            {
                **base,
                "finding_id": "c2",
                "entity_id": "sensor.a",
                "related_entity_id": "sensor.c",
                "rank": 2,
                "flags": ["same_area"],
            },
        ],
        window_days=30,
        period_start=None,
        period_end=None,
        method="test",
        now=NOW,
        alias="lake",
    )
    corr.write_findings(
        con,
        [{**base, "finding_id": "a1", "entity_id": "sensor.b", "rank": 1, "flags": []}],
        window_days=63,
        period_start=None,
        period_end=None,
        method="test",
        now=NOW,
        finding_type="anomaly",
        alias="lake",
    )
    return con


def flags(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    rows = con.execute("SELECT finding_id, flags FROM lake.catalog.finding").fetchall()
    return {r[0]: list(r[1]) for r in rows}


def test_dismissals_flag_pairs_in_either_order_and_entities() -> None:
    con = lake()
    out = [
        Dismissal("correlation", "sensor.b", "sensor.a"),  # reversed pair
        Dismissal("anomaly", "sensor.b"),
        Dismissal("change_point", "sensor.a"),  # no such finding
    ]
    assert findings.apply_dismissals(con, out, alias="lake") == 2
    f = flags(con)
    assert f["c1"] == ["dismissed"]
    assert f["c2"] == ["same_area"]
    assert f["a1"] == ["dismissed"]
    assert findings.apply_dismissals(con, out, alias="lake") == 0  # idempotent
    assert findings.apply_dismissals(con, [], alias="lake") == 0
