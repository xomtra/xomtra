"""Dagster code location of HAmmond ingest.

Jobs
    ``ingest_hourly``     registries -> statistics -> SQLMesh run (scheduled hourly)
    ``retention_daily``   retention -> DuckLake maintenance (scheduled daily)
    ``catalog_daily``     entity profile and drift into ``catalog.*``, then correlations
                          between entities into ``catalog.finding`` (scheduled daily)
    ``history_backfill``  state history of a range from the HA recorder (manual)
    ``silver_restate``    recompute silver for a range (manual, or the sensor
                          ``restate_after_catchup`` after a long stream outage)

The live ``state_changed`` stream is a separate long-running service (``hammond-ingest
stream``), not a Dagster job. Runs are serialised by the queued run coordinator
(``dagster.yaml``) so that heavy jobs never compete for DuckLake commits.
"""

from datetime import UTC, datetime

from dagster import (
    Backoff,
    Config,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    In,
    Nothing,
    OpExecutionContext,
    RetryPolicy,
    RunRequest,
    ScheduleDefinition,
    SensorEvaluationContext,
    SensorResult,
    SkipReason,
    job,
    op,
    sensor,
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


@op
def profile_catalog(context: OpExecutionContext) -> None:
    context.log.info(f"Catalog: {tasks.profile_catalog()}")


@job(tags={"dagster/max_retries": 1})
def ingest_hourly() -> None:
    transform(after=sync_statistics(after=sync_registries()))


@job
def retention_daily() -> None:
    lake_maintenance(after=retention())


@op(ins={"after": In(Nothing)})
def find_correlations(context: OpExecutionContext) -> None:
    for window in tasks.find_correlations():
        context.log.info(f"Correlations: {window}")


@job
def catalog_daily() -> None:
    # Correlations pick binary/enum states from the fresh profile.
    find_correlations(after=profile_catalog())


@job
def history_backfill() -> None:
    backfill_history()


@job
def silver_restate() -> None:
    restate_silver()


@sensor(
    job=silver_restate,
    minimum_interval_seconds=300,
    default_status=DefaultSensorStatus.RUNNING,
)
def restate_after_catchup(context: SensorEvaluationContext) -> SensorResult | SkipReason:
    """Restate silver for ranges the stream caught up after a long outage (ADR 031)."""
    after = int(context.cursor) if context.cursor else None
    gaps = tasks.pending_gaps(after)
    if not gaps:
        return SkipReason("No new catch-up gaps")
    start = min(g[0] for g in gaps)
    end = max(g[1] for g in gaps)
    cursor = str(max(g[2] for g in gaps))
    config = {"start": start.isoformat(), "end": end.isoformat()}
    return SensorResult(
        run_requests=[
            RunRequest(
                run_key=f"catchup-{cursor}",
                run_config={"ops": {"restate_silver": {"config": config}}},
            )
        ],
        cursor=cursor,
    )


def build_definitions(settings: IngestSettings | None = None) -> Definitions:
    tz = (settings or IngestSettings()).timezone
    return Definitions(
        jobs=[ingest_hourly, retention_daily, catalog_daily, history_backfill, silver_restate],
        sensors=[restate_after_catchup],
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
            # After retention and maintenance, before the morning.
            ScheduleDefinition(
                job=catalog_daily,
                cron_schedule="15 4 * * *",
                execution_timezone=tz,
                default_status=DefaultScheduleStatus.RUNNING,
            ),
        ],
    )


defs = build_definitions()
