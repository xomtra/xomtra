"""Anomalies and change points of single entities (phase 1b.2, ADR 046).

Both work on the hourly grid of the correlations (``hacore.correlations``: hourly
``mean`` of a measurement, ``delta`` of a counter, ``share`` of the hour in a state),
with the same exclusions: entities labelled ``hammond_no_analysis``, removed entities,
nearly constant series and uptime clocks.

**Anomalies** (``finding_type = 'anomaly'``): the usual value of every hour of the week
(local time) is the median of the same hour of the week over the baseline
(``baseline_days`` before the last ``recent_days``); an hour of the week with fewer than
``MIN_BUCKET_VALUES`` values falls back to the same hour of the day. The deviation of a
recent hour is ``(value - median) / scale``, where ``scale`` is the robust standard
deviation (1.4826 x MAD) of that hour, but at least half of the series' overall robust
deviation (a bucket that happened to be constant would make any change infinite).
Hours with ``|z| >= z_threshold`` form episodes (same sign, gaps of one hour bridged);
each episode is one finding with its peak ``z`` as ``score``, mean observed and
expected value, and its hours as ``period_start``/``period_end``.

**Change points** (``finding_type = 'change_point'``): a lasting shift of the level of
the daily means (local days) over ``changepoint_days``. For every split in the last
``changepoint_recent_days`` with at least ``MIN_AFTER_DAYS`` after and
``MIN_BEFORE_DAYS`` before, the shift of the medians is scaled by the pooled robust
deviation of the days; the split with the strongest shift (weighted by the number of
days on both sides) wins and is reported when the shift reaches ``changepoint_min_shift``
and at least ``MIN_CONSISTENT`` of the days after lie beyond the median before.

Flags (findings are kept, but listed after the unflagged ones and hidden by default in
``get_findings``): ``short`` (an anomaly of a single hour), ``weather`` (the outside
temperature, if configured, was itself unusual in the same hours or shifted at the same
day by at least ``WEATHER_Z``).
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import numpy.typing as npt

from hacore import correlations as corr
from hacore import significance as sig
from hacore.exclusions import DEFAULT_LABELS, ExclusionLabels, load_no_analysis
from hacore.lake import LAKE_ALIAS

logger = logging.getLogger(__name__)

type FloatArray = npt.NDArray[np.float64]

ANOMALY = "anomaly"
CHANGE_POINT = "change_point"

# 1.4826 x MAD estimates the standard deviation of normally distributed values.
MAD_SD = 1.4826
# Values an hour-of-week bucket needs; otherwise the hour of the day is used.
MIN_BUCKET_VALUES = 3
MIN_HOUR_VALUES = 5
# Known hours of baseline a series needs for anomalies.
MIN_BASELINE_HOURS = 14 * 24
# Weight of the overall deviation when shrinking the deviation of a bucket.
PRIOR_VALUES = 8
# The bucket scale is at least this share of the series' overall robust deviation.
SCALE_FLOOR = 0.5
# ... and for shares of time at least this (3 minutes of an hour).
SHARE_SCALE_FLOOR = 0.05
# v2: |z| above this counts as this in the episode statistic. A nearly constant series
# has a scale near 0 and z in the millions; a few such series would own the tail of
# the pooled reference and nothing else could pass (homelab, 0.4.0).
Z_CAP = 50.0
# Anomalous hours separated by at most this many normal hours form one episode.
EPISODE_GAP = 1
# Episodes kept per series (the strongest ones).
MAX_EPISODES_PER_SERIES = 3
# Change points: days on both sides of a split and consistency of the days after.
MIN_BEFORE_DAYS = 14
MIN_AFTER_DAYS = 7
MIN_CONSISTENT = 0.8
# Days of a series' daily means needed for change points.
MIN_DAILY_DAYS = MIN_BEFORE_DAYS + MIN_AFTER_DAYS
# A local day needs this many known hours to have a daily mean.
MIN_DAY_HOURS = 18
# v2: series with at most this many distinct baseline values are discrete (counts,
# setpoints): their overall scale is Qn (Rousseeuw & Croux 1993), not MAD, which is 0
# whenever more than half of the values are equal.
DISCRETE_VALUES = 10
QN_SAMPLE = 400
# v2 change points: clip of the lag-1 autocorrelation of the days (the surrogates of a
# nearly integrated series would wander anywhere).
MAX_DAY_AR = 0.9
# Mean |z| over an anomaly's hours (or the shift at a change point) of the outside
# temperature that flags a finding as possibly caused by the weather.
WEATHER_Z = 3.0


@dataclass(frozen=True, slots=True)
class AnomalyParams:
    baseline_days: int = 56
    recent_days: int = 7
    z_threshold: float = 4.0
    changepoint_days: int = 90
    changepoint_recent_days: int = 30
    changepoint_min_shift: float = 3.0
    min_coverage: float = 0.7
    top: int = 100
    outside_temp: str | None = None
    timezone: str = "UTC"
    labels: ExclusionLabels = DEFAULT_LABELS
    # "v2": conformal / permutation p-values and BH over series (ADR 062 points 4-5);
    # "v1": fixed thresholds (ADR 046).
    method: str = "v2"
    max_q: float = 0.05
    min_effect_z: float = 3.0  # v2 anomalies: peak |z| at least this
    z_cap: float = Z_CAP  # v2 anomalies: |z| counted at most this in the statistic
    min_shift_lr: float = 2.0  # v2 change points: shift in long-run deviations
    permutations: int = 200
    seed: int = 0

    @property
    def window_days(self) -> int:
        return max(self.baseline_days + self.recent_days, self.changepoint_days)


@dataclass(slots=True)
class AnomalyResult:
    series_used: int = 0
    series_excluded: int = 0
    anomalies: int = 0
    change_points: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    notes: list[str] = field(default_factory=list)
    # v2: run statistics per type (reference size, p/q histograms), no entity names.
    significance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "series_used": self.series_used,
            "series_excluded": self.series_excluded,
            "anomalies": self.anomalies,
            "change_points": self.change_points,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "notes": self.notes,
            "significance": self.significance,
        }


def _id(kind: str, s: corr.Series, extra: str = "") -> str:
    raw = f"{kind}|{s.key}|{s.metric}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _nanmedian(x: FloatArray, axis: int = 0) -> FloatArray:
    """``np.nanmedian`` without the warning for all-missing columns."""
    if x.shape[axis] == 0:
        shape = list(x.shape)
        del shape[axis]
        return np.full(shape, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out: FloatArray = np.nanmedian(x, axis=axis)
    return out


# ---- anomalies ---------------------------------------------------------------------


@dataclass(slots=True)
class Expectation:
    expected: FloatArray  # hours x series, NaN = no expectation
    scale: FloatArray  # hours x series


def expectation(
    values: FloatArray,
    how: npt.NDArray[np.int64],
    hod: npt.NDArray[np.int64],
    baseline: npt.NDArray[np.bool_],
    series: list[corr.Series],
) -> Expectation:
    """Usual value and robust scale of every hour, from the baseline hours."""
    t_len, n = values.shape
    expected = np.full((t_len, n), np.nan)
    bucket_sd = np.full((t_len, n), np.nan)
    bucket_n = np.zeros((t_len, n))
    base = values[baseline]
    base_how = how[baseline]
    base_hod = hod[baseline]
    for h in range(24):
        sub = base[base_hod == h]
        med = _nanmedian(sub)
        sd = MAD_SD * _nanmedian(np.abs(sub - med))
        cnt = (~np.isnan(sub)).sum(axis=0)
        ok = cnt >= MIN_HOUR_VALUES
        rows = hod == h
        expected[np.ix_(rows, ok)] = med[ok]
        bucket_sd[np.ix_(rows, ok)] = sd[ok]
        bucket_n[np.ix_(rows, ok)] = cnt[ok]
    for b in range(168):
        sub = base[base_how == b]
        med = _nanmedian(sub)
        sd = MAD_SD * _nanmedian(np.abs(sub - med))
        cnt = (~np.isnan(sub)).sum(axis=0)
        ok = cnt >= MIN_BUCKET_VALUES
        rows = how == b
        expected[np.ix_(rows, ok)] = med[ok]
        bucket_sd[np.ix_(rows, ok)] = sd[ok]
        bucket_n[np.ix_(rows, ok)] = cnt[ok]
    # Overall robust deviation of the baseline around the usual values.
    resid = values[baseline] - expected[baseline]
    overall = MAD_SD * _nanmedian(np.abs(resid))
    with np.errstate(all="ignore"):
        fallback = np.where(overall > 0, overall, np.nanstd(resid, axis=0))
    overall = np.nan_to_num(fallback)
    # A deviation from a handful of values is noisy: it is shrunk towards the overall
    # one as if the overall one had PRIOR_VALUES values of its own.
    with np.errstate(all="ignore"):
        shrunk = np.sqrt(
            (bucket_n * np.nan_to_num(bucket_sd) ** 2 + PRIOR_VALUES * overall[None, :] ** 2)
            / (bucket_n + PRIOR_VALUES)
        )
    floor = SCALE_FLOOR * overall
    share = np.array([s.metric == "share" for s in series], dtype=bool)
    floor[share] = np.maximum(floor[share], SHARE_SCALE_FLOOR)
    scale = np.where(np.isnan(expected), np.nan, np.fmax(shrunk, floor[None, :]))
    scale[scale <= 0] = np.nan
    return Expectation(expected=expected, scale=scale)


def episodes(z: FloatArray, threshold: float) -> list[tuple[int, int, float, int]]:
    """Runs of ``|z| >= threshold`` of one sign: (first, last, peak z, anomalous hours);
    runs separated by at most ``EPISODE_GAP`` hours are joined."""
    hits = np.flatnonzero(np.abs(np.nan_to_num(z)) >= threshold)
    out: list[tuple[int, int, float, int]] = []
    start = prev = -10
    count = 0
    peak = 0.0
    for i in hits:
        sign = np.sign(z[i])
        if count and i - prev <= EPISODE_GAP + 1 and sign == np.sign(peak):
            prev = int(i)
            count += 1
            if abs(z[i]) > abs(peak):
                peak = float(z[i])
            continue
        if count:
            out.append((start, prev, peak, count))
        start = prev = int(i)
        count = 1
        peak = float(z[i])
    if count:
        out.append((start, prev, peak, count))
    return out


def detect_anomalies(
    grid: corr.Grid, params: AnomalyParams, shifts: dict[int, int] | None = None
) -> list[dict[str, Any]]:
    """Anomaly episodes of the last ``recent_days``; ``shifts`` maps a series to the
    first hour of a change point found for it (episodes after it are flagged)."""
    shifts = shifts or {}
    t_len = len(grid.hours)
    recent_hours = params.recent_days * 24
    if t_len <= recent_hours:
        return []
    how = (grid.local_dow - 1) * 24 + grid.local_hour
    idx = np.arange(t_len)
    recent = idx >= t_len - recent_hours
    baseline = ~recent & (idx >= t_len - recent_hours - params.baseline_days * 24)
    known_base = (~np.isnan(grid.values[baseline])).sum(axis=0)
    exp = expectation(grid.values, how, grid.local_hour, baseline, grid.series)
    with np.errstate(all="ignore"):
        z = (grid.values - exp.expected) / exp.scale
    temp_z = _driver_z(grid, z, params.outside_temp)
    hours = [corr._to_dt(h) for h in grid.hours]
    out: list[dict[str, Any]] = []
    first_recent = t_len - recent_hours
    for j, s in enumerate(grid.series):
        if s.entity_id == params.outside_temp or known_base[j] < MIN_BASELINE_HOURS:
            continue
        zj = z[first_recent:, j]
        found = sorted(episodes(zj, params.z_threshold), key=lambda e: -abs(e[2]))[
            :MAX_EPISODES_PER_SERIES
        ]
        for first, last, peak, count in found:
            a, b = first_recent + first, first_recent + last
            sel = slice(a, b + 1)
            hit = np.abs(np.nan_to_num(z[sel, j])) >= params.z_threshold
            flags = []
            if count == 1:
                flags.append("short")
            if temp_z is not None and abs(_mean(temp_z[sel])) >= WEATHER_Z:
                flags.append("weather")
            shift_at = shifts.get(j)
            if shift_at is not None and a >= shift_at:
                flags.append("level_shift")
            out.append(
                {
                    "finding_id": _id(ANOMALY, s, hours[a].isoformat()),
                    "entity_id": s.entity_id,
                    "metric": s.metric,
                    "state": s.state,
                    "score": round(peak, 2),
                    "n_eff": count,
                    "flags": flags,
                    "observed": _round(float(np.nanmean(grid.values[sel, j][hit]))),
                    "expected": _round(float(np.nanmean(exp.expected[sel, j][hit]))),
                    "period_start": hours[a],
                    "period_end": hours[b] + timedelta(hours=1),
                }
            )
    return _rank(out, params.top, key=lambda f: -abs(float(f["score"])) * np.sqrt(f["n_eff"]))


def _driver_z(grid: corr.Grid, z: FloatArray, entity_id: str | None) -> FloatArray | None:
    if not entity_id:
        return None
    j = next(
        (i for i, s in enumerate(grid.series) if s.entity_id == entity_id and s.metric == "mean"),
        None,
    )
    return None if j is None else z[:, j]


# ---- change points -----------------------------------------------------------------


def daily_means(grid: corr.Grid, timezone: str) -> tuple[list[datetime], FloatArray]:
    """Means per local day (``MIN_DAY_HOURS`` known hours needed); the day starts are
    returned in UTC."""
    hours = [corr._to_dt(h) for h in grid.hours]
    tz = ZoneInfo(timezone)
    day_of = np.array([h.astimezone(tz).date().toordinal() for h in hours], dtype=np.int64)
    days = np.unique(day_of)
    out = np.full((len(days), grid.values.shape[1]), np.nan)
    starts: list[datetime] = []
    for k, d in enumerate(days):
        rows = day_of == d
        first = hours[int(np.argmax(rows))]
        local = first.astimezone(tz)
        starts.append(local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC))
        sub = grid.values[rows]
        n_known = (~np.isnan(sub)).sum(axis=0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(sub, axis=0)
        out[k] = np.where(n_known >= MIN_DAY_HOURS, mean, np.nan)
    return starts, out


@dataclass(frozen=True, slots=True)
class Shift:
    split: int  # index of the first day after the change
    before: float
    after: float
    shift: float  # (after - before) / pooled robust deviation
    consistent: float


def best_shift(x: FloatArray, recent_days: int, *, floor: float = 0.0) -> Shift | None:
    """Strongest level shift of a daily series with the split in the last
    ``recent_days`` days (see the module docstring)."""
    known = np.flatnonzero(~np.isnan(x))
    if len(known) < MIN_DAILY_DAYS:
        return None
    v = x[known]
    n = len(v)
    first_split = max(MIN_BEFORE_DAYS, len(x) - recent_days)
    best: tuple[float, Shift] | None = None
    for split_day in range(first_split, len(x) - MIN_AFTER_DAYS + 1):
        k = int(np.searchsorted(known, split_day))
        if k < MIN_BEFORE_DAYS or n - k < MIN_AFTER_DAYS:
            continue
        before, after = v[:k], v[k:]
        mb, ma = float(np.median(before)), float(np.median(after))
        pooled = np.concatenate([before - mb, after - ma])
        sd = MAD_SD * float(np.median(np.abs(pooled)))
        if sd <= 0:
            sd = float(np.std(pooled))
        sd = max(sd, floor, 1e-9 * max(abs(mb), abs(ma), 1.0))
        shift = (ma - mb) / sd
        # The split is placed by the difference of the means (medians hardly move when
        # a few days of the other level are on the wrong side), weighted like a t-test.
        weight = abs(float(after.mean() - before.mean())) * np.sqrt(len(before) * len(after) / n)
        consistent = float(np.mean(after > mb) if shift > 0 else np.mean(after < mb))
        if best is None or weight > best[0]:
            best = (weight, Shift(split_day, mb, ma, shift, consistent))
    return best[1] if best else None


def detect_change_points(grid: corr.Grid, params: AnomalyParams) -> list[dict[str, Any]]:
    starts, daily = daily_means(grid, params.timezone)
    if len(starts) < MIN_DAILY_DAYS:
        return []
    keep_from = max(0, len(starts) - params.changepoint_days)
    starts, daily = starts[keep_from:], daily[keep_from:]
    temp_j = next(
        (
            i
            for i, s in enumerate(grid.series)
            if s.entity_id == params.outside_temp and s.metric == "mean"
        ),
        None,
    )
    out: list[dict[str, Any]] = []
    for j, s in enumerate(grid.series):
        if j == temp_j:
            continue
        floor = SHARE_SCALE_FLOOR if s.metric == "share" else 0.0
        found = best_shift(daily[:, j], params.changepoint_recent_days, floor=floor)
        if (
            found is None
            or abs(found.shift) < params.changepoint_min_shift
            or found.consistent < MIN_CONSISTENT
        ):
            continue
        flags = []
        if temp_j is not None:
            temp_shift = _shift_at(daily[:, temp_j], found.split)
            if temp_shift is not None and abs(temp_shift) >= WEATHER_Z:
                flags.append("weather")
        out.append(
            {
                "finding_id": _id(CHANGE_POINT, s),
                "entity_id": s.entity_id,
                "metric": s.metric,
                "state": s.state,
                "score": round(found.shift, 2),
                "n_eff": len(starts) - found.split,
                "flags": flags,
                "observed": _round(found.after),
                "expected": _round(found.before),
                "period_start": starts[found.split],
                "period_end": corr._to_dt(grid.hours[-1]) + timedelta(hours=1),
            }
        )
    return _rank(out, params.top, key=lambda f: -abs(float(f["score"])))


def _shift_at(x: FloatArray, split: int) -> float | None:
    before, after = x[:split], x[split:]
    before, after = before[~np.isnan(before)], after[~np.isnan(after)]
    if len(before) < MIN_AFTER_DAYS or len(after) < MIN_AFTER_DAYS:
        return None
    mb, ma = float(np.median(before)), float(np.median(after))
    pooled = np.concatenate([before - mb, after - ma])
    sd = MAD_SD * float(np.median(np.abs(pooled))) or float(np.std(pooled))
    return None if sd <= 0 else (ma - mb) / sd


# ---- v2: calibrated p-values and FDR (ADR 062 points 4-5) --------------------------


def qn_scale(x: FloatArray, rng: np.random.Generator | None = None) -> float:
    """Qn scale estimator: first quartile of the pairwise distances (consistency factor
    2.2219 for normal data); on a random sample of ``QN_SAMPLE`` values."""
    x = x[~np.isnan(x)]
    if len(x) > QN_SAMPLE:
        x = (rng or np.random.default_rng(0)).choice(x, QN_SAMPLE, replace=False)
    n = len(x)
    if n < 2:
        return 0.0
    d = np.abs(x[:, None] - x[None, :])[np.triu_indices(n, 1)]
    h = n // 2 + 1
    k = h * (h - 1) // 2
    return float(2.2219 * np.partition(d, k - 1)[k - 1])


def _discrete_floor(
    values: FloatArray, baseline: npt.NDArray[np.bool_], exp: Expectation
) -> FloatArray:
    """Qn of the baseline residuals for discrete series (0 for the others)."""
    out = np.zeros(values.shape[1])
    base = values[baseline]
    resid = base - exp.expected[baseline]
    for j in range(values.shape[1]):
        col = base[:, j]
        if len(np.unique(col[~np.isnan(col)])) <= DISCRETE_VALUES:
            out[j] = qn_scale(resid[:, j])
    return out


def _z(values: FloatArray, exp: Expectation, floor: FloatArray) -> FloatArray:
    scale = np.fmax(exp.scale, np.where(floor > 0, floor, np.nan)[None, :])
    with np.errstate(all="ignore"):
        out: FloatArray = (values - exp.expected) / scale
    return out


def _episode_stat(peak: float, count: int, cap: float = Z_CAP) -> float:
    """Strength of an episode: peak |z| (at most ``cap``) times the square root of its
    hours."""
    return min(abs(peak), cap) * float(np.sqrt(count))


def _resolution(values: FloatArray, baseline: npt.NDArray[np.bool_]) -> FloatArray:
    """Median gap between neighbouring distinct baseline values of every series: a
    deviation smaller than the step the sensor reports in is not measurable, so the
    scale never goes below it."""
    out = np.zeros(values.shape[1])
    base = values[baseline]
    for j in range(values.shape[1]):
        col = base[:, j]
        gaps = np.diff(np.unique(col[~np.isnan(col)]))
        gaps = gaps[gaps > 0]
        if len(gaps):
            out[j] = float(np.median(gaps))
    return out


def _strongest_episodes(
    z: FloatArray, threshold: float, limit: int, *, cap: float = Z_CAP
) -> list[tuple[int, int, float, int]]:
    found = episodes(z, threshold)
    return sorted(found, key=lambda e: -_episode_stat(e[2], e[3], cap))[:limit]


def _reference(
    values: FloatArray,
    how: npt.NDArray[np.int64],
    grid: corr.Grid,
    *,
    baseline: npt.NDArray[np.bool_],
    first_recent: int,
    floor: FloatArray,
    usable: list[int],
    params: AnomalyParams,
    shifts: dict[int, int],
) -> list[float]:
    """Strongest episode of each usable series on each baseline day, scored against
    the other baseline weeks (0 if none). Days of a series after its detected change
    point are left out: against the old level they would all be episodes."""
    idx = np.arange(len(values))
    week = np.where(baseline, (first_recent - 1 - idx) // 168, -1)
    reference: list[float] = []
    for k in range(int(week.max()) + 1 if baseline.any() else 0):
        held = week == k
        if held.sum() < 24:
            continue
        exp_k = expectation(values, how, grid.local_hour, baseline & ~held, grid.series)
        z_k = _z(values[held], Expectation(exp_k.expected[held], exp_k.scale[held]), floor)
        # One reference value per series and day: enough of them even in a small
        # installation (p >= 1 / (1 + series x baseline days)).
        held_idx = idx[held]
        for day in range(0, len(z_k) - 23, 24):
            for j in usable:
                if j in shifts and held_idx[day] + 23 >= shifts[j]:
                    continue
                found = _strongest_episodes(
                    z_k[day : day + 24, j], params.min_effect_z, 1, cap=params.z_cap
                )
                reference.append(
                    _episode_stat(found[0][2], found[0][3], params.z_cap) if found else 0.0
                )
    return reference


def _anomaly_flags(
    count: int, sel: slice, temp_z: FloatArray | None, shift_at: int | None, start: int
) -> list[str]:
    flags = []
    if count == 1:
        flags.append("short")
    if temp_z is not None and abs(_mean(temp_z[sel])) >= WEATHER_Z:
        flags.append("weather")
    if shift_at is not None and start >= shift_at:
        flags.append("level_shift")
    return flags


def detect_anomalies_v2(
    grid: corr.Grid, params: AnomalyParams, shifts: dict[int, int] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Anomaly episodes with conformal p-values (ADR 062 point 4).

    Reference: every baseline week is scored against the expectation from the other
    baseline weeks (leave one week out); the strongest episode of each series and day
    (0 if none) forms the reference sample, pooled over series (z is on one scale). The
    p-value of a recent episode is its exceedance probability in that sample, with a
    generalised Pareto tail beyond it; Benjamini-Hochberg over all candidate episodes;
    an episode is reported with ``q <= max_q`` and a peak ``|z| >= min_effect_z``.
    The scale is at least the series' resolution and the statistic caps |z| at
    ``Z_CAP``: nearly constant series must not own the tail of the pooled reference.
    """
    shifts = shifts or {}
    t_len = len(grid.hours)
    recent_hours = params.recent_days * 24
    stats: dict[str, Any] = {"reference_size": 0, "candidates": 0}
    if t_len <= recent_hours:
        return [], stats
    values = grid.values
    how = (grid.local_dow - 1) * 24 + grid.local_hour
    idx = np.arange(t_len)
    first_recent = t_len - recent_hours
    recent = idx >= first_recent
    baseline = ~recent & (idx >= first_recent - params.baseline_days * 24)
    known_base = (~np.isnan(values[baseline])).sum(axis=0)
    usable = [
        j
        for j, s in enumerate(grid.series)
        if s.entity_id != params.outside_temp and known_base[j] >= MIN_BASELINE_HOURS
    ]
    exp = expectation(values, how, grid.local_hour, baseline, grid.series)
    floor = np.fmax(_discrete_floor(values, baseline, exp), _resolution(values, baseline))
    z = _z(values, exp, floor)
    reference = _reference(
        values,
        how,
        grid,
        baseline=baseline,
        first_recent=first_recent,
        floor=floor,
        usable=usable,
        params=params,
        shifts=shifts,
    )
    null = sig.reference_null(np.array(reference))
    stats["reference_size"] = null.size
    temp_z = _driver_z(grid, z, params.outside_temp)
    hours = [corr._to_dt(h) for h in grid.hours]
    cands: list[dict[str, Any]] = []
    cand_stat: list[float] = []
    for j in usable:
        s = grid.series[j]
        for first, last, peak, count in _strongest_episodes(
            z[first_recent:, j], params.min_effect_z, MAX_EPISODES_PER_SERIES, cap=params.z_cap
        ):
            a, b = first_recent + first, first_recent + last
            sel = slice(a, b + 1)
            hit = np.abs(np.nan_to_num(z[sel, j])) >= params.min_effect_z
            flags = _anomaly_flags(count, sel, temp_z, shifts.get(j), a)
            cand_stat.append(_episode_stat(peak, count, params.z_cap))
            cands.append(
                {
                    "finding_id": _id(ANOMALY, s, hours[a].isoformat()),
                    "entity_id": s.entity_id,
                    "metric": s.metric,
                    "state": s.state,
                    "score": round(peak, 2),
                    "n_eff": count,
                    "flags": flags,
                    "observed": _round(float(np.nanmean(values[sel, j][hit]))),
                    "expected": _round(float(np.nanmean(exp.expected[sel, j][hit]))),
                    "period_start": hours[a],
                    "period_end": hours[b] + timedelta(hours=1),
                }
            )
    stats["candidates"] = len(cands)
    if not cands:
        stats |= _histograms(np.array([]), np.array([]))
        return [], stats
    if null.size == 0:
        stats["note"] = "no reference weeks"
        return [], stats
    p = null.pvalues(np.array(cand_stat))
    q = corr.benjamini_hochberg(p)
    stats |= _histograms(p, q)
    out = []
    for f, pv, qv in zip(cands, p, q, strict=True):
        if qv <= params.max_q:
            f["p_value"] = float(pv)
            f["q_value"] = float(qv)
            out.append(f)
    ranked = _rank(out, params.top, key=lambda f: (f["q_value"], -abs(float(f["score"]))))
    return ranked, stats


