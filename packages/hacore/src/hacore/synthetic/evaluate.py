"""Evaluation of the correlation analysis on synthetic households (ADR 062 point 8a).

Runs the analysis pipeline on generated households (no lake: grid -> clean ->
residuals -> high-pass -> lagged correlations -> findings) and compares the findings
with the truth of each household. Metrics (ADR 062, Gate 1c criterion 6):

``fdr_strict``      share of findings between unrelated series (false by any reading)
``fdr``             as above, plus indirect and hidden-common relations reported
                    without the flag ``indirect``/``common_factor`` (the gate metric)
``recall_strong``   share of planted direct relations with |r| >= 0.5 that were found
``recall``          share of all planted direct relations (|r| 0.2-0.8) found
``duplicates_flagged``  share of duplicate pairs found that carry ``duplicate_suspect``
``seconds``         run time of the analysis

Each metric is reported per archetype and size, and as the 90th percentile over the
households of a cell (the worse household, not the average).

    python -m hacore.synthetic.evaluate --sizes 10,100 --seeds 4 --days 90
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from hacore import correlations as corr
from hacore.synthetic.generator import ARCHETYPES, Household, HouseholdSpec, Relation, generate

type Analysis = Callable[[Household], list[dict[str, Any]]]

FLAGGED_OK = {
    Relation.INDIRECT: "indirect",
    Relation.COMMON: "common_factor",
}


def current_method(params: corr.CorrelationParams | None = None) -> Analysis:
    """The correlation method in production (ADR 042) on a household's grid."""
    p = params or corr.CorrelationParams(
        window_days=10_000, outside_temp=None, top=100_000, timezone="Europe/Prague"
    )

    def run(h: Household) -> list[dict[str, Any]]:
        params_h = corr.CorrelationParams(
            window_days=p.window_days,
            max_lag=p.max_lag,
            min_coverage=p.min_coverage,
            min_r=p.min_r,
            max_q=p.max_q,
            top=p.top,
            outside_temp=h.outside_temp,
            timezone=h.spec.timezone,
        )
        grid = corr.clean(h.grid, params_h.min_coverage, h.outside_temp)
        resid, series = corr.residuals(grid, h.outside_temp)
        resid = corr.highpass(resid)
        if len(series) < 2:
            return []
        pairs = corr.lagged_correlations(resid, params_h.max_lag)
        return corr.select_findings(pairs, series, params_h, {})

    return run


@dataclass(frozen=True, slots=True)
class HouseholdMetrics:
    archetype: str
    n_series: int
    seed: int
    findings: int
    false_strict: int
    false: int
    fdr_strict: float
    fdr: float
    planted: int
    planted_strong: int
    recall: float | None
    recall_strong: float | None
    duplicates_flagged: float | None
    by_relation: dict[str, int]
    seconds: float


def _key(entity_id: str, state: str | None) -> str:
    return f"{entity_id}={state}" if state is not None else entity_id


def score(h: Household, findings: list[dict[str, Any]], seconds: float) -> HouseholdMetrics:
    by_relation: dict[str, int] = {r.value: 0 for r in Relation}
    false_strict = false = 0
    found: set[frozenset[str]] = set()
    dup_found = dup_flagged = 0
    for f in findings:
        a = _key(f["entity_id"], f["state"])
        b = _key(f["related_entity_id"], f["related_state"])
        rel = h.relation(a, b)
        by_relation[rel.value] += 1
        found.add(frozenset((a, b)))
        flags = set(f.get("flags", []))
        if rel is Relation.UNRELATED:
            false_strict += 1
            false += 1
        elif rel in FLAGGED_OK and FLAGGED_OK[rel] not in flags:
            false += 1
        if rel is Relation.DUPLICATE:
            dup_found += 1
            dup_flagged += "duplicate_suspect" in flags
    observed = set(h.keys)
    planted = [p for p in h.direct_pairs() if p[0] in observed and p[1] in observed]
    strong = [p for p in planted if p[2] >= 0.5]

    def recall(ps: list[tuple[str, str, float, int]]) -> float | None:
        if not ps:
            return None
        return sum(frozenset((a, b)) in found for a, b, _, _ in ps) / len(ps)

    n = len(findings)
    return HouseholdMetrics(
        archetype=h.spec.archetype,
        n_series=h.spec.n_series,
        seed=h.spec.seed,
        findings=n,
        false_strict=false_strict,
        false=false,
        fdr_strict=false_strict / n if n else 0.0,
        fdr=false / n if n else 0.0,
        planted=len(planted),
        planted_strong=len(strong),
        recall=recall(planted),
        recall_strong=recall(strong),
        duplicates_flagged=dup_flagged / dup_found if dup_found else None,
        by_relation=by_relation,
        seconds=round(seconds, 2),
    )


