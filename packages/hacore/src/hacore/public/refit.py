"""Hourly series of the REFIT houses (cleaned release, CSV ``CLEAN_House<n>.csv``).

Each file holds 8-second readings of one house: ``Unix`` (epoch seconds, UTC), the
``Aggregate`` power and up to nine ``Appliance<n>`` channels in watts. The loader turns
them into hourly means with DuckDB (a house file has tens of millions of rows) and
keeps an hour only with at least ``MIN_READINGS`` readings.
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import numpy as np

from hacore.public.houses import House, from_hourly

# Readings of an hour needed for its mean (450 at 8 s sampling = a full hour).
MIN_READINGS = 100
CHANNEL = re.compile(r"^(Aggregate|Appliance\d+)$")
HOUSE = re.compile(r"House_?(\d+)", re.IGNORECASE)


def house_id(path: Path) -> str:
    m = HOUSE.search(path.stem)
    return f"h{int(m.group(1)):02d}" if m else path.stem


def load_house(path: Path, con: duckdb.DuckDBPyConnection | None = None) -> House:
    con = con or duckdb.connect()
    cols = [
        str(r[0])
        for r in con.execute(
            "SELECT column_name FROM (DESCRIBE SELECT * FROM read_csv_auto($p))", {"p": str(path)}
        ).fetchall()
    ]
    channels = [c for c in cols if CHANNEL.match(c)]
    if "Unix" not in cols or not channels:
        raise ValueError(f"{path.name}: not a REFIT house file (columns {cols})")
    means = ", ".join(f'avg("{c}")::DOUBLE' for c in channels)
    rows = con.execute(
        f"""
        SELECT ("Unix"::BIGINT // 3600) AS h, {means}
        FROM read_csv_auto($p)
        GROUP BY h HAVING count(*) >= {MIN_READINGS}
        ORDER BY h
        """,  # noqa: S608 - column names come from the file and match CHANNEL
        {"p": str(path)},
    ).fetchall()
    if not rows:
        raise ValueError(f"{path.name}: no complete hours")
    first, last = int(rows[0][0]), int(rows[-1][0])
    values = np.full((last - first + 1, len(channels)), np.nan)
    for r in rows:
        values[int(r[0]) - first] = [np.nan if v is None else float(v) for v in r[1:]]
    return from_hourly(house_id(path), first, channels, values)


def load_dir(directory: Path) -> list[House]:
    """All REFIT house files in ``directory`` (sorted by house)."""
    con = duckdb.connect()
    files = sorted(directory.glob("*House*.csv"), key=house_id)
    return [load_house(p, con) for p in files]
