"""Sample of findings on public data for the usefulness rating (methodology 8.4,
preregistered in ``docs/evaluation/usefulness-prereg.md``).

Every house of a dataset belongs to exactly one set: calibration (60 %), validation
(20 %) or test (20 %). The v2 analyses run on each house alone (its rooms are its
areas) in windows of ``days``: calibration houses in up to ``MAX_WINDOWS`` windows
over their history, validation and test houses in their last window (forward
simulation). Every finding is scored with the usefulness rules (ADR 068); the channel
names are mapped to device classes so that the rulebook can sort them into families.

The frame is stratified by finding type x trivial x action category; every cell gets
at least ``MIN_PER_CELL`` findings where it has them, the rest of ``total`` is spread
in proportion to the cells' sizes; inside a cell the draw alternates over the strength
terciles. The human sample is ``HUMAN_PER_SET`` findings drawn so from validation and
from test each (at least ``MIN_HUMAN_PER_CELL`` per cell); the rest of ``total`` comes
from calibration. ``weight`` = cell size / drawn inside the finding's set.

    python -m hacore.public.usefulness_sample --dataset ideal --data <dir> --out sample.csv

The CSV holds findings of the public dataset (CC BY 4.0, cite it); the JSON on stdout
holds only counts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np

from hacore import actionability as act
from hacore import anomalies as an
from hacore import areas, usefulness
from hacore import correlations as corr
from hacore.context import Context
from hacore.public.crosshouse import MIN_CHANNEL_COVERAGE, load
from hacore.public.houses import House
from hacore.synthetic.evaluate import analyse_grid

SEED = 20261125
SPLITS = (("calibration", 0.6), ("validation", 0.2), ("test", 0.2))
MAX_WINDOWS = 4
PILOT = 20
TOTAL = 1000
MIN_PER_CELL = 20
HUMAN_PER_SET = 100
MIN_HUMAN_PER_CELL = 5
# Action categories of the strata: none and unclassified form one cell.
CATEGORY_CELLS = ("fault", "savings", "automation", "behaviour", "none")

# Words of a channel name -> (device class, unit) for the rulebook.
CHANNEL_CLASSES = (
    ("humidity", "humidity", "%"),
    ("temperature", "temperature", "°C"),
    ("light", "illuminance", "lx"),
    ("gas", "gas", "m³"),
    ("electric", "power", "W"),
    ("aggregate", "power", "W"),
    ("appliance", "power", "W"),
    ("power", "power", "W"),
)

COLUMNS = (
    "sample_id",
    "dataset",
    "house",
    "split",
    "window_start",
    "finding_type",
    "entity",
    "related",
    "lag_hours",
    "score",
    "q_value",
    "strength",
    "evidence",
    "nontriviality",
    "usefulness",
    "action_category",
    "trivial_rule",
    "flags",
    "period_start",
    "period_end",
    "observed",
    "expected",
    "cell",
    "weight",
    "human",
)


def split_houses(houses: list[str], seed: int = SEED) -> dict[str, str]:
    """House -> set; the shares of ``SPLITS``, every set at least one house when
    there are three or more."""
    order = [houses[int(i)] for i in np.random.default_rng(seed).permutation(len(houses))]
    n = len(order)
    n_val = max(1, round(n * SPLITS[1][1])) if n >= 3 else 0
    n_test = max(1, round(n * SPLITS[2][1])) if n >= 3 else 0
    out = {}
    for i, h in enumerate(order):
        out[h] = "validation" if i < n_val else "test" if i < n_val + n_test else "calibration"
    return out


def channel_info(entity_id: str, channel: str) -> act.EntityInfo:
    words = channel.lower()
    for word, device_class, unit in CHANNEL_CLASSES:
        if word in words:
            return act.EntityInfo(entity_id, channel, device_class, unit)
    return act.EntityInfo(entity_id, channel)


def windows(h: House, days: int, split: str) -> list[int]:
    """Start days of the analysed windows: calibration up to ``MAX_WINDOWS`` without
    overlap over the history, else the last window (ends with the house's data)."""
    last = h.days - days
    if last < 0:
        return []
    if split != "calibration":
        return [last]
    starts = list(range(last, -1, -days))[:MAX_WINDOWS]
    return sorted(starts)


def house_grid(h: House, day: int, days: int) -> tuple[corr.Grid, list[str]] | None:
    block = h.values[day * 24 : (day + days) * 24]
    known = (~np.isnan(block)).mean(axis=0)
    spread = np.nanstd(block, axis=0) if len(block) else np.zeros(block.shape[1])
    ok = [c for c in range(block.shape[1]) if known[c] >= MIN_CHANNEL_COVERAGE and spread[c] > 0]
    if len(ok) < 2:
        return None
    hours = h.hours[day * 24 : (day + days) * 24]
    epoch_hours = hours.astype("datetime64[h]").astype(np.int64)
    channels = [h.channels[c] for c in ok]
    grid = corr.Grid(
        hours=hours.astype("datetime64[us]"),
        local_hour=(epoch_hours % 24).astype(np.int64),
        local_dow=((epoch_hours // 24 + 3) % 7 + 1).astype(np.int64),  # 1970-01-01: Thu
        series=[corr.Series(_entity(h, c), "mean") for c in channels],
        values=block[:, ok],
    )
    return grid, channels


def _entity(h: House, channel: str) -> str:
    return f"sensor.{h.house}_{channel.lower()}"


def scorer_of(h: House, channels: list[str], rules: act.Rules) -> tuple[Context, usefulness.Scorer]:
    infos = [channel_info(_entity(h, c), c) for c in channels]
    groups = {_entity(h, c): f"{h.house}:{h.areas[c]}" for c in channels if c in h.areas}
    entity_areas = {e: areas.EntityArea(g, areas.SOURCE_HA) for e, g in groups.items()}
    meta = act.metadata(infos, entity_areas, {}, rules)
    context = Context(groups=groups, group_flag=areas.SAME_AREA, driver_flag=areas.HOUSE_CONTEXT)
    return context, usefulness.Scorer(meta, rules.rulebook)


def analyse_house(
    h: House, split: str, days: int, rules: act.Rules, dataset: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cparams = corr.CorrelationParams(window_days=days, method="v2")
    aparams = an.AnomalyParams(changepoint_days=days)
    for day in windows(h, days, split):
        built = house_grid(h, day, days)
        if built is None:
            continue
        grid, channels = built
        context, scorer = scorer_of(h, channels, rules)
        window_start = str(h.hours[day * 24])
        rows = [(corr.FINDING_TYPE, f) for f in analyse_grid(grid, cparams, context, scorer)]
        cps, _ = an.detect_change_points_v2(grid, aparams)
        found, _ = an.detect_anomalies_v2(grid, aparams, an._shift_hours(grid, cps))
        rows += [(an.ANOMALY, f) for f in an.rescore(found, scorer, an.ANOMALY, aparams.max_q)]
        rows += [
            (an.CHANGE_POINT, f) for f in an.rescore(cps, scorer, an.CHANGE_POINT, aparams.max_q)
        ]
        for ftype, f in rows:
            out.append(_row(dataset, h.house, split, window_start, ftype=ftype, f=f))
    return out


def _row(
    dataset: str, house: str, split: str, window_start: str, *, ftype: str, f: dict[str, Any]
) -> dict[str, Any]:
    raw = f"{dataset}|{house}|{window_start}|{ftype}|{f['finding_id']}"
    category = str(f.get("action_category") or usefulness.UNCLASSIFIED)
    trivial = f.get("trivial_rule") is not None
    return {
        "sample_id": hashlib.sha256(raw.encode()).hexdigest()[:16],
        "dataset": dataset,
        "house": house,
        "split": split,
        "window_start": window_start,
        "finding_type": ftype,
        "entity": f["entity_id"],
        "related": f.get("related_entity_id"),
        "lag_hours": f.get("lag_hours"),
        "score": f.get("score"),
        "q_value": f.get("q_value"),
        "strength": f.get("strength"),
        "evidence": f.get("evidence"),
        "nontriviality": f.get("nontriviality"),
        "usefulness": f.get("usefulness"),
        "action_category": category,
        "trivial_rule": f.get("trivial_rule"),
        "flags": " ".join(f.get("flags") or []),
        "period_start": str(f.get("period_start") or ""),
        "period_end": str(f.get("period_end") or ""),
        "observed": f.get("observed"),
        "expected": f.get("expected"),
        "cell": cell_of(ftype, trivial, category),
    }


def cell_of(finding_type: str, trivial: bool, category: str) -> str:
    cat = category if category in CATEGORY_CELLS else "none"
    return f"{finding_type}|{'trivial' if trivial else 'kept'}|{cat}"


def allocate(sizes: dict[str, int], total: int, min_per_cell: int) -> dict[str, int]:
    """Findings drawn per cell: at least ``min_per_cell`` (or all a cell has), the rest
    in proportion to the cells' remaining sizes, never more than a cell has."""
    take = {c: min(n, min_per_cell) for c, n in sizes.items()}
    left = total - sum(take.values())
    while left > 0:
        room = {c: sizes[c] - take[c] for c in sizes if sizes[c] > take[c]}
        if not room:
            break
        whole = sum(room.values())
        added = 0
        for c, r in sorted(room.items()):
            extra = min(r, max(1, int(left * r / whole)), left - added)
            take[c] += extra
            added += extra
            if added >= left:
                break
        left -= added
    return take


def _draw(
    rows: list[dict[str, Any]], total: int, min_per_cell: int, rng: np.random.Generator
) -> list[dict[str, Any]]:
    """Stratified draw of ``total`` rows; ``weight`` = cell size / drawn in that cell."""
    cells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cells[row["cell"]].append(row)
    take = allocate({c: len(r) for c, r in cells.items()}, total, min_per_cell)
    picked: list[dict[str, Any]] = []
    for c in sorted(cells):
        members = cells[c]
        k = take[c]
        if not k:
            continue
        strength = np.array([float(r["strength"] or 0) for r in members])
        cuts = np.quantile(strength, [1 / 3, 2 / 3]) if len(members) >= 3 else np.array([])
        terciles = [
            [
                members[i]
                for i in rng.permutation(np.flatnonzero(np.searchsorted(cuts, strength) == t))
            ]
            for t in range(len(cuts) + 1)
        ]
        chosen: list[dict[str, Any]] = []
        while len(chosen) < k:
            for t in terciles:
                if t and len(chosen) < k:
                    chosen.append(t.pop())
        picked += [{**r, "weight": round(len(members) / k, 4)} for r in chosen]
    return picked


def stratified_sample(
    frame: list[dict[str, Any]],
    *,
    total: int = TOTAL,
    min_per_cell: int = MIN_PER_CELL,
    human_per_set: int = HUMAN_PER_SET,
    seed: int = SEED,
) -> list[dict[str, Any]]:
    """The human sample (``human_per_set`` from validation and from test, stratified in
    each set) and the rest of ``total`` from calibration (stratified), all for AI rating.

    The sets are drawn separately: validation and test houses have one window each, so a
    draw over the whole frame would hold far fewer than ``human_per_set`` of them.
    ``weight`` refers to the frame of the finding's set (calibration, validation, test).
    """
    rng = np.random.default_rng(seed)
    by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in frame:
        by_split[row["split"]].append(row)
    picked: list[dict[str, Any]] = []
    for split in ("validation", "test"):
        human = _draw(by_split[split], human_per_set, MIN_HUMAN_PER_CELL, rng)
        picked += [{**r, "human": True} for r in human]
    rest = max(0, total - len(picked))
    picked += [
        {**r, "human": False} for r in _draw(by_split["calibration"], rest, min_per_cell, rng)
    ]
    return sorted(picked, key=lambda r: r["sample_id"])


def pilot_sample(
    frame: list[dict[str, Any]], sample: list[dict[str, Any]], n: int, seed: int = SEED
) -> list[dict[str, Any]]:
    """``n`` findings for the pilot of the rubric (prereg section 3): calibration
    findings outside the sample, stratified as the sample. Validation and test stay
    untouched (their reserve serves a repeated gate)."""
    drawn = {r["sample_id"] for r in sample}
    rest = [r for r in frame if r["split"] == "calibration" and r["sample_id"] not in drawn]
    rng = np.random.default_rng([seed, 1])
    picked = _draw(rest, n, 1, rng)
    return sorted(({**r, "human": True} for r in picked), key=lambda r: r["sample_id"])


@dataclass(frozen=True, slots=True)
class Summary:
    houses: dict[str, int]
    frame: int
    sampled: int
    human: dict[str, int]
    cells: dict[str, dict[str, int]]
    coverage: float | None
    trivial_share: float | None
    channel_classes: dict[str, int]


def summarise(
    houses: dict[str, str],
    frame: list[dict[str, Any]],
    sample: list[dict[str, Any]],
    channel_classes: Counter[str],
) -> Summary:
    sizes = Counter(r["cell"] for r in frame)
    drawn = Counter(r["cell"] for r in sample)
    classified = sum(r["action_category"] != usefulness.UNCLASSIFIED for r in frame)
    trivial = sum(r["trivial_rule"] is not None for r in frame)
    return Summary(
        houses=dict(Counter(houses.values())),
        frame=len(frame),
        sampled=len(sample),
        human=dict(Counter(r["split"] for r in sample if r["human"])),
        cells={c: {"frame": sizes[c], "sampled": drawn[c]} for c in sorted(sizes)},
        coverage=round(classified / len(frame), 3) if frame else None,
        trivial_share=round(trivial / len(frame), 3) if frame else None,
        channel_classes=dict(channel_classes),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.public.usefulness_sample")
    parser.add_argument("--dataset", choices=["refit", "ideal"], required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="CSV of the sampled findings")
    parser.add_argument("--dataset-hash")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--total", type=int, default=TOTAL)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--pilot-out", type=Path, help="CSV of the pilot findings")
    parser.add_argument("--pilot", type=int, default=PILOT)
    args = parser.parse_args(argv)
    houses = load(args.dataset, args.data)
    split = split_houses([h.house for h in houses], args.seed)
    rules = act.load_rules()
    classes: Counter[str] = Counter()
    frame: list[dict[str, Any]] = []
    for h in houses:
        for c in h.channels:
            classes[rules.family(channel_info(_entity(h, c), c))] += 1
        frame += analyse_house(h, split[h.house], args.days, rules, args.dataset)
    sample = stratified_sample(frame, total=args.total, seed=args.seed)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sample)
    if args.pilot_out:
        with args.pilot_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(pilot_sample(frame, sample, args.pilot, args.seed))
    summary = summarise(split, frame, sample, classes)
    out = {
        "manifest": {
            "hacore": version("hacore"),
            "dataset": args.dataset,
            "dataset_hash": args.dataset_hash,
            "days": args.days,
            "total": args.total,
            "seed": args.seed,
            "rules_version": rules.rulebook.version,
        },
        "summary": asdict(summary),
    }
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
