"""``hammond-worker``: live stream and scheduled jobs in one process (ADR 050).

The lite edition (HA add-on, small machines) runs this instead of the Dagster daemon
and webserver. The jobs are the same functions of :mod:`hammond_ingest.tasks` that the
Dagster jobs call, with the same schedules (``SCHEDULES``):

``ingest_hourly``    registries -> statistics -> SQLMesh run          hourly at :07
``retention_daily``  retention -> DuckLake maintenance                daily 03:30
``catalog_daily``    profile -> outside temperature -> correlations
                     -> anomalies                                     daily 04:15
``findings_weekly``  weekly snapshot of the findings                  Monday 07:00

Every five minutes the worker also restates silver for ranges the stream caught up
after a long outage (the Dagster sensor ``restate_after_catchup``).

Jobs run one at a time in a worker thread, so heavy jobs never compete for DuckLake
commits; the stream keeps running in the event loop. Runs and the restate cursor are
kept in the provisioning database (``worker.job_run``, ``worker.cursor``) of the ops
role. A schedule missed while the worker was down runs once after the start. When the
lake's disk has less than ``min_free_ratio`` free, the worker logs a warning and brings
the retention forward (at most once a day).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from hammond_ingest import tasks
from hammond_ingest.settings import IngestSettings

logger = logging.getLogger(__name__)

RESTATE_JOB = "silver_restate"
RESTATE_CURSOR = "restate_after_catchup"
RESTATE_INTERVAL = timedelta(minutes=5)
HA_RETRIES = 3
HA_RETRY_DELAY = 30.0


# ---- schedules ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Schedule:
    """``minute`` past every hour, or at ``hour``:``minute``, optionally on ``weekday``
    (0 = Monday) only, in the worker's time zone."""

    job: str
    minute: int
    hour: int | None = None
    weekday: int | None = None

    def _matches(self, t: datetime) -> bool:
        return (
            t.minute == self.minute
            and (self.hour is None or t.hour == self.hour)
            and (self.weekday is None or t.weekday() == self.weekday)
        )

    def _candidates(self, start: datetime, step: int) -> Iterable[datetime]:
        # Whole hours around ``start``: at most 8 days of hours covers every schedule.
        base = start.replace(minute=self.minute, second=0, microsecond=0)
        for i in range(0, 8 * 24 + 1):
            yield base + timedelta(hours=step * i)

    def next_after(self, t: datetime) -> datetime:
        """First scheduled time strictly after ``t`` (aware datetime, any zone)."""
        for c in self._candidates(t, 1):
            if c > t and self._matches(c):
                return c
        raise AssertionError("unreachable: every schedule fires within a week")

    def previous_at_or_before(self, t: datetime) -> datetime:
        for c in self._candidates(t, -1):
            if c <= t and self._matches(c):
                return c
        raise AssertionError("unreachable: every schedule fires within a week")


SCHEDULES: tuple[Schedule, ...] = (
    Schedule("ingest_hourly", minute=7),
    Schedule("retention_daily", minute=30, hour=3),
    # After retention and maintenance, before the morning.
    Schedule("catalog_daily", minute=15, hour=4),
    # Monday morning, after the daily findings of Monday.
    Schedule("findings_weekly", minute=0, hour=7, weekday=0),
)


# ---- jobs ---------------------------------------------------------------------------


