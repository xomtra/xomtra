"""Dagster code location of HAmmond ingest.

Jobs
    ``ingest_hourly``     registries -> statistics -> SQLMesh run (scheduled hourly)
    ``retention_daily``   retention -> DuckLake maintenance (scheduled daily)
    ``history_backfill``  state history of a range from the HA recorder (manual)
    ``recorder_import``   history from a backup copy of the recorder database (manual)
    ``silver_restate``    recompute silver for a range (manual)

The live ``state_changed`` stream is a separate long-running service (``hammond-ingest
stream``), not a Dagster job. Runs are serialised by the queued run coordinator
(``dagster.yaml``) so that heavy jobs never compete for DuckLake commits.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from dagster import (
    Backoff,
    Config,
    DefaultScheduleStatus,
    Definitions,
    In,
    Nothing,
    OpExecutionContext,
    RetryPolicy,
    ScheduleDefinition,
    job,
    op,
)

from hammond_ingest import tasks
from hammond_ingest.settings import IngestSettings

HA_RETRY = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)


class RangeConfig(Config):
    start: str = "10d"
    """ISO date/time (UTC if no offset) or '<n>d' for n days ago."""
    end: str = ""
    """ISO date/time; empty = now."""

    def bounds(self) -> tuple[datetime, datetime]:
        start = tasks.parse_time(self.start)
        end = tasks.parse_time(self.end) if self.end else datetime.now(UTC)
        return start, end


class ImportConfig(Config):
    path: str
    """Path of home-assistant_v2.db inside the container (mounted under /import)."""
    start: str = ""
    end: str = ""


@op(retry_policy=HA_RETRY)
def sync_registries(context: OpExecutionContext) -> None:
    context.log.info(f"Registry changes written: {tasks.sync_registries()}")


@op(ins={"after": In(Nothing)}, retry_policy=HA_RETRY)
def sync_statistics(context: OpExecutionContext) -> None:
    context.log.info(f"Statistics written: {tasks.sync_statistics()}")


@op(ins={"after": In(Nothing)})
def transform(context: OpExecutionContext) -> None:
    result = tasks.run_transform()
    context.log.info(f"SQLMesh: applied={result.applied} status={result.status}")


@op(retry_policy=HA_RETRY)
def backfill_history(context: OpExecutionContext, config: RangeConfig) -> None:
    start, end = config.bounds()
    context.log.info(f"States written: {tasks.backfill_history(start, end)}")
    tasks.restate(start, end)


@op
def import_recorder(context: OpExecutionContext, config: ImportConfig) -> None:
    result = tasks.import_recorder(
        Path(config.path),
        tasks.parse_time(config.start) if config.start else None,
        tasks.parse_time(config.end) if config.end else None,
    )
    context.log.info(f"Import: {result}")
    if result.first_state and result.last_state:
        tasks.restate(result.first_state, result.last_state + timedelta(hours=1))


@op
def restate_silver(context: OpExecutionContext, config: RangeConfig) -> None:
    start, end = config.bounds()
    tasks.restate(start, end)
    context.log.info(f"Restated silver {start.isoformat()} .. {end.isoformat()}")


@op
def retention(context: OpExecutionContext) -> None:
    context.log.info(f"Retention: {tasks.retention()}")


@op(ins={"after": In(Nothing)})
def lake_maintenance(context: OpExecutionContext) -> None:
    tasks.lake_maintenance()
    context.log.info("DuckLake maintenance done")


@job(tags={"dagster/max_retries": 1})
def ingest_hourly() -> None:
    transform(after=sync_statistics(after=sync_registries()))


@job
def retention_daily() -> None:
    lake_maintenance(after=retention())


@job
def history_backfill() -> None:
    backfill_history()


@job
def recorder_import() -> None:
    import_recorder()


@job
def silver_restate() -> None:
    restate_silver()


def build_definitions(settings: IngestSettings | None = None) -> Definitions:
    tz = (settings or IngestSettings()).timezone
    return Definitions(
        jobs=[ingest_hourly, retention_daily, history_backfill, recorder_import, silver_restate],
        schedules=[
            ScheduleDefinition(
                job=ingest_hourly,
                cron_schedule="7 * * * *",
                execution_timezone=tz,
                default_status=DefaultScheduleStatus.RUNNING,
            ),
            ScheduleDefinition(
                job=retention_daily,
                cron_schedule="30 3 * * *",
                execution_timezone=tz,
                default_status=DefaultScheduleStatus.RUNNING,
            ),
        ],
    )


defs = build_definitions()
