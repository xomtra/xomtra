from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from hacore import correlations as corr
from hacore import findings

NOW = datetime(2026, 6, 3, 5, 0, tzinfo=UTC)  # a Wednesday


@pytest.fixture
def plain() -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("ATTACH ':memory:' AS lake")
    try:
        yield con
    finally:
        con.close()


def write(con: duckdb.DuckDBPyConnection, ids: list[str], now: datetime) -> None:
    rows = [
        {"finding_id": i, "entity_id": "sensor.a", "related_entity_id": "sensor.b", "rank": n}
        for n, i in enumerate(ids, start=1)
    ]
    corr.write_findings(
        con,
        rows,
        window_days=30,
        period_start=now - timedelta(days=30),
        period_end=now,
        method="test",
        now=now,
    )


def test_last_week_start() -> None:
    assert findings.last_week_start(NOW, "Europe/Prague") == date(2026, 5, 25)
    monday_early = datetime(2026, 6, 7, 22, 30, tzinfo=UTC)  # Monday 00:30 in Prague
    assert findings.last_week_start(monday_early, "Europe/Prague") == date(2026, 6, 1)
    assert findings.last_week_start(monday_early, "UTC") == date(2026, 5, 25)


def test_snapshot_replaces_its_week_and_drops_old(plain: duckdb.DuckDBPyConnection) -> None:
    write(plain, ["a", "b"], NOW)
    first = findings.snapshot_week(plain, now=NOW)
    assert first.findings == 2
    assert first.by_type == {"correlation": 2}
    write(plain, ["a", "b", "c"], NOW + timedelta(hours=1))
    again = findings.snapshot_week(plain, now=NOW + timedelta(hours=1))
    assert again.week_start == first.week_start
    assert again.findings == 3
    later = NOW + timedelta(weeks=3)
    findings.snapshot_week(plain, now=later, keep_weeks=2)
    weeks = plain.execute(
        "SELECT week_start, count(*) FROM lake.catalog.finding_weekly GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert weeks == [(findings.last_week_start(later, "UTC"), 3)]


PROVENANCE = ("analyzer", "pack", "pack_version", "source_instance")


def test_findings_record_their_provenance(plain: duckdb.DuckDBPyConnection) -> None:
    write(plain, ["c1"], NOW)
    corr.write_findings(
        plain,
        [{"finding_id": "a1", "entity_id": "sensor.a", "rank": 1}],
        window_days=7,
        period_start=NOW - timedelta(days=7),
        period_end=NOW,
        method="test",
        now=NOW,
        finding_type="anomaly",
        analyzer="anomalies",
    )
    rows = plain.execute(
        "SELECT finding_id, analyzer, pack, pack_version, source_instance "
        "FROM lake.catalog.finding ORDER BY finding_id"
    ).fetchall()
    assert [r[:3] for r in rows] == [("a1", "anomalies", "home"), ("c1", "correlations", "home")]
    assert all(r[3] and r[4] == "ha" for r in rows)

    findings.snapshot_week(plain, now=NOW)
    weekly = plain.execute(
        "SELECT analyzer, pack, source_instance FROM lake.catalog.finding_weekly "
        "ORDER BY finding_id"
    ).fetchall()
    assert weekly == [("anomalies", "home", "ha"), ("correlations", "home", "ha")]


def test_provenance_columns_are_added_to_existing_tables(
    plain: duckdb.DuckDBPyConnection,
) -> None:
    old = ",\n".join(
        line.rstrip(",")
        for line in corr.FINDING_COLUMNS.strip().splitlines()
        if line.strip().split(" ")[0] not in PROVENANCE
    )
    plain.execute("CREATE SCHEMA lake.catalog")
    plain.execute(f"CREATE TABLE lake.catalog.finding ({old})")
    plain.execute(
        "INSERT INTO lake.catalog.finding "
        "(finding_id, finding_type, entity_id, window_days, detected_at) "
        "VALUES ('old', 'correlation', 'sensor.a', 30, $now)",
        {"now": NOW - timedelta(days=1)},
    )
    plain.execute(
        "CREATE TABLE lake.catalog.finding_weekly "
        f"(week_start DATE NOT NULL, snapshot_at TIMESTAMPTZ NOT NULL, {old})"
    )
    findings.ensure_schema(plain)
    for table in ("finding", "finding_weekly"):
        cols = {
            r[0]
            for r in plain.execute(
                "SELECT column_name FROM duckdb_columns() WHERE database_name = 'lake' "
                "AND table_name = ?",
                [table],
            ).fetchall()
        }
        assert set(PROVENANCE) <= cols
    write(plain, ["new"], NOW)
    assert plain.execute(
        "SELECT finding_id, analyzer, source_instance FROM lake.catalog.finding"
    ).fetchall() == [("new", "correlations", "ha")]