def _histograms(p: FloatArray, q: FloatArray) -> dict[str, Any]:
    """Distribution of p and q of all candidates (released or not), no entity names
    (ADR 062 point 10)."""
    return {
        "p_histogram": np.histogram(p, bins=10, range=(0, 1))[0].tolist(),
        "q_histogram": np.histogram(q, bins=10, range=(0, 1))[0].tolist(),
    }


def _day_ar1(x: FloatArray) -> float:
    """Lag-1 autocorrelation of a daily series, clipped to [0, MAX_DAY_AR]."""
    x = x - x.mean()
    den = float(x @ x)
    if len(x) < 3 or den <= 0:
        return 0.0
    return float(np.clip((x[:-1] @ x[1:]) / den, 0.0, MAX_DAY_AR))


def _max_mean_shift(v: FloatArray, splits: npt.NDArray[np.int64]) -> FloatArray:
    """max over ``splits`` of ``|mean after - mean before| * sqrt(nb na / n)`` for each
    row of ``v`` (permutations x days)."""
    n = v.shape[-1]
    cs = np.cumsum(v, axis=-1)
    total = cs[..., -1:]
    nb = splits.astype(np.float64)
    na = n - nb
    before = cs[..., splits - 1] / nb
    after = (total - cs[..., splits - 1]) / na
    stat: FloatArray = np.abs(after - before) * np.sqrt(nb * na / n)
    return stat.max(axis=-1)


