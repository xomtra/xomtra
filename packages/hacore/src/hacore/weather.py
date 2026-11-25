"""Hourly outside temperature from the Open-Meteo historical archive (ADR 043).

The correlations (ADR 042) regress out the outside temperature. A ``weather.*`` entity
of Home Assistant only has the history xomtra collected itself, so the hours before
that come from the Open-Meteo archive (ERA5 reanalysis, the source behind the HA
Open-Meteo integration). The archive lags a few days behind; the live entity covers the
recent hours.

Table ``bronze.ext_outside_temperature`` holds one row per UTC hour. Every sync
re-fetches the last ``REFETCH`` days (the archive fills them in later) and the hours
before the first stored one, up to ``days`` back. Coordinates are rounded to two
decimals (about 1 km) before they leave the installation.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import duckdb

from hacore.lake import LAKE_ALIAS

logger = logging.getLogger(__name__)

SCHEMA = "bronze"
TABLE = "ext_outside_temperature"
SOURCE = "open-meteo-archive"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REFETCH = timedelta(days=10)
TIMEOUT_SECONDS = 60.0

type Fetcher = Callable[[float, float, date, date], list[tuple[datetime, float]]]


@dataclass(frozen=True, slots=True)
class SyncResult:
    fetched_from: date | None
    fetched_to: date | None
    hours: int

    def as_dict(self) -> dict[str, object]:
        return {
            "fetched_from": self.fetched_from.isoformat() if self.fetched_from else None,
            "fetched_to": self.fetched_to.isoformat() if self.fetched_to else None,
            "hours": self.hours,
        }


def ensure_table(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {alias}.{SCHEMA}")
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {alias}.{SCHEMA}.{TABLE} ("
        "source VARCHAR NOT NULL, hour TIMESTAMPTZ NOT NULL, temperature DOUBLE, "
        "fetched_at TIMESTAMPTZ NOT NULL)"
    )


def round_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    return round(float(latitude), 2), round(float(longitude), 2)


def fetch_archive(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    *,
    url: str = ARCHIVE_URL,
) -> list[tuple[datetime, float]]:
    """Hourly ``temperature_2m`` (°C) in UTC; hours the archive does not have yet are
    left out."""
    if not url.startswith("https://"):
        raise ValueError("The Open-Meteo URL must use https")
    query = urllib.parse.urlencode(
        {
            "latitude": f"{latitude:.2f}",
            "longitude": f"{longitude:.2f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": "temperature_2m",
            "timezone": "UTC",
        }
    )
    with urllib.request.urlopen(f"{url}?{query}", timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - https only, checked above
        payload = json.load(resp)
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    out = []
    for t, v in zip(times, temps, strict=False):
        if v is None:
            continue
        out.append((datetime.fromisoformat(t).replace(tzinfo=UTC), float(v)))
    return out


def sync(
    con: duckdb.DuckDBPyConnection,
    latitude: float,
    longitude: float,
    *,
    days: int,
    now: datetime | None = None,
    fetch: Fetcher | None = None,
    alias: str = LAKE_ALIAS,
) -> SyncResult:
    """Fetch the missing and the recent hours of the last ``days`` days."""
    ensure_table(con, alias)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    lat, lon = round_coordinates(latitude, longitude)
    t = f"{alias}.{SCHEMA}.{TABLE}"
    want_from = (now - timedelta(days=days)).date()
    row = con.execute(
        f"SELECT min(hour), max(hour) FROM {t} WHERE source = $s", {"s": SOURCE}
    ).fetchone()
    first, last = row or (None, None)
    if first is not None and first.astimezone(UTC).date() <= want_from and last is not None:
        start = min(last.astimezone(UTC).date(), now.date()) - REFETCH
        start = max(start, want_from)
    else:
        start = want_from
    end = now.date()
    rows = (fetch or fetch_archive)(lat, lon, start, end)
    begin = datetime(start.year, start.month, start.day, tzinfo=UTC)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"DELETE FROM {t} WHERE source = $s AND hour >= $b", {"s": SOURCE, "b": begin})
        if rows:
            con.execute(
                f"INSERT INTO {t} SELECT $s, unnest($h::TIMESTAMPTZ[]), unnest($v::DOUBLE[]), $now",
                {"s": SOURCE, "h": [h for h, _ in rows], "v": [v for _, v in rows], "now": now},
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    result = SyncResult(start, end, len(rows))
    logger.info("Outside temperature archive: %s", result.as_dict())
    return result


def load(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, alias: str = LAKE_ALIAS
) -> dict[datetime, float]:
    """Stored archive hours in ``[start, end)``; empty when the table does not exist."""
    exists = con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = $d AND schema_name = $s "
        "AND table_name = $t",
        {"d": alias, "s": SCHEMA, "t": TABLE},
    ).fetchone()
    if not exists or not exists[0]:
        return {}
    rows = con.execute(
        f"SELECT hour, temperature FROM {alias}.{SCHEMA}.{TABLE} "
        "WHERE source = $s AND hour >= $a AND hour < $b AND temperature IS NOT NULL",
        {"s": SOURCE, "a": start, "b": end},
    ).fetchall()
    return {h.astimezone(UTC): float(v) for h, v in rows}
