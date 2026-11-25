"""Run time and memory of the analyses on one synthetic household (Gate 1c point 6,
PR 6): correlations v2 and anomalies/change points v2, as the daily run does them.

    python -m hacore.synthetic.benchmark --series 1000 --days 90

Prints JSON: seconds per analysis, peak RSS of the process, machine. Targets (ADR
065): correlations of 1000 series x 90 days in 12 s on amd64 (15 s if justified),
measured on arm64 as well.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import sys
import time

from hacore import anomalies as an
from hacore import correlations as corr
from hacore.synthetic.evaluate import analyse_grid
from hacore.synthetic.generator import HouseholdSpec, generate


def _rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # kB on Linux, bytes on macOS
    return round(peak / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hacore.synthetic.benchmark")
    parser.add_argument("--series", type=int, default=1000)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--archetype", default="power_user")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    t0 = time.perf_counter()
    h = generate(
        HouseholdSpec(
            archetype=args.archetype, n_series=args.series, days=args.days, seed=args.seed
        )
    )
    t1 = time.perf_counter()
    params = corr.CorrelationParams(
        window_days=args.days,
        outside_temp=h.outside_temp,
        timezone=h.spec.timezone,
        seed=args.seed,
    )
    findings = analyse_grid(h.grid, params)
    t2 = time.perf_counter()
    ap = an.AnomalyParams(timezone=h.spec.timezone, outside_temp=h.outside_temp)
    grid = corr.clean(h.grid, ap.min_coverage, h.outside_temp)
    cps, _ = an.detect_change_points_v2(grid, ap)
    found, _ = an.detect_anomalies_v2(grid, ap, an._shift_hours(grid, cps))
    t3 = time.perf_counter()
    out = {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "series": len(h.grid.series),
        "days": args.days,
        "generate_seconds": round(t1 - t0, 2),
        "correlations_seconds": round(t2 - t1, 2),
        "correlation_findings": len(findings),
        "anomalies_seconds": round(t3 - t2, 2),
        "anomalies": len(found),
        "change_points": len(cps),
        "peak_rss_mib": _rss_mib(),
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