def _ar1_surrogates(v: FloatArray, phi: float, count: int, rng: np.random.Generator) -> FloatArray:
    """``count`` surrogates of a daily series without a change (count x n): its AR(1)
    innovations, permuted, filtered back with the same ``phi`` (Kirch 2007). The
    surrogates keep the series' scale and autocorrelation; a level shift, which is not
    exchangeable, is spread over all days."""
    x = v - v.mean()
    e = np.empty_like(x)
    e[0] = x[0] * np.sqrt(1 - phi * phi)
    e[1:] = x[1:] - phi * x[:-1]
    e -= e.mean()
    perm = np.argsort(rng.random((count, len(e))), axis=1)
    shuffled = e[perm]
    out = np.empty_like(shuffled)
    out[:, 0] = shuffled[:, 0] / np.sqrt(1 - phi * phi)
    for d in range(1, out.shape[1]):
        out[:, d] = phi * out[:, d - 1] + shuffled[:, d]
    return out


def _change_candidate(
    x: FloatArray,
    known: npt.NDArray[np.int64],
    s: corr.Series,
    *,
    params: AnomalyParams,
    starts: list[datetime],
    grid: corr.Grid,
    temp: FloatArray | None,
) -> dict[str, Any]:
    """Finding of a series with its strongest shift ({} without one); ``_phi`` = lag-1
    autocorrelation of the days around their levels, ``_effect`` = effect reached."""
    n = len(known)
    floor = SHARE_SCALE_FLOOR if s.metric == "share" else 0.0
    found = best_shift(x, params.changepoint_recent_days, floor=floor)
    if found is None:
        return {}
    v = x[known]
    level = np.where(
        np.arange(n) < int(np.searchsorted(known, found.split)), found.before, found.after
    )
    phi = _day_ar1(v - level)
    shift_lr = found.shift / float(np.sqrt((1 + phi) / (1 - phi)))
    flags = []
    if temp is not None:
        temp_shift = _shift_at(temp, found.split)
        if temp_shift is not None and abs(temp_shift) >= WEATHER_Z:
            flags.append("weather")
    effect = abs(shift_lr) >= params.min_shift_lr and found.consistent >= MIN_CONSISTENT
    return {
        "finding_id": _id(CHANGE_POINT, s),
        "entity_id": s.entity_id,
        "metric": s.metric,
        "state": s.state,
        "score": round(shift_lr, 2),
        "n_eff": len(starts) - found.split,
        "flags": flags,
        "observed": _round(found.after),
        "expected": _round(found.before),
        "period_start": starts[found.split],
        "period_end": corr._to_dt(grid.hours[-1]) + timedelta(hours=1),
        "_phi": phi,
        "_resid": v - level,
        "_effect": effect,
    }


