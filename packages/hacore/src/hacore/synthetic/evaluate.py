"""Evaluation of the correlation analysis on synthetic households (ADR 062 point 8a).

Runs the analysis pipeline on generated households (no lake: grid -> clean ->
residuals -> high-pass -> lagged correlations -> findings) and compares the findings
with the truth of each household. Metrics (ADR 062, Gate 1c criterion 6):

``fdr_strict``      share of findings between unrelated series (false by any reading)
``fdr``             as above, plus indirect and hidden-common relations reported
                    without the flag ``indirect``/``common_factor``, over the findings
                    without ``same_area`` (the gate metric, ADR 064)
``fdr_all``         the same over all findings, ``same_area`` ones included and not
                    excused by that flag (the overall view, as before ADR 064)
``same_area_share`` share of findings flagged ``same_area``
``area_coverage``   share of room sensors whose area the analysis knows (HA or name);
                    the summary reports the gate FDR apart for households below 90 %
``recall_strong``   share of planted direct relations with |r| >= 0.5 that were found
``recall``          share of all planted direct relations (|r| 0.2-0.8) found
``duplicates_flagged``  share of duplicate pairs found that carry ``duplicate_suspect``
``structure_flagged``   share of found planted chains (A -> C -> B) and planted hidden
                    common drivers with at least four observed children that carry
                    ``indirect`` / ``common_factor`` (reported)
``structure_strong_flagged``  the same for strong structures (every link or loading
                    >= 0.5): Gate 1c >= 80 %; the share over all structures, weak
                    ones included, is reported
``structure_unidentifiable``  found pairs of planted hidden drivers with only three
                    observed children: no observational test can tell them from
                    direct relations (no tetrad), reported apart
``direct_overflagged``  share of found planted direct relations wrongly flagged
                    ``indirect`` / ``common_factor`` (the price of the flags)
``occupancy_common``    findings whose only cause is the occupancy of the home or a
                    room (hidden, local): reported apart, a known limit of global
                    factors
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

# A planted structure is strong when each of its links (or loadings) is at least this.
STRONG_LINK = 0.5
# Households with a smaller share of room sensors in a known area are reported apart.
INCOMPLETE_AREAS = 0.9

FLAGGED_OK = {
    Relation.INDIRECT: "indirect",
    Relation.COMMON: "common_factor",
}


def current_method(params: corr.CorrelationParams | None = None, *, method: str = "v2") -> Analysis:
    """The correlation analysis of ``hacore.correlations`` on a household's grid
    (``method`` v2 = ADR 062, v1 = ADR 042)."""
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
            method=method,
            seed=h.spec.seed,
        )
        grid = corr.clean(h.grid, params_h.min_coverage, h.outside_temp)
        resid, series = corr.residuals(grid, h.outside_temp)
        resid = corr.highpass(resid)
        if len(series) < 2:
            return []
        if method == "v1":
            pairs = corr.lagged_correlations(resid, params_h.max_lag)
        else:
            pairs, _ = corr.lagged_correlations_v2(
                resid,
                params_h.max_lag,
                min_r=params_h.min_r,
                max_q=params_h.max_q,
                seed=params_h.seed,
            )
        return corr.select_findings(pairs, series, params_h, {}, h.entity_areas())

    return run


@dataclass(frozen=True, slots=True)
class HouseholdMetrics:
    archetype: str
    n_series: int
    seed: int
    findings: int
    false_strict: int
    false: int
    false_all: int
    gate_findings: int
    fdr_strict: float
    fdr: float
    fdr_all: float
    same_area: int
    area_coverage: float | None
    planted: int
    planted_strong: int
    recall: float | None
    recall_strong: float | None
    duplicates_flagged: float | None
    structure_found: int
    structure_flagged: float | None
    structure_strong_found: int
    structure_strong_flagged: float | None
    structure_unidentifiable: int
    direct_overflagged: float | None
    occupancy_common: int
    by_relation: dict[str, int]
    seconds: float


def _key(entity_id: str, state: str | None) -> str:
    return f"{entity_id}={state}" if state is not None else entity_id


def _children(h: Household, key: str) -> int:
    """Observed children of the planted hidden driver of ``key``."""
    stem = key.split("=", maxsplit=1)[0].rsplit("_", 1)[0] + "_"
    return sum(k.startswith(stem) for k in h.keys)


def _strength(h: Household, a: str, b: str, rel: Relation) -> float:
    """Weakest planted link of a planted chain (a -> c -> b) or of the two loadings of a
    pair sharing a planted hidden driver."""
    stem = a.split("=", maxsplit=1)[0].rsplit("_", 1)[0] + "_"
    nodes = {a, b}
    if rel is Relation.INDIRECT:
        nodes |= {k for k in h.keys if k.startswith(stem)}
    weights = [p.weight for k in nodes for p in h.parents.get(k, []) if p.weight]
    return min(weights) if weights else 0.0


def score(h: Household, findings: list[dict[str, Any]], seconds: float) -> HouseholdMetrics:
    by_relation: dict[str, int] = {r.value: 0 for r in Relation}
    false_strict = false = false_all = same_area = 0
    found: set[frozenset[str]] = set()
    dup_found = dup_flagged = 0
    struct_found = struct_flagged = unidentifiable = occupancy = 0
    strong_found = strong_flagged = 0
    direct_found = direct_flagged = 0
    for f in findings:
        a = _key(f["entity_id"], f["state"])
        b = _key(f["related_entity_id"], f["related_state"])
        rel = h.relation(a, b)
        by_relation[rel.value] += 1
        found.add(frozenset((a, b)))
        flags = set(f.get("flags", []))
        explained_by_area = "same_area" in flags
        same_area += explained_by_area
        wrong = rel is Relation.UNRELATED or (rel in FLAGGED_OK and FLAGGED_OK[rel] not in flags)
        false_all += wrong
        if rel is Relation.UNRELATED:
            false_strict += 1
        if wrong and not explained_by_area:
            false += 1
        if rel is Relation.DUPLICATE:
            dup_found += 1
            dup_flagged += "duplicate_suspect" in flags
        is_planted = any(".planted_" in k for k in (a, b))
        structural = bool(flags & {"indirect", "common_factor"})
        if rel in FLAGGED_OK and is_planted:
            if rel is Relation.COMMON and _children(h, a) < 4:
                unidentifiable += 1
            else:
                struct_found += 1
                struct_flagged += structural
                if _strength(h, a, b, rel) >= STRONG_LINK:
                    strong_found += 1
                    strong_flagged += structural
        if rel is Relation.DIRECT and is_planted:
            direct_found += 1
            direct_flagged += structural
        if rel is Relation.COMMON and not is_planted:
            hidden = h.shared_hidden(a, b)
            if hidden and all(x == "h:home" or x.startswith("h:room_") for x in hidden):
                occupancy += 1
    observed = set(h.keys)
    planted = [p for p in h.direct_pairs() if p[0] in observed and p[1] in observed]
    strong = [p for p in planted if p[2] >= 0.5]

    def recall(ps: list[tuple[str, str, float, int]]) -> float | None:
        if not ps:
            return None
        return sum(frozenset((a, b)) in found for a, b, _, _ in ps) / len(ps)

    n = len(findings)
    gate = n - same_area
    return HouseholdMetrics(
        archetype=h.spec.archetype,
        n_series=h.spec.n_series,
        seed=h.spec.seed,
        findings=n,
        false_strict=false_strict,
        false=false,
        false_all=false_all,
        gate_findings=gate,
        fdr_strict=false_strict / n if n else 0.0,
        fdr=false / gate if gate else 0.0,
        fdr_all=false_all / n if n else 0.0,
        same_area=same_area,
        area_coverage=h.area_coverage(),
        planted=len(planted),
        planted_strong=len(strong),
        recall=recall(planted),
        recall_strong=recall(strong),
        duplicates_flagged=dup_flagged / dup_found if dup_found else None,
        structure_found=struct_found,
        structure_flagged=struct_flagged / struct_found if struct_found else None,
        structure_strong_found=strong_found,
        structure_strong_flagged=strong_flagged / strong_found if strong_found else None,
        structure_unidentifiable=unidentifiable,
        direct_overflagged=direct_flagged / direct_found if direct_found else None,
        occupancy_common=occupancy,
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
                "gate_findings": sum(m.gate_findings for m in ms),
                "fdr_pooled": _ratio(sum(m.false for m in ms), sum(m.gate_findings for m in ms)),
                "fdr_all_pooled": _ratio(sum(round(m.fdr_all * m.findings) for m in ms), findings),
                "same_area_share": _ratio(sum(m.same_area for m in ms), findings),
                "area_coverage_mean": _mean(vals("area_coverage")),
                "fdr_pooled_incomplete_areas": _ratio(
                    sum(m.false for m in ms if (m.area_coverage or 0) < INCOMPLETE_AREAS),
                    sum(m.gate_findings for m in ms if (m.area_coverage or 0) < INCOMPLETE_AREAS),
                ),
                "fdr_p90": round(float(np.percentile(vals("fdr"), 90)), 4),
                "recall_strong_mean": _mean(vals("recall_strong")),
                "recall_strong_p10": _pct(vals("recall_strong"), 10),
                "recall_mean": _mean(vals("recall")),
                "duplicates_flagged": _mean(vals("duplicates_flagged")),
                "structure_found": sum(m.structure_found for m in ms),
                "structure_flagged_pooled": round(
                    sum((m.structure_flagged or 0) * m.structure_found for m in ms)
                    / max(sum(m.structure_found for m in ms), 1),
                    4,
                ),
                "structure_strong_found": sum(m.structure_strong_found for m in ms),
                "structure_strong_flagged_pooled": round(
                    sum((m.structure_strong_flagged or 0) * m.structure_strong_found for m in ms)
                    / max(sum(m.structure_strong_found for m in ms), 1),
                    4,
                ),
                "structure_unidentifiable": sum(m.structure_unidentifiable for m in ms),
                "direct_overflagged": _mean(vals("direct_overflagged")),
                "occupancy_common_share": round(sum(m.occupancy_common for m in ms) / findings, 4)
                if findings
                else 0.0,
                "seconds_max": max(m.seconds for m in ms),
            }
        )
    return rows


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _mean(v: list[float]) -> float | None:
    return round(float(np.mean(v)), 4) if v else None


def _pct(v: list[float], q: float) -> float | None:
    return round(float(np.percentile(v, q)), 4) if v else None


def specs(
    sizes: Iterable[int],
    seeds: int,
    days: int,
    archetypes: Iterable[str] = ARCHETYPES,
    *,
    area_coverage: float | None = None,
    descriptive_names: float | None = None,
) -> list[HouseholdSpec]:
    return [
        HouseholdSpec(
            archetype=a,
            n_series=n,
            days=days,
            seed=s,
            area_coverage=area_coverage,
            descriptive_names=descriptive_names,
        )
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
    parser.add_argument("--method", choices=["v1", "v2"], default="v2")
    parser.add_argument(
        "--area-coverage", type=float, help="share of rooms with an HA area (default: drawn)"
    )
    parser.add_argument(
        "--descriptive-names",
        type=float,
        help="share of rooms without an area named after the room (default: drawn)",
    )
    parser.add_argument("--households", action="store_true", help="also print each household")
    args = parser.parse_args(argv)
    metrics = evaluate(
        specs(
            [int(x) for x in args.sizes.split(",")],
            args.seeds,
            args.days,
            args.archetypes.split(","),
            area_coverage=args.area_coverage,
            descriptive_names=args.descriptive_names,
        ),
        current_method(method=args.method),
    )
    out: dict[str, Any] = {"method": args.method, "cells": summarise(metrics)}
    if args.households:
        out["households"] = [asdict(m) for m in metrics]
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
