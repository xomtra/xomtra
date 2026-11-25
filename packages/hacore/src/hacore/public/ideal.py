"""Hourly series of the IDEAL homes (``auxiliarydata/hourly_readings`` of the IDEAL
Household Energy Dataset, Edinburgh DataShare, CC BY 4.0).

One gzipped CSV per sensor, named
``home<N>_<room><M>_sensor<id>[c<id>]_<type>_<subtype>.csv.gz``, with rows
``timestamp,value`` (UTC, hourly). Room sensors (temperature, humidity, light) carry
their room, which becomes the area of the channel (``same_area`` within a home).
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np

from hacore.public.houses import House, from_hourly

NAME = re.compile(
    r"^home(?P<home>\d+)_(?P<room>[a-z]+)(?P<room_id>\d+)_sensor(?P<sensor>[0-9c]+)_"
    r"(?P<type>[^_]+)_(?P<subtype>.+)\.csv\.gz$"
)
# Members with the hourly readings (also in datasets.json).
HOURLY = r"^auxiliarydata/hourly_readings/home\d+_.+\.csv\.gz$"


def load_dir(directory: Path) -> list[House]:
    files: dict[str, list[tuple[Path, re.Match[str]]]] = defaultdict(list)
    for p in sorted(directory.glob("home*.csv.gz")):
        m = NAME.match(p.name)
        if m:
            files[m["home"]].append((p, m))
    con = duckdb.connect()
    houses = []
    for home in sorted(files, key=int):
        series: dict[str, dict[int, float]] = {}
        areas: dict[str, str] = {}
        for p, m in files[home]:
            channel = f"{m['room']}{m['room_id']}_{m['type']}_{m['subtype']}_{m['sensor']}"
            rows = con.execute(
                "SELECT (epoch(date_trunc('hour', ts))::BIGINT // 3600) AS h, avg(v) "
                "FROM read_csv($p, header = false, columns = {'ts': 'TIMESTAMP', 'v': 'DOUBLE'}) "
                "WHERE v IS NOT NULL GROUP BY h",
                {"p": str(p)},
            ).fetchall()
            if rows:
                series[channel] = {int(h): float(v) for h, v in rows}
                if m["type"].startswith("room"):
                    areas[channel] = f"{m['room']}{m['room_id']}"
        if not series:
            continue
        first = min(min(v) for v in series.values())
        last = max(max(v) for v in series.values())
        channels = sorted(series)
        values = np.full((last - first + 1, len(channels)), np.nan)
        for c, ch in enumerate(channels):
            for h, v in series[ch].items():
                values[h - first, c] = v
        houses.append(from_hourly(f"home{int(home):03d}", first, channels, values, areas))
    return houses
