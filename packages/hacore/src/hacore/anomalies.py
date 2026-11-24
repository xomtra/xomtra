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
from hacore.exclusions import load_no_analysis
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "series_used": self.series_used,
            "series_excluded": self.series_excluded,
            "anomalies": self.anomalies,
            "change_points": self.change_points,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "notes": self.notes,
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
            grid, load_no_analysis(con, alias), params.outside_temp
        )
        if params.outside_temp:
            corr.fill_from_archive(con, grid, params.outside_temp, alias=alias)
        grid = corr.clean(grid, params.min_coverage, params.outside_temp)
        result.series_used = sum(1 for s in grid.series if s.entity_id != params.outside_temp)
        result.period_start = corr._to_dt(grid.hours[0])
        result.period_end = corr._to_dt(grid.hours[-1]) + timedelta(hours=1)
        change_points = detect_change_points(grid, params)
        anomalies = detect_anomalies(grid, params, _shift_hours(grid, change_points))
    method = (
        f"hour-of-week median and robust deviation over {params.baseline_days} d before the "
        f"last {params.recent_days} d; |z| >= {params.z_threshold:g}; score = peak z, "
        "n_eff = anomalous hours"
    )
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
        method=(
            f"shift of the median of daily means in the last {params.changepoint_recent_days} "
            f"d, in robust deviations (>= {params.changepoint_min_shift:g}); "
            "period_start = first day after the change, observed/expected = median "
            "after/before, n_eff = days after"
        ),
        now=now,
        finding_type=CHANGE_POINT,
        alias=alias,
    )
    result.anomalies = len(anomalies)
    result.change_points = len(change_points)
    logger.info("Anomalies: %s", result.as_dict())
    return result
