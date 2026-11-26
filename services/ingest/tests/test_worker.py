import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from xomtra_ingest import worker
from xomtra_ingest.settings import IngestSettings

PRAGUE = ZoneInfo("Europe/Prague")


def test_schedules_match_dagster() -> None:
    from xomtra_ingest.definitions import build_definitions

    defs = build_definitions(IngestSettings())
    crons = {s.job.name: s.cron_schedule for s in defs.schedules}
    for s in worker.SCHEDULES:
        hour = "*" if s.hour is None else str(s.hour)
        weekday = "*" if s.weekday is None else str((s.weekday + 1) % 7)
        assert crons[s.job] == f"{s.minute} {hour} * * {weekday}"
    assert set(worker.JOBS) == {s.job for s in worker.SCHEDULES}


def test_next_and_previous() -> None:
    hourly, retention, _, weekly = worker.SCHEDULES
    t = datetime(2026, 11, 25, 3, 40, tzinfo=PRAGUE)  # Wednesday
    assert hourly.next_after(t) == datetime(2026, 11, 25, 4, 7, tzinfo=PRAGUE)
    assert hourly.previous_at_or_before(t) == datetime(2026, 11, 25, 3, 7, tzinfo=PRAGUE)
    assert retention.next_after(t) == datetime(2026, 11, 26, 3, 30, tzinfo=PRAGUE)
    assert retention.previous_at_or_before(t) == datetime(2026, 11, 25, 3, 30, tzinfo=PRAGUE)
    assert weekly.next_after(t) == datetime(2026, 11, 30, 7, 0, tzinfo=PRAGUE)
    assert weekly.previous_at_or_before(t) == datetime(2026, 11, 23, 7, 0, tzinfo=PRAGUE)
    exact = datetime(2026, 11, 25, 4, 7, tzinfo=PRAGUE)
    assert hourly.next_after(exact) == exact + timedelta(hours=1)
    assert hourly.previous_at_or_before(exact) == exact


def _scheduler(calls: list[str], **kwargs: object) -> worker.Scheduler:
    def job(name: str) -> Callable[[IngestSettings], dict[str, str]]:
        def run(_s: IngestSettings) -> dict[str, str]:
            calls.append(name)
            if name == "catalog_daily":
                raise ValueError("boom")
            return {"ok": name}

        return run

    return worker.Scheduler(
        IngestSettings(timezone="Europe/Prague"),
        worker.MemoryRunStore(),
        jobs={s.job: job(s.job) for s in worker.SCHEDULES},
        **kwargs,  # type: ignore[arg-type]
    )


def test_run_job_records_success_and_failure() -> None:
    calls: list[str] = []
    sched = _scheduler(calls)
    assert sched.run_job("ingest_hourly")
    assert not sched.run_job("catalog_daily", "schedule")
    runs = sched.store.recent()
    assert [(r.job, r.status) for r in runs] == [
        ("catalog_daily", "failure"),
        ("ingest_hourly", "success"),
    ]
    assert runs[0].error == "ValueError: boom"
    assert runs[0].finished_at is not None


def test_missed_jobs() -> None:
    calls: list[str] = []
    sched = _scheduler(calls)
    now = datetime(2026, 11, 25, 5, 0, tzinfo=UTC)
    assert sched.missed(now) == [s.job for s in worker.SCHEDULES]
    for job in ("ingest_hourly", "retention_daily", "catalog_daily", "findings_weekly"):
        sched.run_job(job)
    # catalog_daily failed, so it is still missed; the others ran after their due time.
    assert sched.missed(datetime.now(UTC)) == ["catalog_daily"]