def evaluate(
    specs: Iterable[HouseholdSpec], analysis: Analysis | None = None
) -> list[HouseholdMetrics]:
    run = analysis or current_method()
    out = []
    for spec in specs:
        h = generate(spec)
        started = time.perf_counter()
        findings = run(h)
        out.append(score(h, findings, time.perf_counter() - started))
    return out


def summarise(metrics: list[HouseholdMetrics]) -> list[dict[str, Any]]:
    """Per archetype and size: pooled FDR, mean and 90th percentile over households."""
    cells: dict[tuple[str, int], list[HouseholdMetrics]] = {}
    for m in metrics:
        cells.setdefault((m.archetype, m.n_series), []).append(m)
    rows = []
    for (archetype, size), ms in sorted(cells.items()):

        def vals(name: str, ms: list[HouseholdMetrics] = ms) -> list[float]:
            return [v for m in ms if (v := getattr(m, name)) is not None]

        findings = sum(m.findings for m in ms)
        rows.append(
            {
                "archetype": archetype,
                "n_series": size,
                "households": len(ms),
                "findings": findings,
                "fdr_strict_pooled": round(sum(m.false_strict for m in ms) / findings, 4)
                if findings
                else 0.0,
                "fdr_pooled": round(sum(m.false for m in ms) / findings, 4) if findings else 0.0,
                "fdr_p90": round(float(np.percentile(vals("fdr"), 90)), 4),
                "recall_strong_mean": _mean(vals("recall_strong")),
                "recall_strong_p10": _pct(vals("recall_strong"), 10),
                "recall_mean": _mean(vals("recall")),
                "duplicates_flagged": _mean(vals("duplicates_flagged")),
                "seconds_max": max(m.seconds for m in ms),
            }
        )
    return rows


def _mean(v: list[float]) -> float | None:
    return round(float(np.mean(v)), 4) if v else None


def _pct(v: list[float], q: float) -> float | None:
    return round(float(np.percentile(v, q)), 4) if v else None


def specs(
    sizes: Iterable[int], seeds: int, days: int, archetypes: Iterable[str] = ARCHETYPES
) -> list[HouseholdSpec]:
    return [
        HouseholdSpec(archetype=a, n_series=n, days=days, seed=s)
        for a in archetypes
        for n in sizes
        for s in range(seeds)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.synthetic.evaluate")
    parser.add_argument("--sizes", default="10,100", help="comma separated series counts")
    parser.add_argument("--seeds", type=int, default=4, help="households per cell")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--archetypes", default=",".join(ARCHETYPES))
    parser.add_argument("--households", action="store_true", help="also print each household")
    args = parser.parse_args(argv)
    metrics = evaluate(
        specs(
            [int(x) for x in args.sizes.split(",")],
            args.seeds,
            args.days,
            args.archetypes.split(","),
        )
    )
    out: dict[str, Any] = {"method": "current (ADR 042)", "cells": summarise(metrics)}
    if args.households:
        out["households"] = [asdict(m) for m in metrics]
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
