"""Cross-household null on a public dataset (ADR 065, Gate 1c criterion 6).

A *mixed household* takes a few channels from each of several real houses. Every house
is shifted by whole weeks onto one common calendar, and the real start dates of any two
houses lie at least ``MIN_GAP_DAYS`` apart: the houses share neither the weather nor the
dates, and the day of the week is kept. Pairs within a house may be related; pairs
between houses are not, so every finding between houses is false.

Metrics per run (and summed over runs):

``cross_pairs``         series pairs between houses (the null pairs tested)
``findings``            all correlation findings, ``cross`` those between houses
``fdr_all``             cross / findings (all findings, flagged ones included)
``fdr_gate``            the same over findings without ``same_area``/``house_context``
                        (the Gate 1c metric)
``fdr_default_view``    the same over unflagged findings (what a user sees first)
``fdr_strict``          equal to fdr_all: a pair between houses is unrelated by any
                        reading (kept for comparison with the synthetic metrics)
``context_share``       share of findings flagged same_area/house_context; the rooms of
                        a house are its areas (IDEAL), so only pairs inside a house can
                        carry it: ``cross_context`` (between houses) must be 0
``null_runs_with_findings``  with one channel per house every pair is null: the share of
                        such runs with any finding estimates the familywise error rate

Anomalies on the same grid: findings per 100 series-weeks without planted events, then
recall of planted episodes (``magnitude`` x the series' robust deviation for 3-12 hours
in the recent week) and the findings that appear only after planting elsewhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np

from hacore import anomalies as an
from hacore import areas
from hacore import correlations as corr
from hacore.context import Context
from hacore.public import ideal, refit
from hacore.public.houses import FloatArray, House
from hacore.synthetic.evaluate import analyse_grid

# Real start dates of two houses in one mixed household lie at least this far apart.
MIN_GAP_DAYS = 28
# A channel takes part with at least this share of known hours in its window.
MIN_CHANNEL_COVERAGE = 0.8
# Common calendar: a Monday, far from any dataset, in UTC.
EPOCH = np.datetime64("2001-01-01T00", "h")
CONTEXT_FLAGS = frozenset({"same_area", "house_context"})
MAGNITUDES = (4.0, 6.0, 8.0)
# A series is heavy-tailed when its robust deviation is under this share of the spread
# of its residuals (a normal series has about 1).
HEAVY_TAIL_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class Placement:
    house: str
    start: np.datetime64  # real UTC start (a Monday midnight)
    channels: list[str]


@dataclass(slots=True)
class RunMetrics:
    seed: int
    houses: int
    series: int
    cross_pairs: int
    findings: int = 0
    cross: int = 0
    gate_findings: int = 0
    gate_cross: int = 0
    unflagged: int = 0
    unflagged_cross: int = 0
    context_flagged: int = 0
    cross_context: int = 0
    seconds: float = 0.0
    anomalies: int = 0
    series_weeks: int = 0
    change_points: int = 0
    planted: dict[str, int] = field(default_factory=dict)
    planted_found: dict[str, int] = field(default_factory=dict)
    # Diagnostics of the planted episodes, per magnitude: among the candidates at all
    # (one of the strongest episodes of its series in the recent week), and the p-value
    # and peak |z| of the strongest overlapping candidate.
    planted_candidate: dict[str, int] = field(default_factory=dict)
    planted_p: dict[str, list[float]] = field(default_factory=dict)
    planted_peak_z: dict[str, list[float]] = field(default_factory=dict)
    reference_quantiles: dict[str, float] = field(default_factory=dict)
    planted_shape: dict[str, int] = field(default_factory=dict)
    planted_found_shape: dict[str, int] = field(default_factory=dict)
    new_after_planting: int = 0


def _mondays(h: House, days: int) -> list[int]:
    """Day indices of ``h`` that are Mondays with ``days`` of data after them."""
    first = h.hours[0].astype("datetime64[D]")
    dow0 = int((first.astype(np.int64) + 3) % 7)  # 1970-01-01 was a Thursday (3)
    start = (7 - dow0) % 7
    return list(range(start, h.days - days + 1, 7))


def _channels_ok(h: House, day: int, days: int) -> list[int]:
    block = h.values[day * 24 : (day + days) * 24]
    known = (~np.isnan(block)).mean(axis=0)
    spread = np.nanstd(block, axis=0) if len(block) else np.zeros(block.shape[1])
    return [c for c in range(block.shape[1]) if known[c] >= MIN_CHANNEL_COVERAGE and spread[c] > 0]


def mixed_grid(
    houses: list[House], days: int, per_house: int, rng: np.random.Generator
) -> tuple[corr.Grid, list[Placement], Context] | None:
    """A mixed household of ``per_house`` channels from each house that fits (see the
    module docstring) and its context: the rooms of the houses as areas, no presence
    entity. None with fewer than two houses."""
    taken: list[np.datetime64] = []
    columns: list[FloatArray] = []
    series: list[corr.Series] = []
    placements: list[Placement] = []
    groups: dict[str, str] = {}
    for i in rng.permutation(len(houses)):
        h = houses[int(i)]
        options = []
        for day in _mondays(h, days):
            real = h.hours[day * 24]
            if all(
                abs((real - t).astype("timedelta64[D]").astype(int)) >= MIN_GAP_DAYS for t in taken
            ):
                ok = _channels_ok(h, day, days)
                if ok:
                    options.append((day, ok))
        if not options:
            continue
        day, ok = options[int(rng.integers(len(options)))]
        chosen = sorted(rng.choice(ok, size=min(per_house, len(ok)), replace=False).tolist())
        block = h.values[day * 24 : (day + days) * 24]
        for c in chosen:
            columns.append(block[:, c])
            entity = f"sensor.{h.house}_{h.channels[c].lower()}"
            series.append(corr.Series(entity, "mean"))
            if h.channels[c] in h.areas:
                groups[entity] = f"{h.house}:{h.areas[h.channels[c]]}"
        taken.append(h.hours[day * 24])
        placements.append(Placement(h.house, h.hours[day * 24], [h.channels[c] for c in chosen]))
    if len(placements) < 2:
        return None
    t = days * 24
    hours = EPOCH + np.arange(t).astype("timedelta64[h]")
    local_hour = np.arange(t) % 24
    local_dow = (np.arange(t) // 24) % 7 + 1  # EPOCH is a Monday
    grid = corr.Grid(
        hours=hours.astype("datetime64[us]"),
        local_hour=local_hour.astype(np.int64),
        local_dow=local_dow.astype(np.int64),
        series=series,
        values=np.column_stack(columns),
    )
    context = Context(groups=groups, group_flag=areas.SAME_AREA, driver_flag=areas.HOUSE_CONTEXT)
    return grid, placements, context


def _house(entity_id: str) -> str:
    return entity_id.split(".", 1)[1].split("_", 1)[0]


def _cross_pairs(series: list[corr.Series]) -> int:
    counts: dict[str, int] = {}
    for s in series:
        counts[_house(s.entity_id)] = counts.get(_house(s.entity_id), 0) + 1
    n = sum(counts.values())
    return (n * (n - 1) - sum(c * (c - 1) for c in counts.values())) // 2


def score_correlations(m: RunMetrics, findings: list[dict[str, Any]]) -> None:
    for f in findings:
        cross = _house(f["entity_id"]) != _house(f["related_entity_id"])
        flags = set(f["flags"])
        m.findings += 1
        m.cross += cross
        if flags & CONTEXT_FLAGS:
            m.context_flagged += 1
            m.cross_context += cross
        else:
            m.gate_findings += 1
            m.gate_cross += cross
        if not flags:
            m.unflagged += 1
            m.unflagged_cross += cross


def _robust_scale(grid: corr.Grid, params: an.AnomalyParams) -> tuple[FloatArray, FloatArray]:
    """Robust deviation of every series in the recent week, and whether the series is
    heavy-tailed: its robust deviation is under ``HEAVY_TAIL_RATIO`` of the 5-95 %
    spread of its baseline residuals (an appliance that idles most hours)."""
    t = len(grid.hours)
    first_recent = t - params.recent_days * 24
    idx = np.arange(t)
    baseline = (idx < first_recent) & (idx >= first_recent - params.baseline_days * 24)
    how = (grid.local_dow - 1) * 24 + grid.local_hour
    exp = an.expectation(grid.values, how, grid.local_hour, baseline, grid.series)
    scale = np.asarray(np.nanmedian(exp.scale[first_recent:], axis=0), dtype=np.float64)
    resid = grid.values[baseline] - exp.expected[baseline]
    spread = (np.nanpercentile(resid, 95, axis=0) - np.nanpercentile(resid, 5, axis=0)) / 3.29
    return scale, np.asarray(scale < HEAVY_TAIL_RATIO * spread)


def _anomalies(grid: corr.Grid, params: an.AnomalyParams) -> tuple[list[dict[str, Any]], int]:
    cps, _ = an.detect_change_points_v2(grid, params)
    found, _ = an.detect_anomalies_v2(grid, params, an._shift_hours(grid, cps))
    return found, len(cps)


def score_anomalies(
    m: RunMetrics, grid: corr.Grid, params: an.AnomalyParams, rng: np.random.Generator
) -> None:
    grid = corr.clean(grid, params.min_coverage, None)
    if len(grid.series) == 0 or len(grid.hours) <= params.recent_days * 24:
        return
    base, m.change_points = _anomalies(grid, params)
    m.anomalies = len(base)
    m.series_weeks = len(grid.series) * max(1, len(grid.hours) // (24 * 7))
    seen = {(f["entity_id"], f["period_start"]) for f in base}
    scale, heavy = _robust_scale(grid, params)
    t = len(grid.hours)
    first_recent = t - params.recent_days * 24
    k = max(1, len(grid.series) // 10)
    cols = rng.choice(
        len(grid.series), size=min(k * len(MAGNITUDES), len(grid.series)), replace=False
    )
    values = grid.values.copy()
    planted: list[tuple[str, float, int, int, str]] = []
    for n, j in enumerate(cols):
        if not np.isfinite(scale[j]) or scale[j] <= 0:
            continue
        mag = MAGNITUDES[n % len(MAGNITUDES)]
        length = int(rng.integers(3, 13))
        start = int(rng.integers(first_recent, t - length))
        values[start : start + length, j] += mag * scale[j] * rng.choice([-1.0, 1.0])
        shape = "heavy_tailed" if heavy[j] else "regular"
        planted.append((grid.series[j].entity_id, mag, start, start + length, shape))
    injected = corr.Grid(grid.hours, grid.local_hour, grid.local_dow, grid.series, values)
    # Every candidate with its p- and q-value; the reported ones are q <= max_q.
    everything = replace(params, max_q=1.0, top=1_000_000)
    cps, _ = an.detect_change_points_v2(injected, params)
    cands, stats = an.detect_anomalies_v2(injected, everything, an._shift_hours(injected, cps))
    m.reference_quantiles = stats.get("reference_quantiles", {})
    after = [f for f in cands if f["q_value"] <= params.max_q]
    hours = [corr._to_dt(h) for h in grid.hours]
    for entity, mag, a, b, shape in planted:
        key = f"{mag:g}"
        m.planted[key] = m.planted.get(key, 0) + 1
        m.planted_shape[shape] = m.planted_shape.get(shape, 0) + 1
        lo, hi = hours[a], hours[b - 1]
        overlap = [
            f
            for f in cands
            if f["entity_id"] == entity and f["period_start"] <= hi and f["period_end"] > lo
        ]
        if not overlap:
            continue
        best = min(overlap, key=lambda f: f["p_value"])
        m.planted_candidate[key] = m.planted_candidate.get(key, 0) + 1
        m.planted_p.setdefault(key, []).append(float(best["p_value"]))
        m.planted_peak_z.setdefault(key, []).append(abs(float(best["score"])))
        if best["q_value"] <= params.max_q:
            m.planted_found[key] = m.planted_found.get(key, 0) + 1
            m.planted_found_shape[shape] = m.planted_found_shape.get(shape, 0) + 1
    planted_entities = {p[0] for p in planted}
    m.new_after_planting = sum(
        1
        for f in after
        if (f["entity_id"], f["period_start"]) not in seen
        and f["entity_id"] not in planted_entities
    )


def run(
    houses: list[House],
    *,
    runs: int,
    days: int,
    per_house: int,
    seed: int,
    anomalies: bool = True,
) -> list[RunMetrics]:
    out = []
    for r in range(runs):
        rng = np.random.default_rng([seed, r])
        mixed = mixed_grid(houses, days, per_house, rng)
        if mixed is None:
            continue
        grid, placements, context = mixed
        params = corr.CorrelationParams(
            window_days=days, outside_temp=None, top=100_000, timezone="UTC", seed=seed + r
        )
        m = RunMetrics(
            seed=seed + r,
            houses=len(placements),
            series=len(grid.series),
            cross_pairs=_cross_pairs(grid.series),
        )
        started = time.perf_counter()
        score_correlations(m, analyse_grid(grid, params, context))
        m.seconds = round(time.perf_counter() - started, 2)
        if anomalies:
            score_anomalies(m, grid, an.AnomalyParams(timezone="UTC", seed=seed + r), rng)
        out.append(m)
    return out


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def summarise(metrics: list[RunMetrics], per_house: int) -> dict[str, Any]:
    tot = {
        k: sum(getattr(m, k) for m in metrics)
        for k in (
            "cross_pairs",
            "findings",
            "cross",
            "gate_findings",
            "gate_cross",
            "unflagged",
            "unflagged_cross",
            "context_flagged",
            "cross_context",
            "anomalies",
            "series_weeks",
            "change_points",
            "new_after_planting",
        )
    }
    planted: dict[str, int] = {}
    found: dict[str, int] = {}
    for m in metrics:
        for k, v in m.planted.items():
            planted[k] = planted.get(k, 0) + v
        for k, v in m.planted_found.items():
            found[k] = found.get(k, 0) + v
    candidate: dict[str, int] = {}
    p_values: dict[str, list[float]] = {}
    peak_z: dict[str, list[float]] = {}
    for m in metrics:
        for k, v in m.planted_candidate.items():
            candidate[k] = candidate.get(k, 0) + v
        for k, ps in m.planted_p.items():
            p_values.setdefault(k, []).extend(ps)
        for k, zs in m.planted_peak_z.items():
            peak_z.setdefault(k, []).extend(zs)
    quantiles = [m.reference_quantiles for m in metrics if m.reference_quantiles]
    shape: dict[str, int] = {}
    shape_found: dict[str, int] = {}
    for m in metrics:
        for k, v in m.planted_shape.items():
            shape[k] = shape.get(k, 0) + v
        for k, v in m.planted_found_shape.items():
            shape_found[k] = shape_found.get(k, 0) + v
    out: dict[str, Any] = {
        "runs": len(metrics),
        "channels_per_house": per_house,
        "houses_mean": _mean([m.houses for m in metrics]),
        "series_mean": _mean([m.series for m in metrics]),
        **tot,
        "fdr_all": _ratio(tot["cross"], tot["findings"]),
        "fdr_gate": _ratio(tot["gate_cross"], tot["gate_findings"]),
        "fdr_default_view": _ratio(tot["unflagged_cross"], tot["unflagged"]),
        "fdr_strict": _ratio(tot["cross"], tot["findings"]),
        "cross_per_1000_pairs": _ratio(1000 * tot["cross"], tot["cross_pairs"]),
        "context_share": _ratio(tot["context_flagged"], tot["findings"]),
        "seconds_p90": _pct([m.seconds for m in metrics], 90),
        "anomalies_per_100_series_weeks": _ratio(100 * tot["anomalies"], tot["series_weeks"]),
        "planted": planted,
        "recall_by_magnitude": {k: _ratio(found.get(k, 0), v) for k, v in sorted(planted.items())},
        "planted_by_shape": shape,
        "recall_by_shape": {k: _ratio(shape_found.get(k, 0), v) for k, v in sorted(shape.items())},
        "planted_candidate_share": {
            k: _ratio(candidate.get(k, 0), v) for k, v in sorted(planted.items())
        },
        "planted_p_median": {k: _pct(v, 50, 4) for k, v in sorted(p_values.items())},
        "planted_peak_z_median": {k: _pct(v, 50) for k, v in sorted(peak_z.items())},
        "reference_quantiles_median": {
            k: _pct([q[k] for q in quantiles], 50) for k in ("p50", "p90", "p99", "zero_share")
        }
        if quantiles
        else {},
    }
    if per_house == 1:
        out["null_runs_with_findings"] = _ratio(sum(m.findings > 0 for m in metrics), len(metrics))
    return out


def _mean(v: list[int]) -> float | None:
    return round(float(np.mean(v)), 1) if v else None


def _pct(v: list[float], q: float, digits: int = 2) -> float | None:
    return round(float(np.percentile(v, q)), digits) if v else None


def manifest(
    args: argparse.Namespace, houses: list[House], dataset_sha256: str | None
) -> dict[str, Any]:
    """What reproduces the run (ADR 065 point 3): code, data, parameters, seeds."""
    return {
        "hacore": version("hacore"),
        "git_sha": os.environ.get("GITHUB_SHA"),
        "dataset": args.dataset,
        "dataset_sha256": dataset_sha256,
        "houses": [h.house for h in houses],
        "params": {
            "runs": args.runs,
            "days": args.days,
            "channels_per_house": args.channels,
            "seed": args.seed,
            "min_gap_days": MIN_GAP_DAYS,
            "min_channel_coverage": MIN_CHANNEL_COVERAGE,
            "correlations": "v2 defaults (CorrelationParams)",
            "anomalies": "v2 defaults (AnomalyParams)",
        },
        "reproduce": (
            f"python -m hacore.public.crosshouse --dataset {args.dataset} --data <dir> "
            f"--runs {args.runs} --days {args.days} --channels {args.channels} --seed {args.seed}"
        ),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(dataset: str, data: Path) -> list[House]:
    if dataset == "refit":
        return refit.load_dir(data)
    if dataset == "ideal":
        return ideal.load_dir(data)
    raise ValueError(f"unknown dataset {dataset}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.public.crosshouse")
    parser.add_argument("--dataset", choices=["refit", "ideal"], required=True)
    parser.add_argument("--data", type=Path, required=True, help="directory with the house files")
    parser.add_argument("--archive", type=Path, help="downloaded archive, for its SHA-256")
    parser.add_argument(
        "--dataset-hash", help="hash of the data used (e.g. the member list from fetch)"
    )
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument(
        "--channels", type=int, default=3, help="channels per house (1 = pure null)"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-anomalies", action="store_true")
    args = parser.parse_args(argv)
    houses = load(args.dataset, args.data)
    out = {
        "manifest": manifest(
            args, houses, sha256(args.archive) if args.archive else args.dataset_hash
        ),
        "summary": summarise(
            run(
                houses,
                runs=args.runs,
                days=args.days,
                per_house=args.channels,
                seed=args.seed,
                anomalies=not args.no_anomalies,
            ),
            args.channels,
        ),
    }
    json.dump(out, sys.stdout, indent=2, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
