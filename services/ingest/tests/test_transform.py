import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmesh.utils import CompletionStatus

from hacore import bronze
from hacore import catalog as entity_catalog
from hacore.history import StateRow, StatisticRow, iso_to_us
from hacore.lake import LAKE_ALIAS, LakeSettings, connect_local, install_extensions
from xomtra_ingest import transform

EXT = os.environ.get("XOMTRA_DUCKDB_EXTENSION_DIR") or None


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
        # Coarse partitions, never one per timestamp (the SQLMesh default for the time
        # column, which produced a directory per row on a real installation).
        partitions = {
            row[0]: row[1]
            for row in con.execute(
                "SELECT t.table_name, string_agg(c.column_name || ':' || p.transform, ',') "
                "FROM __ducklake_metadata_lake.ducklake_partition_column p "
                "JOIN __ducklake_metadata_lake.ducklake_table t USING (table_id) "
                "JOIN __ducklake_metadata_lake.ducklake_column c "
                "  ON c.table_id = p.table_id AND c.column_id = p.column_id "
                "WHERE t.end_snapshot IS NULL AND c.end_snapshot IS NULL "
                "  AND t.table_name LIKE 'silver__%' GROUP BY ALL"
            ).fetchall()
        }
        timestamp_identity = ("last_updated:identity", "period_start:identity")
        assert not any(t in v for v in partitions.values() for t in timestamp_identity), partitions
        assert any(k.startswith("silver__states__") for k in partitions), partitions

        # The catalog profile runs on the real silver views (column names and types).
        profiled = entity_catalog.profile(con)
        assert profiled.entities == 1
        kind = con.execute(
            "SELECT kind, area_name, statistics_unit FROM lake.catalog.entity"
        ).fetchone()
        assert kind == ("measurement", "Kitchen", None)
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
    assert conn.secrets["xomtra_s3"]["secret"] == "s''k"


def test_local_lake_has_no_s3_secret() -> None:
    settings = LakeSettings(
        pg_host="db", pg_db="ducklake", pg_user="u", pg_password="p", local_data_path="/data/lake"
    )
    conn = transform.lake_connection(settings)
    assert not conn.secrets
    assert conn.catalogs[LAKE_ALIAS].data_path == "/data/lake/"


class FakeContext:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.execution_time: datetime | None = None

    def run(self, *, execution_time: datetime) -> CompletionStatus:
        self.execution_time = execution_time
        if not self.fail:
            return CompletionStatus.SUCCESS
        error = RuntimeError("Execution failed for node silver.entities")
        error.__cause__ = ValueError("audit unique_values failed")
        logging.getLogger("sqlmesh.core.scheduler").info(str(error), exc_info=error)
        return CompletionStatus.FAILURE


def test_run_failure_names_the_model_and_the_cause() -> None:
    when = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)
    ctx = FakeContext(fail=True)
    with pytest.raises(transform.TransformError, match=r"silver\.entities.*audit unique_values"):
        transform._run(ctx, when)  # type: ignore[arg-type]
    assert ctx.execution_time == when
    log = logging.getLogger("sqlmesh.core.scheduler")
    assert not log.handlers
    ok = FakeContext(fail=False)
    assert transform._run(ok, when) is CompletionStatus.SUCCESS  # type: ignore[arg-type]
