"""Analysis settings an admin may change at run time (ADR 060).

The ingest reads its settings from the environment (``XOMTRA_*``). The analysis
thresholds below can also be stored in the database by the admin GUI; a stored value
wins over the environment, which wins over the default. ``SPECS`` is the one list of
those keys with their bounds: the admin GUI validates with it, the ingest applies the
stored values through its own settings model (a test keeps both in step).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

type Kind = Literal["float", "int", "bool", "choice"]
type Value = float | int | bool | str


@dataclass(frozen=True, slots=True)
class Spec:
    key: str
    kind: Kind
    default: Value
    group: str
    label: str
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    lo_open: bool = False  # lo itself is not allowed
    hi_open: bool = False
    choices: tuple[str, ...] = ()

    @property
    def env(self) -> str:
        return f"XOMTRA_{self.key.upper()}"

    def bounds(self) -> str:
        if self.kind == "choice":
            return " | ".join(self.choices)
        if self.kind == "bool":
            return "on | off"
        lo = "" if self.lo is None else f"{'(' if self.lo_open else '['}{self.lo:g}"
        hi = "" if self.hi is None else f"{self.hi:g}{')' if self.hi_open else ']'}"
        if self.lo is not None and self.hi is not None:
            return f"{lo}, {hi}"
        if self.lo is not None:
            return f"{'>' if self.lo_open else '>='} {self.lo:g}"
        return f"{'<' if self.hi_open else '<='} {self.hi:g}" if self.hi is not None else ""

    def parse(self, raw: Any) -> Value:
        """The value of ``raw`` (form text or stored JSON); ValueError if invalid."""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "on", "yes"}:
                return True
            if text in {"0", "false", "off", "no"}:
                return False
            raise ValueError(f"{self.key}: expected on or off")
        if self.kind == "choice":
            text = str(raw).strip()
            if text not in self.choices:
                raise ValueError(f"{self.key}: expected one of {', '.join(self.choices)}")
            return text
        try:
            number = float(str(raw).strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"{self.key}: not a number") from None
        if not math.isfinite(number):
            raise ValueError(f"{self.key}: not a finite number")
        if self.kind == "int":
            if number != int(number):
                raise ValueError(f"{self.key}: expected a whole number")
            number = int(number)
        if self.lo is not None and (number < self.lo or (self.lo_open and number == self.lo)):
            raise ValueError(f"{self.key}: out of range {self.bounds()}")
        if self.hi is not None and (number > self.hi or (self.hi_open and number == self.hi)):
            raise ValueError(f"{self.key}: out of range {self.bounds()}")
        return number


CORR = "Correlations"
ANOM = "Anomalies"
CHANGE = "Change points"

SPECS: tuple[Spec, ...] = (
    Spec(
        "correlation_max_q",
        "float",
        0.01,
        CORR,
        "Target false discovery rate (q)",
        "Benjamini-Hochberg over all pairs: expected share of false findings.",
        lo=0,
        hi=1,
        lo_open=True,
    ),
    Spec(
        "correlation_min_r",
        "float",
        0.3,
        CORR,
        "Minimum |r|",
        "Smallest correlation worth reporting, whatever the significance.",
        lo=0,
        hi=1,
        lo_open=True,
        hi_open=True,
    ),
    Spec(
        "correlation_min_coverage",
        "float",
        0.7,
        CORR,
        "Minimum coverage",
        "Share of known hours a series needs in the window (also used by anomalies).",
        lo=0,
        hi=1,
        lo_open=True,
    ),
    Spec(
        "correlation_max_lag_hours",
        "int",
        24,
        CORR,
        "Maximum lag (hours)",
        lo=0,
        hi=168,
    ),
    Spec("correlation_top", "int", 100, CORR, "Findings kept per window", lo=1, hi=1000),
    Spec(
        "correlation_method",
        "choice",
        "v2",
        CORR,
        "Method",
        "v2 = calibrated significance and structural flags; v1 = the original method.",
        choices=("v2", "v1"),
    ),
    Spec(
        "correlation_structure_display",
        "choice",
        "hide",
        CORR,
        "Indirect and common-factor relations",
        "hide = like the other flags; lower = shown after the findings without flags; "
        "show = among them.",
        choices=("hide", "lower", "show"),
    ),
    Spec("anomalies_enabled", "bool", True, ANOM, "Anomalies and change points"),
    Spec(
        "anomaly_max_q",
        "float",
        0.05,
        ANOM,
        "Target false discovery rate (q)",
        "Benjamini-Hochberg over the episodes of all series (v2).",
        lo=0,
        hi=1,
        lo_open=True,
    ),
    Spec(
        "anomaly_z_cap",
        "float",
        50.0,
        ANOM,
        "z cap of the episode statistic",
        "Nearly constant sensors cannot dominate the reference above this |z| (v2).",
        lo=3,
        hi=1000,
        lo_open=True,
    ),
    Spec(
        "anomaly_baseline_days",
        "int",
        56,
        ANOM,
        "Baseline (days)",
        "Days before the recent week the expectation is learnt from.",
        lo=14,
        hi=365,
    ),
    Spec("anomaly_recent_days", "int", 7, ANOM, "Recent period (days)", lo=1, hi=30),
    Spec("anomaly_top", "int", 100, ANOM, "Findings kept", lo=1, hi=1000),
    Spec(
        "anomaly_z",
        "float",
        4.0,
        ANOM,
        "Fixed |z| threshold (v1 only)",
        lo=2,
        hi=20,
    ),
    Spec(
        "anomaly_method",
        "choice",
        "v2",
        ANOM,
        "Method",
        "v2 = conformal p-values and FDR over series; v1 = the original method "
        "(fixed |z| threshold).",
        choices=("v2", "v1"),
    ),
    Spec(
        "changepoint_window_days",
        "int",
        90,
        CHANGE,
        "Window (days)",
        lo=28,
        hi=730,
    ),
    Spec(
        "changepoint_recent_days",
        "int",
        30,
        CHANGE,
        "Recent period (days)",
        "Changes are looked for in these last days.",
        lo=7,
        hi=365,
    ),
    Spec(
        "changepoint_min_shift",
        "float",
        3.0,
        CHANGE,
        "Minimum shift (v1, deviations)",
        lo=1,
        hi=20,
    ),
)
BY_KEY: dict[str, Spec] = {s.key: s for s in SPECS}

# Retention periods (ADR 034, ADR 094): stored by the admin GUI's page Retention in the
# same history as the analysis settings, applied by the next retention run. Their
# default comes from the edition (hacore.edition) or the installation's configuration
# (environment, the add-on's options); ``default`` here is the full edition's.
RETENTION = "Retention"
RETENTION_SPECS: tuple[Spec, ...] = (
    Spec("states_retention_days", "int", 365, RETENTION, "States", lo=1, hi=3650),
    Spec(
        "statistics_5min_retention_days",
        "int",
        365,
        RETENTION,
        "5-minute statistics",
        lo=1,
        hi=3650,
    ),
    Spec(
        "statistics_hour_retention_days",
        "int",
        5 * 365,
        RETENTION,
        "Hourly statistics",
        lo=1,
        hi=3650,
    ),
)
RETENTION_BY_KEY: dict[str, Spec] = {s.key: s for s in RETENTION_SPECS}
# Every key the admin GUI may store.
STORED_BY_KEY: dict[str, Spec] = BY_KEY | RETENTION_BY_KEY


def parse_stored(rows: list[tuple[str, Any]]) -> tuple[dict[str, Value], list[str]]:
    """Stored overrides (key, decoded JSON value) that are known and valid, and the
    problems with the others (an older or newer version may have stored them)."""
    out: dict[str, Value] = {}
    problems: list[str] = []
    for key, raw in rows:
        spec = STORED_BY_KEY.get(key)
        if spec is None:
            problems.append(f"{key}: unknown setting, ignored")
            continue
        try:
            out[key] = spec.parse(raw)
        except ValueError as err:
            problems.append(f"{err}, ignored")
    return out, problems