def _retry_ha[T](fn: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """Steps that call Home Assistant: 3 retries with exponential backoff (as Dagster)."""
    for attempt in range(HA_RETRIES + 1):
        try:
            return fn()
        except (OSError, TimeoutError) as err:
            if attempt == HA_RETRIES:
                raise
            delay = HA_RETRY_DELAY * 2**attempt
            logger.warning("Home Assistant step failed (%s), retry in %.0f s", err, delay)
            sleep(delay)
    raise AssertionError("unreachable")


def _ingest_hourly(s: IngestSettings) -> dict[str, Any]:
    registries = _retry_ha(tasks.sync_registries)
    statistics = _retry_ha(lambda: tasks.sync_statistics(s))
    result = tasks.run_transform()
    return {
        "registries": registries,
        "statistics": statistics,
        "transform": {"applied": result.applied, "status": result.status},
    }


def _retention_daily(s: IngestSettings) -> dict[str, Any]:
    out = tasks.retention(s)
    tasks.lake_maintenance(s)
    return out


def _catalog_daily(s: IngestSettings) -> dict[str, Any]:
    # Correlations and anomalies pick binary/enum states from the fresh profile.
    return {
        "catalog": tasks.profile_catalog(s),
        "outside_temperature": tasks.sync_outside_temperature(s),
        "correlations": tasks.find_correlations(s),
        "anomalies": tasks.find_anomalies(s),
    }


def _findings_weekly(s: IngestSettings) -> dict[str, Any]:
    return tasks.snapshot_findings(s)


JOBS: dict[str, Callable[[IngestSettings], Any]] = {
    "ingest_hourly": _ingest_hourly,
    "retention_daily": _retention_daily,
    "catalog_daily": _catalog_daily,
    "findings_weekly": _findings_weekly,
}


# ---- run store ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    run_id: int
    job: str
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    status: str  # running | success | failure
    error: str | None = None


class RunStore(Protocol):
    def start(self, job: str, trigger: str) -> int: ...
    def finish(self, run_id: int, status: str, error: str | None, result: Any) -> None: ...
    def last_success(self, job: str) -> datetime | None: ...
    def recent(self, limit: int = 50) -> list[Run]: ...
    def get_cursor(self, name: str) -> str | None: ...
    def set_cursor(self, name: str, value: str) -> None: ...


@dataclass
class MemoryRunStore:
    """Run store in memory (tests, ``--no-store`` development runs)."""

    runs: list[Run] = field(default_factory=list)
    cursors: dict[str, str] = field(default_factory=dict)
    results: dict[int, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self, job: str, trigger: str) -> int:
        with self._lock:
            run = Run(len(self.runs) + 1, job, trigger, datetime.now(UTC), None, "running")
            self.runs.append(run)
            return run.run_id

    def finish(self, run_id: int, status: str, error: str | None, result: Any) -> None:
        with self._lock:
            old = self.runs[run_id - 1]
            self.runs[run_id - 1] = Run(
                old.run_id, old.job, old.trigger, old.started_at, datetime.now(UTC), status, error
            )
            self.results[run_id] = result

    def last_success(self, job: str) -> datetime | None:
        done = [r.started_at for r in self.runs if r.job == job and r.status == "success"]
        return max(done) if done else None

    def recent(self, limit: int = 50) -> list[Run]:
        return list(reversed(self.runs))[:limit]

    def get_cursor(self, name: str) -> str | None:
        return self.cursors.get(name)

    def set_cursor(self, name: str, value: str) -> None:
        self.cursors[name] = value


SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS worker;
CREATE TABLE IF NOT EXISTS worker.job_run (
    run_id      bigserial PRIMARY KEY,
    job         text NOT NULL,
    trigger     text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL CHECK (status IN ('running', 'success', 'failure')),
    error       text,
    result      jsonb
);
CREATE INDEX IF NOT EXISTS job_run_job_started ON worker.job_run (job, started_at DESC);
CREATE TABLE IF NOT EXISTS worker.cursor (
    name       text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresRunStore:
    """``worker.job_run`` and ``worker.cursor`` in the ops database (psycopg2).

    One connection, serialised; the schema is created on first use. Runs left
    ``running`` by a killed worker are marked failed at start.
    """

    def __init__(self, dsn: str, *, keep_days: int = 90) -> None:
        import psycopg2  # noqa: PLC0415 - only needed with the database store

        self._conn = psycopg2.connect(dsn, connect_timeout=10)
        self._conn.autocommit = True
        self._lock = threading.Lock()
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(
                "UPDATE worker.job_run SET status = 'failure', finished_at = now(), "
                "error = 'worker stopped during the run' WHERE status = 'running'"
            )
            cur.execute(
                "DELETE FROM worker.job_run WHERE started_at < now() - make_interval(days => %s)",
                (keep_days,),
            )

    def _one(self, sql: str, args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() if cur.description else None

    def start(self, job: str, trigger: str) -> int:
        row = self._one(
            "INSERT INTO worker.job_run (job, trigger, status) VALUES (%s, %s, 'running') "
            "RETURNING run_id",
            (job, trigger),
        )
        if row is None:
            raise RuntimeError("worker.job_run insert returned no id")
        return int(row[0])

    def finish(self, run_id: int, status: str, error: str | None, result: Any) -> None:
        self._one(
            "UPDATE worker.job_run SET finished_at = now(), status = %s, error = %s, "
            "result = %s::jsonb WHERE run_id = %s",
            (status, error, json.dumps(result, default=str), run_id),
        )

    def last_success(self, job: str) -> datetime | None:
        row = self._one(
            "SELECT max(started_at) FROM worker.job_run WHERE job = %s AND status = 'success'",
            (job,),
        )
        return row[0] if row else None

    def recent(self, limit: int = 50) -> list[Run]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, job, trigger, started_at, finished_at, status, error "
                "FROM worker.job_run ORDER BY run_id DESC LIMIT %s",
                (limit,),
            )
            return [Run(*row) for row in cur.fetchall()]

    def get_cursor(self, name: str) -> str | None:
        row = self._one("SELECT value FROM worker.cursor WHERE name = %s", (name,))
        return str(row[0]) if row else None

    def set_cursor(self, name: str, value: str) -> None:
        self._one(
            "INSERT INTO worker.cursor (name, value) VALUES (%s, %s) ON CONFLICT (name) "
            "DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (name, value),
        )

    def close(self) -> None:
        self._conn.close()


# ---- disk guard ---------------------------------------------------------------------


def free_ratio(path: str) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / usage.total if usage.total else 1.0


# ---- scheduler ----------------------------------------------------------------------


class Scheduler:
    """Runs the scheduled jobs one at a time; see the module docstring."""

    def __init__(
        self,
        settings: IngestSettings,
        store: RunStore,
        *,
        jobs: dict[str, Callable[[IngestSettings], Any]] | None = None,
        schedules: Iterable[Schedule] = SCHEDULES,
        disk_path: str | None = None,
        min_free_ratio: float = 0.10,
        restate: Callable[[datetime, datetime], None] = tasks.restate,
        pending_gaps: Callable[[int | None], list[tuple[datetime, datetime, int]]] = (
            tasks.pending_gaps
        ),
        free: Callable[[str], float] = free_ratio,
    ) -> None:
        self.settings = settings
        self.store = store
        self.jobs = JOBS if jobs is None else jobs
        self.schedules = tuple(schedules)
        self.tz = ZoneInfo(settings.timezone)
        self.disk_path = disk_path
        self.min_free_ratio = min_free_ratio
        self._restate = restate
        self._pending_gaps = pending_gaps
        self._free = free
        self._low_disk_retention: datetime | None = None
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    # -- single runs (synchronous, called in the worker thread) --

    def run_job(self, job: str, trigger: str = "manual") -> bool:
        fn = self.jobs[job]
        run_id = self.store.start(job, trigger)
        started = time.monotonic()
        logger.info("Job %s started (%s)", job, trigger)
        try:
            result = fn(self.settings)
        except Exception as err:
            logger.exception("Job %s failed", job)
            self.store.finish(run_id, "failure", f"{type(err).__name__}: {err}", None)
            return False
        self.store.finish(run_id, "success", None, result)
        logger.info("Job %s done in %.0f s", job, time.monotonic() - started)
        return True

    def restate_caught_up(self) -> bool:
        """Restate silver for new catch-up gaps of the stream; False if none."""
        cursor = self.store.get_cursor(RESTATE_CURSOR)
        gaps = self._pending_gaps(int(cursor) if cursor else None)
        if not gaps:
            return False
        start = min(g[0] for g in gaps)
        end = max(g[1] for g in gaps)
        run_id = self.store.start(RESTATE_JOB, "catch-up")
        try:
            self._restate(start, end)
        except Exception as err:
            logger.exception("Restate after catch-up failed")
            self.store.finish(run_id, "failure", f"{type(err).__name__}: {err}", None)
            return True
        self.store.set_cursor(RESTATE_CURSOR, str(max(g[2] for g in gaps)))
        self.store.finish(
            run_id, "success", None, {"start": start.isoformat(), "end": end.isoformat()}
        )
        return True

    def check_disk(self, now: datetime) -> None:
        """Warn and bring the retention forward when the lake's disk runs full."""
        if not self.disk_path:
            return
        ratio = self._free(self.disk_path)
        if ratio >= self.min_free_ratio:
            return
        logger.warning(
            "Only %.0f %% free on the lake's disk (%s); shorten HAMMOND_*_RETENTION_DAYS "
            "or add space",
            ratio * 100,
            self.disk_path,
        )
        if self._low_disk_retention and now - self._low_disk_retention < timedelta(days=1):
            return
        self._low_disk_retention = now
        if "retention_daily" in self.jobs:
            self.run_job("retention_daily", "low-disk")

    def missed(self, now: datetime) -> list[str]:
        """Jobs whose last scheduled time passed without a successful run since."""
        local = now.astimezone(self.tz)
        out = []
        for s in self.schedules:
            due = s.previous_at_or_before(local)
            last = self.store.last_success(s.job)
            if last is None or last < due:
                out.append(s.job)
        return out

    def next_due(self, now: datetime) -> tuple[datetime, list[str]]:
        local = now.astimezone(self.tz)
        times = {s.job: s.next_after(local) for s in self.schedules}
        first = min(times.values())
        return first, [job for job, t in times.items() if t == first]

    # -- loop --

    async def _sleep_until(self, when: datetime) -> bool:
        """Sleep until ``when``; True if the worker is stopping."""
        delay = max(0.0, (when - datetime.now(UTC)).total_seconds())
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    async def run(self) -> None:
        lock = asyncio.Lock()

        async def in_thread(fn: Callable[..., Any], *args: Any) -> Any:
            async with lock:
                return await asyncio.to_thread(fn, *args)

        for job in self.missed(datetime.now(UTC)):
            if self._stopping.is_set():
                return
            await in_thread(self.run_job, job, "missed")

        next_restate = datetime.now(UTC) + RESTATE_INTERVAL
        while not self._stopping.is_set():
            due, jobs = self.next_due(datetime.now(UTC))
            wake = min(due, next_restate)
            if await self._sleep_until(wake):
                return
            now = datetime.now(UTC)
            if now >= next_restate:
                next_restate = now + RESTATE_INTERVAL
                await in_thread(self.check_disk, now)
                await in_thread(self.restate_caught_up)
            if now >= due:
                for job in jobs:
                    await in_thread(self.run_job, job, "schedule")


async def run_worker(settings: IngestSettings, store: RunStore, stream: Any) -> None:
    """Stream and scheduler until SIGTERM/SIGINT (``stream.stop()``, ``scheduler.stop()``)."""
    import signal  # noqa: PLC0415

    from hacore import lake  # noqa: PLC0415

    lake_settings = lake.LakeSettings.from_env()
    scheduler = Scheduler(settings, store, disk_path=lake_settings.local_data_path)
    loop = asyncio.get_running_loop()

    def stop() -> None:
        stream.stop()
        scheduler.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    stream_task = asyncio.create_task(stream.run(), name="stream")
    scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
    done, pending = await asyncio.wait(
        {stream_task, scheduler_task}, return_when=asyncio.FIRST_COMPLETED
    )
    # One part ended (stop, or an error): stop the other and report the error.
    stream.stop()
    scheduler.stop()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()
