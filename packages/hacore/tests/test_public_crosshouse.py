import gzip
import io
import itertools
import os
import zipfile
from pathlib import Path

import numpy as np
import pytest

from hacore.public import crosshouse as ch
from hacore.public import fetch, ideal, refit
from hacore.public.houses import House, from_hourly


def fake_house(name: str, rng: np.random.Generator, days: int = 400, channels: int = 4) -> House:
    """Independent houses: a daily profile, AR(1) noise and one related pair inside."""
    t = days * 24
    first = (
        int(np.datetime64("2014-01-01T00", "h").astype(np.int64)) + int(rng.integers(0, 60)) * 24
    )
    hod = np.arange(t) % 24
    profile = 200 + 150 * np.sin(2 * np.pi * (hod - 7) / 24)
    vals = np.empty((t, channels))
    for c in range(channels):
        e = rng.normal(0, 1, t)
        x = np.empty(t)
        x[0] = e[0]
        for i in range(1, t):
            x[i] = 0.6 * x[i - 1] + e[i]
        vals[:, c] = profile + 40 * x
    vals[:, 1] = vals[:, 0] * 0.5 + rng.normal(0, 10, t)  # related inside the house
    return from_hourly(name, first, [f"Appliance{c}" for c in range(channels)], vals)


def test_mixed_grid_separates_houses() -> None:
    rng = np.random.default_rng(1)
    houses = [fake_house(f"h{i:02d}", rng) for i in range(6)]
    mixed = ch.mixed_grid(houses, 90, 2, np.random.default_rng(0))
    assert mixed is not None
    grid, placements, _ = mixed
    assert len(placements) >= 2
    starts = sorted(p.start for p in placements)
    gaps = [(b - a).astype("timedelta64[D]").astype(int) for a, b in itertools.pairwise(starts)]
    assert min(gaps) >= ch.MIN_GAP_DAYS
    assert all(
        int((p.start.astype("datetime64[D]").astype(np.int64) + 3) % 7) == 0 for p in placements
    )
    assert grid.values.shape == (90 * 24, 2 * len(placements))


def test_null_is_quiet_and_within_house_relation_found() -> None:
    rng = np.random.default_rng(2)
    houses = [fake_house(f"h{i:02d}", rng) for i in range(8)]
    metrics = ch.run(houses, runs=2, days=90, per_house=2, seed=0)
    s = ch.summarise(metrics, 2)
    assert s["runs"] == 2
    assert s["cross_pairs"] > 0
    assert s["cross"] <= 1  # independent houses: (almost) no findings between them
    assert s["findings"] >= 1  # the related pair inside a house is found
    assert s["context_share"] == 0
    assert set(s["recall_by_magnitude"]) <= {"4", "6", "8"}


def test_refit_loader(tmp_path: Path) -> None:
    f = tmp_path / "CLEAN_House3.csv"
    start = 1_400_000_400  # not an hour boundary
    lines = ["Time,Unix,Aggregate,Appliance1,Appliance2,Issues"]
    for i in range(0, 3 * 3600, 8):
        lines.append(f"x,{start + i},{100 + (i // 3600)},{5},{i % 7},0")
    f.write_text("\n".join(lines))
    h = refit.load_house(f)
    assert h.house == "h03"
    assert h.channels == ["Aggregate", "Appliance1", "Appliance2"]
    assert int(h.hours[0].astype(np.int64)) % 24 == 0
    known = h.values[~np.isnan(h.values[:, 0])]
    assert len(known) >= 2
    assert np.allclose(known[:, 1], 5)


def test_ideal_loader(tmp_path: Path) -> None:
    def write(name: str, rows: list[str]) -> None:
        (tmp_path / name).write_bytes(gzip.compress("\n".join(rows).encode()))

    write(
        "home113_hall1201_sensor5922c5924_electric-mains_electric-combined.csv.gz",
        ["2017-06-20 11:00:00,425", "2017-06-20 12:00:00,228", "2017-06-20 14:00:00,347"],
    )
    write("home113_kitchen7_sensor1_room_temperature.csv.gz", ["2017-06-20 12:00:00,21.5"])
    write("home007_bedroom2_sensor9_room_humidity.csv.gz", ["2017-06-21 00:00:00,55"])
    houses = ideal.load_dir(tmp_path)
    assert [h.house for h in houses] == ["home007", "home113"]
    h = houses[1]
    assert len(h.channels) == 2
    temp = next(c for c in h.channels if "temperature" in c)
    assert h.areas == {temp: "kitchen7"}
    assert int(h.hours[0].astype(np.int64)) % 24 == 0
    mains = h.values[:, h.channels.index(next(c for c in h.channels if "mains" in c))]
    assert np.nansum(mains) == 425 + 228 + 347


def test_fetch_extracts_members_by_range(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("auxiliarydata/light/big.csv.gz", os.urandom(300_000))
        zf.writestr("auxiliarydata/hourly_readings/home1_hall1_sensor1_x_y.csv.gz", b"a,1\n")
        zf.writestr("auxiliarydata/hourly_readings/home2_hall1_sensor2_x_y.csv.gz", b"b,2\n")
    data = buf.getvalue()

    class Resp(io.BytesIO):
        status = 206

        def __init__(self, body: bytes) -> None:
            super().__init__(body)
            self.headers: dict[str, str] = {}

    def fake_open(self: fetch.RangeFile, headers: dict[str, str], method: str = "GET") -> Resp:
        self.requests += 1
        if method == "HEAD":
            r = Resp(b"")
            r.headers = {"Content-Length": str(len(data))}
            return r
        lo, hi = (int(x) for x in headers["Range"].removeprefix("bytes=").split("-"))
        return Resp(data[lo : hi + 1])

    monkeypatch.setattr(fetch.RangeFile, "_open", fake_open)
    digest, files = fetch.extract("https://example.invalid/x.zip", ideal.HOURLY, tmp_path)
    assert sorted(p.name for p in files) == [
        "home1_hall1_sensor1_x_y.csv.gz",
        "home2_hall1_sensor2_x_y.csv.gz",
    ]
    assert (tmp_path / "home2_hall1_sensor2_x_y.csv.gz").read_bytes() == b"b,2\n"
    again, _ = fetch.extract(
        "https://example.invalid/x.zip", ideal.HOURLY, tmp_path, expected_sha256=digest
    )
    assert again == digest
    with pytest.raises(ValueError, match="archive changed"):
        fetch.extract(
            "https://example.invalid/x.zip", ideal.HOURLY, tmp_path, expected_sha256="0" * 64
        )