def _long_run_sd(resid: FloatArray, phi: float) -> float:
    """Long-run deviation of an AR(1) series: innovation deviation / (1 - phi)."""
    e = resid[1:] - phi * resid[:-1]
    return float(np.std(e)) / (1 - phi) if len(e) > 1 else 0.0


def detect_change_points_v2(
    grid: corr.Grid, params: AnomalyParams
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Level shifts with permutation p-values (ADR 062 point 5).

    Per series: the statistic is the largest difference of the mean daily level before
    and after a split in the last ``changepoint_recent_days`` (CUSUM-like, scaled by
    ``sqrt(nb na / n)``), in long-run deviations of the series. Its null comes from
    ``permutations`` surrogates of the series without a change: the AR(1) innovations
    of the days around their fitted levels, permuted and filtered back (Kirch 2007;
    permutations of whole weeks, as first planned, have too few distinct orders in 90
    days). The standardised null is pooled over series, with a generalised Pareto tail,
    so that a single series can reach the small p-values Benjamini-Hochberg over many
    series needs. A change is reported with ``q <= max_q``, a shift of at least
    ``min_shift_lr`` long-run deviations and at least ``MIN_CONSISTENT`` of the days
    after beyond the median before.
    """
    stats: dict[str, Any] = {"tested": 0}
    starts, daily = daily_means(grid, params.timezone)
    if len(starts) < MIN_DAILY_DAYS:
        return [], stats
    keep_from = max(0, len(starts) - params.changepoint_days)
    starts, daily = starts[keep_from:], daily[keep_from:]
    temp_j = next(
        (
            i
            for i, s in enumerate(grid.series)
            if s.entity_id == params.outside_temp and s.metric == "mean"
        ),
        None,
    )
    rng = np.random.default_rng(params.seed)
    cands: list[dict[str, Any]] = []
    observed: list[float] = []
    null_parts: list[FloatArray] = []
    for j, s in enumerate(grid.series):
        if j == temp_j:
            continue
        x = daily[:, j]
        known = np.flatnonzero(~np.isnan(x))
        if len(known) < MIN_DAILY_DAYS:
            continue
        v = x[known]
        n = len(v)
        first_split = max(MIN_BEFORE_DAYS, len(x) - params.changepoint_recent_days)
        splits = np.unique(np.searchsorted(known, np.arange(first_split, len(x))))
        splits = splits[(splits >= MIN_BEFORE_DAYS) & (splits <= n - MIN_AFTER_DAYS)]
        if not len(splits) or float(np.ptp(v)) == 0:
            continue
        cand = _change_candidate(
            x,
            known,
            s,
            params=params,
            starts=starts,
            grid=grid,
            temp=None if temp_j is None else daily[:, temp_j],
        )
        if not cand:
            continue
        phi = float(cand.pop("_phi"))
        resid = cand.pop("_resid")
        sd = _long_run_sd(resid, phi)
        if sd <= 0:
            continue
        surrogates = _ar1_surrogates(resid, phi, params.permutations, rng)
        observed.append(float(_max_mean_shift(v[None, :], splits)[0]) / sd)
        null_parts.append(_max_mean_shift(surrogates, splits) / sd)
        cands.append(cand)
    stats["tested"] = len(cands)
    if not cands:
        stats |= _histograms(np.array([]), np.array([]))
        return [], stats
    null = sig.reference_null(np.concatenate(null_parts))
    stats["reference_size"] = null.size
    p_arr = null.pvalues(np.array(observed))
    q_arr = corr.benjamini_hochberg(p_arr)
    stats |= _histograms(p_arr, q_arr)
    out = []
    for f, pv, qv in zip(cands, p_arr, q_arr, strict=True):
        if f.pop("_effect") and qv <= params.max_q:
            f["p_value"] = float(pv)
            f["q_value"] = float(qv)
            out.append(f)
    return _rank(out, params.top, key=lambda f: -abs(float(f["score"]))), stats


# ---- common ------------------------------------------------------------------------


def _rank(findings: list[dict[str, Any]], top: int, key: Any) -> list[dict[str, Any]]:
    findings.sort(key=key)
    findings.sort(key=lambda f: bool(f["flags"]))  # stable: flagged ones last
    out = findings[:top]
    for rank, f in enumerate(out, start=1):
        f["rank"] = rank
    return out


def _mean(x: FloatArray) -> float:
    known = x[~np.isnan(x)]
    return float(known.mean()) if len(known) else 0.0


def _round(x: float) -> float | None:
    if not np.isfinite(x):
        return None
    return float(f"{x:.4g}")


def _shift_hours(grid: corr.Grid, change_points: list[dict[str, Any]]) -> dict[int, int]:
    """Series index -> grid hour of its change point."""
    index = {(s.entity_id, s.metric, s.state): j for j, s in enumerate(grid.series)}
    first = corr._to_dt(grid.hours[0])
    out = {}
    for f in change_points:
        j = index.get((f["entity_id"], f["metric"], f["state"]))
        if j is not None:
            out[j] = int((f["period_start"] - first).total_seconds() // 3600)
    return out


def _methods(params: AnomalyParams) -> tuple[str, str]:
    if params.method == "v1":
        return (
            f"hour-of-week median and robust deviation over {params.baseline_days} d before "
            f"the last {params.recent_days} d; |z| >= {params.z_threshold:g}; score = peak "
            "z, n_eff = anomalous hours",
            f"shift of the median of daily means in the last "
            f"{params.changepoint_recent_days} d, in robust deviations "
            f"(>= {params.changepoint_min_shift:g}); period_start = first day after the "
            "change, observed/expected = median after/before, n_eff = days after",
        )
    return (
        f"hour-of-week median and robust deviation (Qn for discrete series, at least the "
        f"sensor's resolution) over {params.baseline_days} d before the last "
        f"{params.recent_days} d; conformal p of the episode (min(peak |z|, {params.z_cap:g}) x "
        "sqrt(hours)) against leave-one-week-out baseline "
        f"episodes, BH over episodes q <= {params.max_q:g}, peak |z| >= "
        f"{params.min_effect_z:g}; score = peak z, n_eff = anomalous hours (v2, ADR 062)",
        f"shift of the daily level in the last {params.changepoint_recent_days} d; p from "
        f"{params.permutations} AR(1) surrogates per series (permuted innovations), pooled, "
        f"BH over series "
        f"q <= {params.max_q:g}, shift >= {params.min_shift_lr:g} long-run deviations; "
        "score = shift in long-run deviations, period_start = first day after the change, "
        "observed/expected = median after/before, n_eff = days after (v2, ADR 062)",
    )


def run(
    con: duckdb.DuckDBPyConnection,
    params: AnomalyParams,
    *,
    now: datetime | None = None,
    alias: str = LAKE_ALIAS,
) -> AnomalyResult:
    """Detect anomalies and change points and replace their findings."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    result = AnomalyResult()
    corr.ensure_schema(con, alias)
    grid_params = corr.CorrelationParams(
        window_days=params.window_days,
        min_coverage=params.min_coverage,
        outside_temp=params.outside_temp,
        timezone=params.timezone,
    )
    grid = corr.load_grid(con, grid_params, now=now, alias=alias)
    anomalies: list[dict[str, Any]] = []
    change_points: list[dict[str, Any]] = []
    if grid is None or len(grid.hours) < MIN_BASELINE_HOURS:
        result.notes.append("not enough data in the window")
    else:
        grid, result.series_excluded = corr._drop_excluded(
            grid, load_no_analysis(con, alias, params.labels), params.outside_temp
        )
        if params.outside_temp:
            corr.fill_from_archive(con, grid, params.outside_temp, alias=alias)
        grid = corr.clean(grid, params.min_coverage, params.outside_temp)
        result.series_used = sum(1 for s in grid.series if s.entity_id != params.outside_temp)
        result.period_start = corr._to_dt(grid.hours[0])
        result.period_end = corr._to_dt(grid.hours[-1]) + timedelta(hours=1)
        if params.method == "v1":
            change_points = detect_change_points(grid, params)
            anomalies = detect_anomalies(grid, params, _shift_hours(grid, change_points))
        else:
            change_points, cp_stats = detect_change_points_v2(grid, params)
            anomalies, an_stats = detect_anomalies_v2(
                grid, params, _shift_hours(grid, change_points)
            )
            result.significance = {ANOMALY: an_stats, CHANGE_POINT: cp_stats}
            logger.info("Anomalies significance: %s", result.significance)
    method, cp_method = _methods(params)
    corr.write_findings(
        con,
        anomalies,
        window_days=params.recent_days,
        period_start=result.period_start,
        period_end=result.period_end,
        method=method,
        now=now,
        finding_type=ANOMALY,
        alias=alias,
    )
    corr.write_findings(
        con,
        change_points,
        window_days=params.changepoint_days,
        period_start=result.period_start,
        period_end=result.period_end,
        method=cp_method,
        now=now,
        finding_type=CHANGE_POINT,
        alias=alias,
    )
    result.anomalies = len(anomalies)
    result.change_points = len(change_points)
    logger.info("Anomalies: %s", result.as_dict())
    return result
