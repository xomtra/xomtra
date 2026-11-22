import duckdb

from hacore import bronze
from hacore.history import StateRow, StatisticRow, iso_to_us

T0 = iso_to_us("2026-01-01T10:00:00.123456+00:00")


def state(entity_id: str, offset_s: float, value: str, **attrs: object) -> StateRow:
    us = T0 + round(offset_s * 1_000_000)
    return StateRow(entity_id, value, dict(attrs) or None, us, us)


def count(con: duckdb.DuckDBPyConnection, name: str) -> int:
    row = con.execute(f"SELECT count(*) FROM {bronze.table(name)}").fetchone()
    assert row is not None
    return int(row[0])


def test_ensure_schema_is_idempotent(lake: duckdb.DuckDBPyConnection) -> None:
    bronze.ensure_schema(lake)
    tables = lake.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'bronze' ORDER BY 1"
    ).fetchall()
    assert [t[0] for t in tables] == ["ha_registry", "ha_states", "ha_statistics"]


def test_write_states_is_idempotent(lake: duckdb.DuckDBPyConnection) -> None:
    rows = [state("sensor.a", 0, "1.5", unit_of_measurement="°C"), state("sensor.a", 60, "2")]
    assert bronze.write_states(lake, rows, source="history") == 2
    # Overlapping batch (catch-up after reconnect): only the new row is inserted.
    assert bronze.write_states(lake, [*rows, state("sensor.a", 120, "3")], source="live") == 1
    # Duplicates inside one batch are collapsed.
    assert bronze.write_states(lake, [state("sensor.b", 0, "x")] * 3, source="live") == 1
    assert count(lake, bronze.STATES) == 4
    row = lake.execute(
        "SELECT state, attributes->>'unit_of_measurement', epoch_us(last_updated), source "
        "FROM lake.bronze.ha_states WHERE entity_id = 'sensor.a' ORDER BY last_updated LIMIT 1"
    ).fetchone()
    assert row == ("1.5", "°C", T0, "history")
    assert bronze.state_watermark_us(lake) == T0 + 120_000_000


def test_write_states_handles_nan_and_empty(lake: duckdb.DuckDBPyConnection) -> None:
    assert bronze.write_states(lake, [], source="live") == 0
    assert bronze.write_states(lake, [state("sensor.c", 0, "1", x=float("nan"))], source="t") == 1


def test_write_statistics(lake: duckdb.DuckDBPyConnection) -> None:
    rows = [
        StatisticRow("sensor.a", "hour", T0, mean=1.0, min=0.5, max=2.0),
        StatisticRow("sensor.a", "5minute", T0, mean=1.0),
        StatisticRow("sensor.e", "hour", T0, state=10.0, sum=3.5, last_reset_us=T0),
    ]
    assert bronze.write_statistics(lake, rows, source="api") == 3
    assert bronze.write_statistics(lake, rows, source="api") == 0
    assert bronze.statistics_watermark_us(lake, "hour") == T0
    assert bronze.statistics_watermark_us(lake, "5minute") == T0


def test_write_registry_change_log(lake: duckdb.DuckDBPyConnection) -> None:
    areas = [{"area_id": "kitchen", "name": "Kitchen"}, {"area_id": "hall", "name": "Hall"}]
    assert bronze.write_registry(lake, "areas", areas, source="api") == 2
    assert bronze.write_registry(lake, "areas", areas, source="api") == 0
    changed = [{"area_id": "kitchen", "name": "Kitchen 2"}]
    # kitchen changed, hall removed
    assert bronze.write_registry(lake, "areas", changed, source="api") == 2
    removed = lake.execute("SELECT item_id FROM lake.bronze.ha_registry WHERE removed").fetchall()
    assert removed == [("hall",)]
    # hall comes back
    assert bronze.write_registry(lake, "areas", [*changed, areas[1]], source="api") == 1
    # only_new never overrides known items
    old = [{"area_id": "kitchen", "name": "Old"}, {"area_id": "attic", "name": "Attic"}]
    assert bronze.write_registry(lake, "areas", old, source="backup", only_new=True) == 1
