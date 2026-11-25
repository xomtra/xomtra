import csv
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hacore import actionability as act
from hacore.public import usefulness_sample as us
from hacore.public.houses import from_hourly

DAYS = 200


def house(name: str, seed: int) -> Any:
    rng = np.random.default_rng(seed)
    t = np.arange(DAYS * 24)
    daily = np.sin(2 * np.pi * t / 24)
    a = rng.normal(size=len(t)).cumsum() * 0.05 + daily
    temp = 20 + daily + 0.3 * rng.normal(size=len(t))
    hum = 50 - 2 * daily + 0.5 * rng.normal(size=len(t))
    power = 100 + 30 * np.roll(a, 2) + 5 * rng.normal(size=len(t))
    power[-300:-290] += 400  # an episode in the last week
    values = np.column_stack([temp, hum, power, power * 0.9 + rng.normal(size=len(t))])
    channels = [
        "kitchen1_room_temperature_1",
        "kitchen1_room_humidity_2",
        "home_electric-combined_mains_3",
        "home_electric_appliance_4",
    ]
    areas = {channels[0]: "kitchen1", channels[1]: "kitchen1"}
    return from_hourly(name, 24 * 19000, channels, values, areas)


def test_split_houses_shares_and_seed() -> None:
    names = [f"home{i:03d}" for i in range(20)]
    split = us.split_houses(names)
    assert Counter(split.values()) == {"calibration": 12, "validation": 4, "test": 4}
    assert split == us.split_houses(names)
    assert set(us.split_houses(names[:3]).values()) == {"calibration", "validation", "test"}


def test_channel_info_maps_words_to_device_classes() -> None:
    rules = act.load_rules()
    assert rules.family(us.channel_info("sensor.x", "kitchen1_room_temperature_1")) == "temperature"
    assert rules.family(us.channel_info("sensor.x", "lounge2_room_humidity_5")) == "humidity"
    assert rules.family(us.channel_info("sensor.x", "home_electric-combined_mains_3")) == "power"
    assert rules.family(us.channel_info("sensor.x", "home_gas-pulse_gas_9")) == "energy"
    assert rules.family(us.channel_info("sensor.x", "Appliance3")) == "power"


def test_windows() -> None:
    h = house("home001", 1)
    assert us.windows(h, 90, "test") == [DAYS - 90]
    assert us.windows(h, 90, "calibration") == [20, 110]
    assert us.windows(h, 300, "calibration") == []


def test_analyse_house_scores_findings() -> None:
    rows = us.analyse_house(house("home001", 1), "test", 90, act.load_rules(), "ideal")
    assert rows
    types = {r["finding_type"] for r in rows}
    assert "correlation" in types
    for r in rows:
        assert r["split"] == "test"
        assert r["usefulness"] is not None
        assert r["cell"].split("|")[0] == r["finding_type"]
    # temperature and humidity of one room: same area, but different device classes
    th = [
        r
        for r in rows
        if {"kitchen1" in str(r["entity"]), "kitchen1" in str(r["related"])} == {True}
    ]
    assert all(r["trivial_rule"] != "T4" for r in th)


def test_allocate() -> None:
    take = us.allocate({"a": 5, "b": 100, "c": 300}, total=100, min_per_cell=20)
    assert take["a"] == 5
    assert take["b"] >= 20
    assert sum(take.values()) == 100
    assert take["c"] > take["b"]
    assert us.allocate({"a": 5}, total=100, min_per_cell=20) == {"a": 5}


def frame_rows(n: int, cell: str, split: str) -> list[dict[str, Any]]:
    return [
        {
            "sample_id": f"{cell}-{split}-{i}",
            "cell": cell,
            "split": split,
            "strength": i / n,
        }
        for i in range(n)
    ]


def test_stratified_sample_weights_and_human_sample() -> None:
    frame = (
        frame_rows(300, "correlation|kept|savings", "calibration")
        + frame_rows(10, "anomaly|kept|fault", "validation")
        + frame_rows(200, "anomaly|kept|none", "test")
    )
    sample = us.stratified_sample(frame, total=100, min_per_cell=20, human_per_set=5)
    assert len(sample) == 100
    by_cell = Counter(r["cell"] for r in sample)
    assert by_cell["anomaly|kept|fault"] == 10
    rare = next(r for r in sample if r["cell"] == "anomaly|kept|fault")
    assert rare["weight"] == 1.0
    big = next(r for r in sample if r["cell"] == "correlation|kept|savings")
    assert big["weight"] == pytest.approx(300 / by_cell["correlation|kept|savings"], abs=1e-3)
    human = Counter(r["split"] for r in sample if r["human"])
    assert human == {"validation": 5, "test": 5}
    # strength terciles are drawn evenly inside a cell
    s = [r["strength"] for r in sample if r["cell"] == "correlation|kept|savings"]
    assert min(s) < 1 / 3 < 2 / 3 < max(s)


def write_ideal(directory: Path) -> None:
    for i in range(1, 6):
        h = house(f"home{i:03d}", i)
        for c, ch in enumerate(h.channels):
            room, rest = ch.split("_", 1)
            parts = rest.rsplit("_", 1)
            name = f"home{i}_{room if room != 'home' else 'hall'}{1 if room == 'home' else ''}"
            name += f"_sensor{parts[1]}_{parts[0]}.csv.gz"
            with gzip.open(directory / name, "wt") as f:
                for k, v in enumerate(h.values[:, c]):
                    hour = np.datetime64(h.hours[k], "s").astype(str).replace("T", " ")
                    f.write(f"{hour},{v}\n")


def test_main_writes_the_sample(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_ideal(tmp_path)
    out = tmp_path / "sample.csv"
    assert (
        us.main(["--dataset", "ideal", "--data", str(tmp_path), "--out", str(out), "--total", "40"])
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["summary"]["houses"]["calibration"] == 3
    assert summary["summary"]["frame"] > 0
    assert summary["manifest"]["rules_version"] == act.load_rules().rulebook.version
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert 0 < len(rows) <= 40
    assert set(rows[0]) == set(us.COLUMNS)