def test_restate_after_catch_up_moves_the_cursor() -> None:
    restated: list[tuple[datetime, datetime]] = []
    t0 = datetime(2026, 11, 20, tzinfo=UTC)
    gaps = [(t0, t0 + timedelta(hours=2), 5), (t0 + timedelta(days=1), t0 + timedelta(days=2), 9)]
    seen: list[int | None] = []

    def pending(after: int | None) -> list[tuple[datetime, datetime, int]]:
        seen.append(after)
        return [g for g in gaps if after is None or g[2] > after]

    sched = _scheduler([], restate=lambda a, b: restated.append((a, b)), pending_gaps=pending)
    assert sched.restate_caught_up()
    assert restated == [(t0, t0 + timedelta(days=2))]
    assert sched.store.get_cursor(worker.RESTATE_CURSOR) == "9"
    assert not sched.restate_caught_up()
    assert seen == [None, 9]


def test_failed_restate_keeps_the_cursor() -> None:
    t0 = datetime(2026, 11, 20, tzinfo=UTC)

    def fail(_a: datetime, _b: datetime) -> None:
        raise RuntimeError("sqlmesh")

    sched = _scheduler([], restate=fail, pending_gaps=lambda _a: [(t0, t0, 3)])
    assert sched.restate_caught_up()
    assert sched.store.get_cursor(worker.RESTATE_CURSOR) is None
    assert sched.store.recent()[0].status == "failure"


def test_low_disk_brings_retention_forward_once_a_day() -> None:
    calls: list[str] = []
    sched = _scheduler(calls, disk_path="/data/lake", free=lambda _p: 0.05)
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    sched.check_disk(now)
    sched.check_disk(now + timedelta(hours=1))
    assert calls == ["retention_daily"]
    sched.check_disk(now + timedelta(days=1, minutes=1))
    assert calls == ["retention_daily", "retention_daily"]
    assert sched.store.recent()[0].trigger == "low-disk"


def test_enough_disk_does_nothing() -> None:
    calls: list[str] = []
    sched = _scheduler(calls, disk_path="/data/lake", free=lambda _p: 0.5)
    sched.check_disk(datetime.now(UTC))
    no_path = _scheduler(calls, free=lambda _p: 0.0)
    no_path.check_disk(datetime.now(UTC))
    assert calls == []


def test_full_disk_pauses_writing_jobs_but_not_the_retention() -> None:
    calls: list[str] = []
    sched = _scheduler(calls, disk_path="/data/lake", stop_free_ratio=0.03, free=lambda _p: 0.01)
    assert sched.disk_full()
    assert not sched.run_job("ingest_hourly", "schedule")
    assert sched.run_job("retention_daily", "schedule")
    assert calls == ["retention_daily"]
    assert [r.job for r in sched.store.recent()] == ["retention_daily"]
    roomy = _scheduler(calls, disk_path="/data/lake", stop_free_ratio=0.03, free=lambda _p: 0.2)
    assert not roomy.disk_full()


def test_disk_gate() -> None:
    s = IngestSettings(worker_stop_free_ratio=0.05)
    gate = worker.disk_gate("/data/lake", s, free=lambda _p: 0.04)
    assert gate is not None
    assert gate() is False
    assert worker.disk_gate("/data/lake", s, free=lambda _p: 0.5)() is True  # type: ignore[misc]
    assert worker.disk_gate(None, s) is None  # object storage
    assert worker.disk_gate("/data/lake", IngestSettings(worker_stop_free_ratio=0)) is None


def test_retry_ha() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return "ok"

    assert worker._retry_ha(flaky, sleep=sleeps.append) == "ok"
    assert sleeps == [30.0, 60.0]

    def broken() -> str:
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        worker._retry_ha(broken, sleep=lambda _s: None)


def test_run_loop_runs_missed_jobs_then_stops() -> None:
    calls: list[str] = []
    sched = _scheduler(calls)

    async def main() -> None:
        task = asyncio.create_task(sched.run())
        for _ in range(200):
            if len(calls) == len(worker.SCHEDULES):
                break
            await asyncio.sleep(0.01)
        sched.stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    assert calls == [s.job for s in worker.SCHEDULES]


def test_worker_settings_defaults() -> None:
    s = IngestSettings()
    assert s.worker_pg_db == "ops"
    assert s.worker_min_free_ratio == 0.10
    assert s.worker_stop_free_ratio == 0.03
