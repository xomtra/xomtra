import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hacore import catalog
from hacore.lake import connect_local, install_extensions
from hammond_mcp.data import DataAccess

EXT = os.environ.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None
NOW = datetime.now(UTC).replace(microsecond=0)

ENTITIES = [
    # entity_id, name, area, unit, device_class, state_class
    ("sensor.temp", "Teplota obývák", "Obývák", "°C", "temperature", "measurement"),
    ("sensor.energy", "Energie", "Technická", "kWh", "energy", "total_increasing"),
    ("binary_sensor.door", "Vstupní dveře", "Chodba", None, "door", None),
    ("person.jana", "Jana", None, None, None, None),
    ("device_tracker.phone", "Telefon", None, None, None, None),
]


def _seed(catalog_path: str, data_path: str) -> None:
    con = connect_local(catalog_path, data_path, extension_dir=EXT)
    try:
        con.execute("CREATE SCHEMA lake.silver")
        con.execute(
            """CREATE TABLE lake.silver.entities (
            entity_id VARCHAR, domain VARCHAR, name VARCHAR, platform VARCHAR,
            device_id VARCHAR, device_name VARCHAR, manufacturer VARCHAR, model VARCHAR,
            area_id VARCHAR, area_name VARCHAR, floor_id VARCHAR, floor_name VARCHAR,
            label_ids VARCHAR[], unit_of_measurement VARCHAR, device_class VARCHAR,
            state_class VARCHAR, entity_category VARCHAR, disabled BOOLEAN, hidden BOOLEAN,
            in_registry BOOLEAN, has_statistics BOOLEAN)"""
        )
        for eid, name, area, unit, dc, sc in ENTITIES:
            con.execute(
                "INSERT INTO lake.silver.entities (entity_id, domain, name, area_name, "
                "unit_of_measurement, device_class, state_class, in_registry, disabled) "
                "VALUES (?, split_part(?, '.', 1), ?, ?, ?, ?, ?, TRUE, FALSE)",
                [eid, eid, name, area, unit, dc, sc],
            )
        con.execute(
            """CREATE TABLE lake.silver.states (
            entity_id VARCHAR, domain VARCHAR, state VARCHAR, state_value DOUBLE,
            is_missing BOOLEAN, attributes JSON, last_changed TIMESTAMPTZ,
            last_updated TIMESTAMPTZ, context_id VARCHAR, source VARCHAR)"""
        )
        rows = []
        for i in range(72):
            ts = NOW - timedelta(hours=72 - i)
            rows.append(("sensor.temp", "sensor", str(20 + i % 4), 20.0 + i % 4, False, ts))
            rows.append(("person.jana", "person", "home" if i % 2 else "away", None, False, ts))
        rows.append(("binary_sensor.door", "binary_sensor", "on", None, False, NOW))
        con.executemany(
            "INSERT INTO lake.silver.states (entity_id, domain, state, state_value, is_missing, "
            "last_updated, last_changed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*r, r[-1]) for r in rows],
        )
        con.execute(
            """CREATE TABLE lake.silver.statistics (
            statistic_id VARCHAR, period VARCHAR, period_start TIMESTAMPTZ, mean DOUBLE,
            min DOUBLE, max DOUBLE, state DOUBLE, sum DOUBLE, last_reset TIMESTAMPTZ,
            source VARCHAR)"""
        )
        hour = NOW.replace(minute=0, second=0)
        con.executemany(
            "INSERT INTO lake.silver.statistics (statistic_id, period, period_start, mean, "
            "min, max) VALUES ('sensor.temp', 'hour', ?, ?, ?, ?)",
            [(hour - timedelta(hours=h), 21.0, 20.0, 23.0) for h in range(1, 49)],
        )
        con.execute(
            """CREATE TABLE lake.silver.statistics_meta (
            statistic_id VARCHAR, source VARCHAR, name VARCHAR, unit_of_measurement VARCHAR,
            unit_class VARCHAR, has_mean BOOLEAN, has_sum BOOLEAN, mean_type INTEGER)"""
        )
        con.execute(
            "INSERT INTO lake.silver.statistics_meta (statistic_id, unit_of_measurement, "
            "has_mean) VALUES ('sensor.temp', '°C', TRUE)"
        )
        catalog.profile(con, now=NOW)
    finally:
        con.close()


@pytest.fixture(scope="session")
def lake_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    install_extensions(EXT)
    base: Path = tmp_path_factory.mktemp("lake")
    paths = str(base / "c.ducklake"), str(base / "data") + "/"
    _seed(*paths)
    return paths


@pytest.fixture
def data(lake_paths: tuple[str, str]) -> Iterator[DataAccess]:
    access = DataAccess.local(*lake_paths, extension_dir=EXT, timezone="Europe/Prague")
    try:
        yield access
    finally:
        access.close()
