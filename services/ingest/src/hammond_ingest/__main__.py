"""``hammond-ingest`` command line.

hammond-ingest stream                       # live state_changed stream (service)
hammond-ingest sync                         # registries + statistics, then transform
hammond-ingest backfill --start 10d         # state history from the HA recorder
hammond-ingest transform                    # SQLMesh plan/run bronze -> silver
hammond-ingest restate --start ... --end ...
hammond-ingest retention | maintenance
hammond-ingest catalog                      # entity profile and drift (catalog.*)
hammond-ingest outside-temperature          # Open-Meteo archive of the outside temperature
hammond-ingest correlations                 # correlations into catalog.finding
hammond-ingest anomalies                    # anomalies and change points into catalog.finding
hammond-ingest findings-weekly              # snapshot of the findings of the last week
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta

from hacore import lake
from hacore.stream import StateStream, StreamSettings
from hammond_ingest import tasks
from hammond_ingest.settings import IngestSettings

logger = logging.getLogger("hammond_ingest")


def _stream(settings: IngestSettings) -> int:
    url, token = settings.require_ha()
    lake_settings = lake.LakeSettings.from_env()
    stream = StateStream(
        url,
        token,
        lambda: lake.connect(lake_settings),
        StreamSettings(
            flush_interval=settings.stream_flush_seconds,
            max_buffer=settings.stream_max_buffer,
            max_catchup=timedelta(days=settings.stream_max_catchup_days),
            heartbeat_file=settings.heartbeat_file,
            labels=settings.labels,
        ),
    )

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stream.stop)
        await stream.run()

    asyncio.run(run())
    logger.info("Stream stopped, %d states written", stream.written)
    return 0


def _print(value: object) -> None:
    print(json.dumps(value, default=str, indent=2))


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912, PLR0915 - one branch per command
    parser = argparse.ArgumentParser(prog="hammond-ingest")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stream", help="live state_changed stream with catch-up")
    sub.add_parser("sync", help="registries and statistics, then transform")
    p = sub.add_parser("backfill", help="state history from the HA recorder")
    p.add_argument("--start", required=True, help="ISO date/time or '<n>d' (days ago)")
    p.add_argument("--end", help="ISO date/time (default now)")
    p.add_argument("--no-transform", action="store_true")
    sub.add_parser("transform", help="SQLMesh plan + run (bronze -> silver)")
    p = sub.add_parser("restate", help="recompute silver for a time range")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    sub.add_parser("retention", help="delete rows older than HAMMOND_*_RETENTION_DAYS")
    sub.add_parser("maintenance", help="compact files, expire snapshots, delete old files")
    sub.add_parser("catalog", help="profile entities and record drift (catalog.*)")
    sub.add_parser("outside-temperature", help="Open-Meteo archive of the outside temperature")
    sub.add_parser("correlations", help="correlations between entities (catalog.finding)")
    sub.add_parser("anomalies", help="anomalies and change points (catalog.finding)")
    sub.add_parser("findings-weekly", help="snapshot of the findings of the last week")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("websockets", "sqlmesh", "sqlglot"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = IngestSettings()

    match args.command:
        case "stream":
            return _stream(settings)
        case "sync":
            _print({"registries": tasks.sync_registries(), "statistics": tasks.sync_statistics()})
            _print(tasks.run_transform())
        case "backfill":
            start = tasks.parse_time(args.start)
            end = tasks.parse_time(args.end) if args.end else datetime.now(UTC)
            _print(tasks.backfill_history(start, end))
            if not args.no_transform:
                tasks.restate(start, end)
        case "transform":
            _print(tasks.run_transform())
        case "restate":
            tasks.restate(tasks.parse_time(args.start), tasks.parse_time(args.end))
        case "retention":
            _print(tasks.retention(settings))
        case "maintenance":
            tasks.lake_maintenance(settings)
        case "catalog":
            _print(tasks.profile_catalog(settings))
        case "outside-temperature":
            _print(tasks.sync_outside_temperature(settings))
        case "correlations":
            _print(tasks.find_correlations(settings))
        case "anomalies":
            _print(tasks.find_anomalies(settings))
        case "findings-weekly":
            _print(tasks.snapshot_findings(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
