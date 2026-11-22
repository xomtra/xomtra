from datetime import UTC, datetime, timedelta

import duckdb

from hacore import bronze, maintenance
from hacore.history import StateRow, StatisticRow, iso_to_us

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def at(days_ago: int) -> int:
    return iso_to_us((NOW - timedelta(days=days_ago)).isoformat())


def test_retention_and_maintenance(lake: duckdb.DuckDBPyConnection) -> None:
    states = [StateRow("sensor.a", str(d), None, at(d), at(d)) for d in (1, 100, 400, 800)]
    bronze.write_states(lake, states, source="t")
    bronze.write_statistics(
        lake,
        [StatisticRow("sensor.a", "hour", at(d)) for d in (10, 500, 2000)]
        + [StatisticRow("sensor.a", "5minute", at(d)) for d in (10, 500)],
        source="t",
    )
    result = maintenance.apply_retention(lake, now=NOW)  # defaults: 365 d, 365 d, 5 years
    # states 400 + 800 days, hourly 2000 days, 5-minute 500 days
    assert result.deleted == {"bronze.ha_states": 2, "bronze.ha_statistics": 2}
    left_stats = lake.execute(
        "SELECT period, count(*) FROM lake.bronze.ha_statistics GROUP BY ALL ORDER BY 1"
    ).fetchall()
    assert left_stats == [("5minute", 1), ("hour", 2)]
    maintenance.compact_and_expire(lake, timedelta(0))
    left = lake.execute("SELECT count(*) FROM lake.bronze.ha_states").fetchone()
    assert left == (2,)
