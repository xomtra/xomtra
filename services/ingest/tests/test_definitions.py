from datetime import UTC, datetime, timedelta

import pytest

from hammond_ingest import tasks
from hammond_ingest.__main__ import main
from hammond_ingest.definitions import build_definitions
from hammond_ingest.settings import IngestSettings


def test_definitions_load_with_schedules() -> None:
    defs = build_definitions(IngestSettings(timezone="Europe/Prague"))
    jobs = {j.name for j in defs.resolve_all_job_defs()}
    assert {"ingest_hourly", "retention_daily", "history_backfill", "recorder_import"} <= jobs
    schedules = {s.job_name: s for s in defs.schedules or []}  # type: ignore[union-attr]
    assert schedules["ingest_hourly"].execution_timezone == "Europe/Prague"


def test_parse_time() -> None:
    assert tasks.parse_time("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)
    rel = tasks.parse_time("3d")
    assert abs((datetime.now(UTC) - rel) - timedelta(days=3)) < timedelta(seconds=5)


def test_settings_defaults_and_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAMMOND_OPS_PG_PASSWORD", "p@ss/word")
    s = IngestSettings()
    assert s.retention_days == 365
    assert s.ops_pg_uri("dagster").endswith(":p%40ss%2Fword@postgres:5432/dagster")
    with pytest.raises(ValueError, match="HAMMOND_HA_URL"):
        IngestSettings(ha_url="").require_ha()


def test_cli_requires_command() -> None:
    with pytest.raises(SystemExit):
        main([])
