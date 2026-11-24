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
