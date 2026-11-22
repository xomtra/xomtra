import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hacore import bronze
from hacore.history import StateRow, StatisticRow, iso_to_us
from hacore.lake import LakeSettings, connect_local, install_extensions
from hammond_ingest import transform

EXT = os.environ.get("HAMMOND_DUCKDB_EXTENSION_DIR") or None


def us(dt: datetime) -> int:
    return iso_to_us(dt.isoformat())


@pytest.fixture
def lake_paths(tmp_path: Path) -> tuple[str, str, str]:
    install_extensions(EXT)
    return str(tmp_path / "c.ducklake"), str(tmp_path / "data") + "/", str(tmp_path / "s.db")


def seed(catalog: str, data: str) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    t1, t2 = now - timedelta(hours=5), now - timedelta(hours=4)
    con = connect_local(catalog, data, extension_dir=EXT)
    try:
        bronze.ensure_schema(con)
        bronze.write_states(
            con,
            [
                StateRow("sensor.t", "21.5", {"unit_of_measurement": "°C"}, us(t1), us(t1)),
                StateRow("sensor.t", "unavailable", None, us(t2), us(t2)),
            ],
            source="t",
        )
        bronze.write_statistics(
            con, [StatisticRow("sensor.t", "hour", us(t1), mean=21.0)], source="t"
        )
        bronze.write_registry(
            con, "areas", [{"area_id": "kitchen", "name": "Kitchen", "floor_id": "g"}], source="t"
        )
        bronze.write_registry(con, "floors", [{"floor_id": "g", "name": "Ground"}], source="t")
        bronze.write_registry(
            con, "devices", [{"id": "d1", "area_id": "kitchen", "labels": ["x"]}], source="t"
        )
        bronze.write_registry(
            con,
            "entities",
            [{"id": "e1", "entity_id": "sensor.t", "device_id": "d1", "labels": ["y"]}],
            source="t",
        )
        bronze.write_registry(
            con,
            "entity_attributes",
            [{"entity_id": "sensor.t", "unit_of_measurement": "°C", "device_class": "temperature"}],
            source="t",
        )
        bronze.write_registry(con, "statistics_meta", [{"statistic_id": "sensor.t"}], source="t")
    finally:
        con.close()


def test_silver_models_build_on_ducklake(lake_paths: tuple[str, str, str]) -> None:
    catalog, data, state = lake_paths
    seed(catalog, data)
    config = transform.local_config(catalog, data, state, EXT)
    result = transform.apply_and_run(config)
    assert result.applied
    now = datetime.now(UTC)
    transform.restate(now - timedelta(days=1), now, config)

    con = connect_local(catalog, data, extension_dir=EXT)
    try:
        states = con.execute(
            "SELECT state, state_value, is_missing FROM lake.silver.states ORDER BY last_updated"
        ).fetchall()
        assert states == [("21.5", 21.5, False), ("unavailable", None, True)]
        entity = con.execute(
            "SELECT area_name, floor_name, unit_of_measurement, device_class, has_statistics, "
            "list_sort(label_ids) FROM lake.silver.entities WHERE entity_id = 'sensor.t'"
        ).fetchone()
        assert entity == ("Kitchen", "Ground", "°C", "temperature", True, ["x", "y"])
        assert con.execute("SELECT count(*) FROM lake.silver.statistics").fetchone() == (1,)
    finally:
        con.close()


def test_catalog_uri_has_no_quotes() -> None:
    settings = LakeSettings(
        pg_host="db",
        pg_db="duck lake",
        pg_user="u'x",
        pg_password="p'@:/w",
        s3_endpoint="s3:8333",
        s3_access_key="a",
        s3_secret_key="s'k",
        s3_bucket="b",
    )
    uri = transform.catalog_uri(settings)
    assert "'" not in uri
    assert uri.startswith("postgres:postgresql://u%27x:p%27%40%3A%2Fw@db:5432/duck%20lake")
    conn = transform.lake_connection(settings)
    assert isinstance(conn.secrets, dict)
    assert conn.secrets["hammond_s3"]["secret"] == "s''k"
