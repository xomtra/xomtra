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
    assert {
        "ingest_hourly",
        "retention_daily",
        "catalog_daily",
        "findings_weekly",
        "history_backfill",
        "silver_restate",
    } <= jobs
    schedules = {s.job_name: s for s in defs.schedules or []}  # type: ignore[union-attr]
    assert schedules["ingest_hourly"].execution_timezone == "Europe/Prague"
    assert schedules["catalog_daily"].cron_schedule == "15 4 * * *"
    assert schedules["findings_weekly"].cron_schedule == "0 7 * * 1"


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


def test_catalog_daily_runs_correlations_after_profile() -> None:
    defs = build_definitions(IngestSettings())
    job = defs.resolve_job_def("catalog_daily")
    deps = job.graph.dependencies
    assert {n.name for n in job.graph.node_defs} == {
        "profile_catalog",
        "sync_outside_temperature",
        "find_correlations",
        "find_anomalies",
    }
    assert any("find_correlations" in str(k) for k in deps)
    assert any("find_anomalies" in str(k) for k in deps)


def test_correlation_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    s = IngestSettings()
    assert s.correlation_windows_days == [365, 30]
    assert s.correlation_outside_temp == ""
    monkeypatch.setenv("HAMMOND_CORRELATION_WINDOWS_DAYS", "[30, 90, 30]")
    monkeypatch.setenv("HAMMOND_CORRELATION_OUTSIDE_TEMP", "sensor.outdoor")
    s = IngestSettings()
    assert s.correlation_windows_days == [90, 30]
    assert s.correlation_outside_temp == "sensor.outdoor"
    with pytest.raises(ValueError, match=r"7\.\.1825"):
        IngestSettings(correlation_windows_days=[3])


def test_cli_correlations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tasks, "find_correlations", lambda settings: [{"window_days": 30}])
    assert main(["correlations"]) == 0
    assert '"window_days": 30' in capsys.readouterr().out


def test_outside_temperature_off_by_default() -> None:
    assert tasks.sync_outside_temperature(IngestSettings()) == {
        "skipped": "HAMMOND_OPEN_METEO_ENABLED is off"
    }
    with pytest.raises(ValueError, match="https"):
        IngestSettings(open_meteo_url="http://example.org")


def test_cli_anomalies_and_weekly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tasks, "find_anomalies", lambda settings: {"anomalies": 3})
    monkeypatch.setattr(tasks, "snapshot_findings", lambda settings: {"findings": 5})
    assert main(["anomalies"]) == 0
    assert '"anomalies": 3' in capsys.readouterr().out
    assert main(["findings-weekly"]) == 0
    assert '"findings": 5' in capsys.readouterr().out


def test_anomalies_can_be_switched_off() -> None:
    assert tasks.find_anomalies(IngestSettings(anomalies_enabled=False)) == {
        "skipped": "HAMMOND_ANOMALIES_ENABLED is off"
    }
