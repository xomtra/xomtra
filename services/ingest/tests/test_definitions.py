from datetime import UTC, datetime, timedelta

import pytest
from dagster import SensorResult, SkipReason, build_sensor_context

from hammond_ingest import tasks
from hammond_ingest.__main__ import main
from hammond_ingest.definitions import build_definitions, restate_after_catchup
from hammond_ingest.settings import IngestSettings


def test_definitions_load_with_schedules() -> None:
    defs = build_definitions(IngestSettings(timezone="Europe/Prague"))
    jobs = {j.name for j in defs.resolve_all_job_defs()}
    assert {"ingest_hourly", "retention_daily", "history_backfill", "silver_restate"} <= jobs
    schedules = {s.job_name: s for s in defs.schedules or []}  # type: ignore[union-attr]
    assert schedules["ingest_hourly"].execution_timezone == "Europe/Prague"


def test_restate_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    gaps = [(start, start + timedelta(hours=5), 10), (start, start + timedelta(hours=8), 20)]
    monkeypatch.setattr(
        tasks, "pending_gaps", lambda after: [g for g in gaps if g[2] > (after or 0)]
    )

    result = restate_after_catchup(build_sensor_context())
    assert isinstance(result, SensorResult)
    assert result.cursor == "20"
    [request] = result.run_requests or []
    assert request.run_key == "catchup-20"
    config = request.run_config["ops"]["restate_silver"]["config"]
    assert config == {"start": start.isoformat(), "end": (start + timedelta(hours=8)).isoformat()}

    skipped = restate_after_catchup(build_sensor_context(cursor="20"))
    assert isinstance(skipped, SkipReason)


def test_parse_time() -> None:
    assert tasks.parse_time("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)
    rel = tasks.parse_time("3d")
    assert abs((datetime.now(UTC) - rel) - timedelta(days=3)) < timedelta(seconds=5)


def test_settings_defaults_and_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMMOND_OPS_PG_PASSWORD", "p@ss/word")
    s = IngestSettings()
    assert s.states_retention_days == 365
    assert s.statistics_5min_retention_days == 365
    assert s.statistics_hour_retention_days == 1825
    assert s.ops_pg_uri("dagster").endswith(":p%40ss%2Fword@postgres:5432/dagster")
    with pytest.raises(ValueError, match="HAMMOND_HA_URL"):
        IngestSettings(ha_url="").require_ha()


def test_cli_requires_command() -> None:
    with pytest.raises(SystemExit):
        main([])
