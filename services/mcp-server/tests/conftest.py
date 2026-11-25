import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from hacore import catalog, confidence, correlations
from hacore import findings as findings_mod
from hacore.context import Context
from hacore.lake import connect_local, install_extensions
from xomtra_mcp.data import DataAccess

TZ = "Europe/Prague"
EXT = os.environ.get("XOMTRA_DUCKDB_EXTENSION_DIR") or None
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
        # One correlation with a visible pair and one with a hidden related entity.
        base = {"metric": "mean", "state": None, "related_metric": "share", "lag_hours": 0}
        findings = [
            {
                **base,
                "finding_id": "f1",
                "entity_id": "sensor.temp",
                "related_entity_id": "binary_sensor.door",
                "related_state": "on",
                "score": 0.6,
                "q_value": 1e-6,
                "n_eff": 500,
                "rank": 1,
                "flags": [],
            },
            {
                **base,
                "finding_id": "f2",
                "entity_id": "sensor.temp",
                "related_entity_id": "person.jana",
                "related_state": "home",
                "score": 0.5,
                "q_value": 1e-5,
                "n_eff": 500,
                "rank": 2,
                "flags": [],
            },
        ]
        # A week earlier: f1 and f9, snapshot of that week.
        earlier = NOW - timedelta(days=7)
        gone = {**findings[0], "finding_id": "f9", "related_entity_id": "sensor.energy"}
        correlations.write_findings(
            con,
            [findings[0], gone],
            window_days=30,
            period_start=earlier - timedelta(days=30),
            period_end=earlier,
            method="test",
            now=earlier,
        )
        findings_mod.snapshot_week(con, timezone=TZ, now=earlier)
        findings[0]["entity_group"] = ["sensor.temp_copy", "person.jana"]
        trivial = {
            **base,
            "finding_id": "f3",
            "entity_id": "sensor.temp",
            "related_entity_id": "sensor.energy",
            "related_metric": "delta",
            "score": 0.9,
            "rank": 3,
            "flags": ["same_device"],
        }
        correlations.write_findings(
            con,
            [*findings, trivial],
            window_days=30,
            period_start=NOW - timedelta(days=30),
            period_end=NOW,
            method="test",
            now=NOW,
        )
        # Confidence of the 30-day correlation run (ADR 067): a small installation.
        correlations.write_run(
            con,
            finding_type="correlation",
            window_days=30,
            now=NOW,
            result=correlations.CorrelationResult(window_days=30, series_used=4, findings=3),
            conf=confidence.assess(
                ["sensor.temp", "sensor.energy", "binary_sensor.door", "person.jana"],
                Context(groups={"sensor.temp": "living"}),
            ),
        )
        week = datetime.combine(
            findings_mod.last_week_start(NOW, TZ), datetime.min.time(), ZoneInfo(TZ)
        )
        anomaly = {
            "entity_id": "sensor.energy",
            "metric": "delta",
            "score": 6.5,
            "n_eff": 3,
            "observed": 5.0,
            "expected": 1.0,
            "period_start": week + timedelta(days=2),
            "period_end": week + timedelta(days=2, hours=3),
        }
        correlations.write_findings(
            con,
            [
                {**anomaly, "finding_id": "a1", "rank": 1, "flags": []},
                {
                    **anomaly,
                    "finding_id": "a2",
                    "rank": 2,
                    "score": -4.2,
                    "n_eff": 1,
                    "flags": ["short"],
                },
                {
                    **anomaly,
                    "finding_id": "a3",
                    "rank": 3,
                    "entity_id": "person.jana",
                    "metric": "share",
                    "state": "home",
                },
            ],
            window_days=7,
            period_start=None,
            period_end=None,
            method="anomaly test",
            now=NOW,
            finding_type="anomaly",
        )
        findings_mod.snapshot_week(con, timezone=TZ, now=NOW)
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
