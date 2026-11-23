import contextlib
from datetime import UTC, datetime, timedelta

import duckdb

from hacore import catalog

NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)

ENTITY_ROWS = [
    # entity_id, name, area, unit, device_class, state_class
    ("sensor.temp", "Teplota", "Obývák", "°C", "temperature", "measurement"),
    ("sensor.energy", "Energie", "Technická", "kWh", "energy", "total_increasing"),
    ("binary_sensor.door", "Dveře", "Chodba", None, "door", None),
    ("sensor.mode", "Režim", None, None, None, None),
]


def make_silver(con: duckdb.DuckDBPyConnection, entities: list[tuple[object, ...]]) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS lake.silver")
    con.execute(
        """CREATE OR REPLACE TABLE lake.silver.entities (
        entity_id VARCHAR, domain VARCHAR, name VARCHAR, platform VARCHAR, device_id VARCHAR,
        device_name VARCHAR, manufacturer VARCHAR, model VARCHAR, area_id VARCHAR,
        area_name VARCHAR, floor_id VARCHAR, floor_name VARCHAR, label_ids VARCHAR[],
        unit_of_measurement VARCHAR, device_class VARCHAR, state_class VARCHAR,
        entity_category VARCHAR, disabled BOOLEAN, hidden BOOLEAN, in_registry BOOLEAN,
        has_statistics BOOLEAN)"""
    )
    for eid, name, area, unit, dc, sc in entities:
        con.execute(
            "INSERT INTO lake.silver.entities (entity_id, domain, name, area_name, "
            "unit_of_measurement, device_class, state_class, in_registry, has_statistics) "
            "VALUES (?, split_part(?, '.', 1), ?, ?, ?, ?, ?, TRUE, ?)",
            [eid, eid, name, area, unit, dc, sc, sc is not None],
        )
    con.execute(
        """CREATE TABLE IF NOT EXISTS lake.silver.states (
        entity_id VARCHAR, domain VARCHAR, state VARCHAR, state_value DOUBLE,
        is_missing BOOLEAN, attributes JSON, last_changed TIMESTAMPTZ,
        last_updated TIMESTAMPTZ, context_id VARCHAR, source VARCHAR)"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE lake.silver.statistics_meta (
        statistic_id VARCHAR, source VARCHAR, name VARCHAR, unit_of_measurement VARCHAR,
        unit_class VARCHAR, has_mean BOOLEAN, has_sum BOOLEAN, mean_type INTEGER)"""
    )
    con.execute(
        "INSERT INTO lake.silver.statistics_meta (statistic_id, unit_of_measurement) "
        "VALUES ('sensor.temp', '°C'), ('sensor.energy', 'kWh')"
    )


def add_states(
    con: duckdb.DuckDBPyConnection,
    entity_id: str,
    values: list[str],
    end: datetime,
    step: timedelta,
) -> None:
    rows = []
    for i, v in enumerate(values):
        ts = end - step * (len(values) - 1 - i)
        num = None
        with contextlib.suppress(ValueError):
            num = float(v)
        domain = entity_id.split(".", maxsplit=1)[0]
        rows.append((entity_id, domain, v, num, v == "unavailable", ts))
    con.executemany(
        "INSERT INTO lake.silver.states (entity_id, domain, state, state_value, is_missing, "
        "last_updated) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def seed(con: duckdb.DuckDBPyConnection) -> None:
    make_silver(con, ENTITY_ROWS)
    hour = timedelta(hours=1)
    add_states(con, "sensor.temp", [str(20 + (i % 5)) for i in range(600)], NOW, hour)
    add_states(con, "sensor.energy", [str(i) for i in range(100)], NOW, hour)
    add_states(con, "binary_sensor.door", ["on", "off"] * 30, NOW, hour)
    add_states(con, "sensor.mode", ["eco", "comfort", "away", "unavailable"] * 10, NOW, hour)


def entity(con: duckdb.DuckDBPyConnection, entity_id: str) -> dict[str, object]:
    cur = con.execute("SELECT * FROM lake.catalog.entity WHERE entity_id = ?", [entity_id])
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(cols, row, strict=True))


def test_profile_kinds_and_stats(lake: duckdb.DuckDBPyConnection) -> None:
    seed(lake)
    result = catalog.profile(lake, now=NOW)
    assert result.entities == 4
    assert result.drift == {}  # first run records no drift

    temp = entity(lake, "sensor.temp")
    assert temp["kind"] == "measurement"
    assert temp["value_min"] == 20.0
    assert temp["value_max"] == 24.0
    assert temp["statistics_unit"] == "°C"
    assert temp["stale"] is False
    assert temp["changes"] == 600

    assert entity(lake, "sensor.energy")["kind"] == "counter"
    assert entity(lake, "binary_sensor.door")["kind"] == "binary"
    mode = entity(lake, "sensor.mode")
    assert mode["kind"] == "enum"
    assert mode["missing_share"] == 0.25
    assert mode["top_states"][0]["n"] == 10


def test_drift_between_profiles(lake: duckdb.DuckDBPyConnection) -> None:
    seed(lake)
    catalog.profile(lake, now=NOW)

    # unit change, entity removed, new entity, door stopped reporting
    changed = [
        ("sensor.temp", "Teplota", "Obývák", "°F", "temperature", "measurement"),
        ("sensor.energy", "Energie", "Technická", "kWh", "energy", "total_increasing"),
        ("binary_sensor.door", "Dveře", "Chodba", None, "door", None),
        ("sensor.new", "Nový", None, None, None, None),
    ]
    make_silver(lake, changed)
    lake.execute("DELETE FROM lake.silver.states WHERE entity_id = 'sensor.mode'")
    later = NOW + timedelta(days=3)
    add_states(
        lake, "sensor.temp", [str(80 + (i % 3)) for i in range(150)], later, timedelta(minutes=30)
    )
    add_states(lake, "sensor.new", ["1", "2"], later, timedelta(hours=1))

    result = catalog.profile(lake, now=later)
    assert result.drift["new_entity"] == 1
    assert result.drift["removed_entity"] == 1
    assert result.drift["stopped_reporting"] == 2  # door, energy: nothing for 3 days
    assert result.drift["range_shift"] == 1  # temp: recent median far above the 5-95 % range

    rows = lake.execute(
        "SELECT entity_id, field, old_value, new_value FROM lake.catalog.drift "
        "WHERE drift_type = 'metadata_change' ORDER BY ALL"
    ).fetchall()
    assert ("sensor.temp", "unit_of_measurement", "°C", "°F") in rows

    # an unchanged third run records no new drift
    again = catalog.profile(lake, now=later + timedelta(minutes=1))
    assert "metadata_change" not in again.drift
    assert "range_shift" not in again.drift
