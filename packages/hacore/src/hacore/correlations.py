"""Correlations between entities over an hourly grid (phase 1b.1, ADR 042).

The method follows ``ha_correlations.py`` (the prototype that read a copy of the HA
recorder) but reads the silver layer of the lake:

1. **Hourly grid** of series over a window (``window_days``), in UTC hours:

   * ``mean``  - hourly mean of a measurement (``silver.statistics``, period ``hour``);
   * ``delta`` - hourly increase of a counter (difference of the statistics ``sum``;
     a negative step is a reset and becomes missing);
   * ``share`` - share of the hour a binary or enum entity spent in one state
     (``silver.states``; time in ``unavailable``/``unknown`` does not count; an hour
     with less than half of known time is missing).

   Gaps of up to 3 hours are filled forward. Series covering less than
   ``min_coverage`` of the grid, or constant ones, are dropped. The grid starts at the
   first hour with data, so a young installation is not penalised for the part of the
   window before it existed.
2. **Residuals** after regressing out the daily cycle (hour of day in the local time
   zone), the day of the week and, if configured, the outside temperature (linear and
   quadratic). What remains is the part not explained by "it is evening" or "it is cold".
3. **Lagged correlation** of residuals of every pair for lags 0..``max_lag`` hours, on
   the hours both series have; for each pair the lag with the largest ``|r|`` wins.
   ``leader`` precedes ``follower`` by ``lag_hours``.
4. **Significance**: an effective number of observations corrected for lag-1
   autocorrelation, a two-sided p-value, Bonferroni over the tested lags and
   Benjamini-Hochberg FDR over all pairs (``q_value``).
5. **Findings**: pairs with ``|r| >= min_r`` and ``q <= max_q``, strongest first, at
   most ``top``; pairs of the same entity are skipped, pairs of the same device and
   near-duplicates (lag 0, ``|r| > 0.97``) are flagged, not hidden.

Findings replace the previous ones of the same type and window in ``catalog.finding``
in one transaction; a finding seen before keeps its ``first_detected_at``.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import numpy as np
import numpy.typing as npt

from hacore import significance as sig
from hacore import weather
from hacore.exclusions import DEFAULT_LABELS, ExclusionLabels, load_no_analysis
from hacore.lake import LAKE_ALIAS, sql_literal

logger = logging.getLogger(__name__)

type FloatArray = npt.NDArray[np.float64]

SCHEMA = "catalog"
FINDING = "finding"
FINDING_TYPE = "correlation"

FINDING_COLUMNS = """
    finding_id VARCHAR NOT NULL,
    finding_type VARCHAR NOT NULL,
    entity_id VARCHAR NOT NULL,
    metric VARCHAR,
    state VARCHAR,
    related_entity_id VARCHAR,
    related_metric VARCHAR,
    related_state VARCHAR,
    lag_hours INTEGER,
    score DOUBLE,
    q_value DOUBLE,
    n_eff INTEGER,
    rank INTEGER,
    flags VARCHAR[],
    window_days INTEGER,
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    method VARCHAR,
    entity_group VARCHAR[],
    related_group VARCHAR[],
    observed DOUBLE,
    expected DOUBLE,
    p_value DOUBLE,
    explained_by VARCHAR[],
    first_detected_at TIMESTAMPTZ,
    detected_at TIMESTAMPTZ NOT NULL
"""

ADDED_COLUMNS = (
    ("entity_group", "VARCHAR[]"),
    ("related_group", "VARCHAR[]"),
    ("observed", "DOUBLE"),
    ("expected", "DOUBLE"),
    ("p_value", "DOUBLE"),
    ("explained_by", "VARCHAR[]"),
)
# Columns of one finding as produced by an analysis (the rest is set when writing).
_ROW_DEFAULTS: dict[str, Any] = {
    "metric": None,
    "state": None,
    "related_entity_id": None,
    "related_metric": None,
    "related_state": None,
    "lag_hours": None,
    "score": None,
    "q_value": None,
    "n_eff": None,
    "flags": [],
    "entity_group": [],
    "related_group": [],
    "observed": None,
    "expected": None,
    "p_value": None,
    "explained_by": [],
}
_ROW_KEYS = frozenset(
    {*_ROW_DEFAULTS, "finding_id", "entity_id", "rank", "period_start", "period_end"}
)

# Forward-fill gaps up to this many hours (a sensor that did not change for a while).
FFILL_LIMIT = 3
# A pair needs at least this many common hours to be tested.
MIN_PAIR_HOURS = 168
# Lag-0 correlation above this is most likely the same quantity twice.
DUPLICATE_R = 0.95
# Residuals are high-pass filtered: their centred moving average over this many hours
# is subtracted, so slow trends (growing memory, uptime, a filling disk) do not
# correlate with each other; what remains is co-movement within hours to days.
HIGHPASS_HOURS = 168
# Entities listed with a finding as members of the same group (near-duplicates).
MAX_GROUP_MEMBERS = 20
# Upper bound of series in one run (the pair matrices grow with its square).
MAX_SERIES = 2000
# States of an enum entity turned into share series.
MAX_STATES_PER_ENTITY = 3
# A series needs at least this many hours of data (after the forward fill).
MIN_SERIES_HOURS = 168
# ... and its value must change in at least this share of consecutive known hours:
# a nearly constant series with a few steps correlates perfectly with any other one.
MIN_CHANGE_SHARE = 0.05
# A series must have data within this many hours of the window's end.
MAX_STALE_HOURS = 72
# Share of hours with the same positive step that makes a mean series a clock.
CLOCK_SHARE = 0.8
# Standardised residuals are clipped to this many standard deviations.
Z_CLIP = 4.0
# Minimum variance (in standardised units) of each series over a pair's common hours.
MIN_OVERLAP_VAR = 0.05


@dataclass(frozen=True, slots=True)
class CorrelationParams:
    window_days: int = 365
    max_lag: int = 24
    min_coverage: float = 0.7
    min_r: float = 0.3
    max_q: float = 0.01
    top: int = 100
    outside_temp: str | None = None
    timezone: str = "UTC"
    include_states: bool = True
    labels: ExclusionLabels = DEFAULT_LABELS
    # Significance method: "v2" calibrated (ADR 062), "v1" n_eff + Bonferroni (ADR 042).
    method: str = "v2"
    seed: int = 0  # of the null sample (v2), so a run is reproducible


@dataclass(frozen=True, slots=True)
class Series:
    entity_id: str
    metric: str  # mean | delta | share
    state: str | None = None

    @property
    def key(self) -> str:
        return f"{self.entity_id}={self.state}" if self.state is not None else self.entity_id


@dataclass(slots=True)
class Grid:
    hours: npt.NDArray[np.datetime64]
    local_hour: npt.NDArray[np.int64]
    local_dow: npt.NDArray[np.int64]
    series: list[Series]
    values: FloatArray  # hours x series, NaN = missing


@dataclass(slots=True)
class CorrelationResult:
    window_days: int
    series_total: int = 0
    series_used: int = 0
    hours: int = 0
    pairs_tested: int = 0
    findings: int = 0
    series_excluded: int = 0
    temperature_archive_hours: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    notes: list[str] = field(default_factory=list)
    significance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "series_total": self.series_total,
            "series_used": self.series_used,
            "hours": self.hours,
            "pairs_tested": self.pairs_tested,
            "findings": self.findings,
            "series_excluded": self.series_excluded,
            "temperature_archive_hours": self.temperature_archive_hours,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "notes": self.notes,
            "significance": self.significance,
        }


def ensure_schema(con: duckdb.DuckDBPyConnection, alias: str = LAKE_ALIAS) -> None:
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {alias}.{SCHEMA}")
    con.execute(f"CREATE TABLE IF NOT EXISTS {alias}.{SCHEMA}.{FINDING} ({FINDING_COLUMNS})")
    # Columns added after the first release of the table (1b.1 follow-up, 1b.2).
    for column, type_ in ADDED_COLUMNS:
        con.execute(
            f"ALTER TABLE {alias}.{SCHEMA}.{FINDING} ADD COLUMN IF NOT EXISTS {column} {type_}"
        )


# ---- 1. hourly grid ----------------------------------------------------------------


def _exists(con: duckdb.DuckDBPyConnection, alias: str, schema: str, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM (SELECT table_name AS t, schema_name AS s, database_name AS d "
        "FROM duckdb_tables() UNION ALL SELECT view_name, schema_name, database_name "
        "FROM duckdb_views()) WHERE d = $d AND s = $s AND t = $t",
        {"d": alias, "s": schema, "t": table},
    ).fetchone()
    return bool(row and row[0])


def _statistics_sql(alias: str) -> str:
    s = f"{alias}.silver.statistics"
    # $start, $end: grid bounds (UTC hours, end exclusive)
    return f"""
SELECT statistic_id AS entity_id, 'mean' AS metric, NULL::VARCHAR AS state,
       period_start AS h, mean AS v
FROM {s}
WHERE period = 'hour' AND period_start >= $start AND period_start < $end AND mean IS NOT NULL
UNION ALL
SELECT entity_id, 'delta', NULL, h, v FROM (
  SELECT statistic_id AS entity_id, period_start AS h,
         CASE WHEN lag(period_start) OVER w = period_start - INTERVAL 1 HOUR
              THEN sum - lag(sum) OVER w END AS v
  FROM {s}
  WHERE period = 'hour' AND mean IS NULL AND sum IS NOT NULL
    AND period_start >= $start - INTERVAL 1 HOUR AND period_start < $end
  WINDOW w AS (PARTITION BY statistic_id ORDER BY period_start)
)
WHERE h >= $start AND v >= 0
"""


def _weather_temp_sql(alias: str, entity_id: str) -> str:
    """Hourly mean of the ``temperature`` attribute of a ``weather.*`` entity (it has no
    statistics); used as the outside temperature driver."""
    return f"""
SELECT entity_id, 'mean' AS metric, NULL::VARCHAR AS state,
       date_trunc('hour', last_updated) AS h,
       avg(TRY_CAST(attributes ->> 'temperature' AS DOUBLE)) AS v
FROM {alias}.silver.states
WHERE entity_id = {sql_literal(entity_id)} AND NOT is_missing
  AND last_updated >= $start AND last_updated < $end
GROUP BY ALL
"""


def _targets_sql(alias: str, has_catalog: bool) -> str:
    """(entity_id, state) pairs to turn into share series."""
    if has_catalog:
        # binary: the state "on" if seen, else the less frequent of the two top states;
        # enum: up to MAX_STATES_PER_ENTITY most frequent states.
        return f"""
WITH e AS (
  SELECT entity_id, kind, top_states FROM {alias}.{SCHEMA}.entity
  WHERE kind IN ('binary', 'enum') AND NOT coalesce(disabled, FALSE)
    AND len(coalesce(top_states, [])) >= 2
), s AS (
  SELECT entity_id, kind, u.state, u.n, row_number() OVER (
    PARTITION BY entity_id ORDER BY u.n DESC, u.state) AS rn
  FROM e, unnest(e.top_states) AS t(u)
)
SELECT entity_id, state FROM s
WHERE kind = 'enum' AND rn <= {MAX_STATES_PER_ENTITY}
UNION ALL
SELECT entity_id, coalesce(
  max(state) FILTER (WHERE state = 'on'), max(state) FILTER (WHERE rn = 2)) AS state
FROM s WHERE kind = 'binary' GROUP BY entity_id
"""
    # No catalog yet: binary domains only, state "on".
    return f"""
SELECT DISTINCT entity_id, 'on' AS state FROM {alias}.silver.states
WHERE domain IN ('binary_sensor', 'input_boolean', 'switch', 'light', 'fan')
  AND last_updated >= $start AND last_updated < $end
"""


def _shares_sql(alias: str) -> str:
    # Each state row lasts until the next row of the entity (or the end of the grid);
    # the interval is split into hours and its seconds summed per (hour, state).
    return f"""
WITH iv AS (
  SELECT entity_id, state, is_missing,
         greatest(last_updated, $start) AS t0,
         lead(last_updated, 1, $end) OVER (
           PARTITION BY entity_id ORDER BY last_updated) AS t1
  FROM {alias}.silver.states
  WHERE entity_id IN (SELECT entity_id FROM share_targets)
    AND last_updated >= $start AND last_updated < $end
), hourly AS (
  SELECT entity_id, state, is_missing, h,
         epoch(least(t1, h + INTERVAL 1 HOUR)) - epoch(greatest(t0, h)) AS secs
  FROM (
    SELECT *, unnest(generate_series(
      date_trunc('hour', t0), t1 - INTERVAL 1 MICROSECOND, INTERVAL 1 HOUR)) AS h
    FROM iv WHERE t1 > t0
  )
), known AS (
  SELECT entity_id, h, sum(secs) AS secs
  FROM hourly WHERE NOT is_missing GROUP BY ALL
), in_state AS (
  SELECT entity_id, state, h, sum(secs) AS secs
  FROM hourly WHERE NOT is_missing GROUP BY ALL
)
SELECT k.entity_id, 'share' AS metric, t.state, k.h,
       coalesce(i.secs, 0) / k.secs AS v
FROM known AS k
JOIN share_targets AS t USING (entity_id)
LEFT JOIN in_state AS i ON i.entity_id = k.entity_id AND i.state = t.state AND i.h = k.h
WHERE k.secs >= 1800
"""


def load_grid(
    con: duckdb.DuckDBPyConnection,
    params: CorrelationParams,
    *,
    now: datetime | None = None,
    alias: str = LAKE_ALIAS,
) -> Grid | None:
    """Build the hourly grid; ``None`` when there are no data in the window."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    end = now.replace(minute=0, second=0, microsecond=0)  # the current hour is incomplete
    start = end - timedelta(days=params.window_days)
    bounds = {"start": start, "end": end}

    parts: list[str] = []
    if _exists(con, alias, "silver", "statistics"):
        parts.append(_statistics_sql(alias))
    if params.include_states and _exists(con, alias, "silver", "states"):
        has_catalog = _exists(con, alias, SCHEMA, "entity")
        targets = _targets_sql(alias, has_catalog)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE share_targets AS {targets}",
            bounds if not has_catalog else {},
        )
        con.execute("DELETE FROM share_targets WHERE state IS NULL")
        parts.append(_shares_sql(alias))
    temp = params.outside_temp
    if temp and temp.startswith("weather.") and _exists(con, alias, "silver", "states"):
        parts.append(_weather_temp_sql(alias, temp))
    if not parts:
        return None
    union = "\nUNION ALL\n".join(f"({p})" for p in parts)
    try:
        con.execute(f"CREATE OR REPLACE TEMP TABLE corr_long AS {union}", bounds)
        first = con.execute("SELECT min(h) FROM corr_long").fetchone()
        if first is None or first[0] is None:
            return None
        grid_start = first[0].astimezone(UTC)
        hours = con.execute(
            "SELECT h, hour(timezone($tz, h))::BIGINT AS lh, "
            "isodow(timezone($tz, h))::BIGINT AS dow "
            "FROM (SELECT unnest(generate_series($s, $e - INTERVAL 1 HOUR, INTERVAL 1 HOUR)) "
            "AS h) ORDER BY h",
            {"tz": params.timezone, "s": grid_start, "e": end},
        ).fetchnumpy()
        series_rows = con.execute(
            "SELECT entity_id, metric, state, count(*) AS n FROM corr_long "
            "GROUP BY ALL ORDER BY n DESC, entity_id, metric, state"
        ).fetchall()
        series = [
            Series(str(e), str(m), None if s is None else str(s)) for e, m, s, _ in series_rows
        ]
        con.execute(
            "CREATE OR REPLACE TEMP TABLE corr_series AS SELECT "
            "unnest($e::VARCHAR[]) AS entity_id, unnest($m::VARCHAR[]) AS metric, "
            "unnest($st::VARCHAR[]) AS state, unnest($i::BIGINT[]) AS idx",
            {
                "e": [s.entity_id for s in series],
                "m": [s.metric for s in series],
                "st": [s.state for s in series],
                "i": list(range(len(series))),
            },
        )
        cells = con.execute(
            "SELECT s.idx::BIGINT AS j, "
            "((epoch(l.h) - epoch($s)) // 3600)::BIGINT AS i, l.v::DOUBLE AS v "
            "FROM corr_long AS l JOIN corr_series AS s "
            "ON s.entity_id = l.entity_id AND s.metric = l.metric "
            "AND s.state IS NOT DISTINCT FROM l.state "
            "WHERE l.h >= $s AND l.v IS NOT NULL AND isfinite(l.v)",
            {"s": grid_start},
        ).fetchnumpy()
    finally:
        for t in ("corr_long", "corr_series", "share_targets"):
            con.execute(f"DROP TABLE IF EXISTS {t}")

    values = np.full((len(hours["h"]), len(series)), np.nan)
    rows = np.asarray(cells["i"], dtype=np.int64)
    cols = np.asarray(cells["j"], dtype=np.int64)
    ok = (rows >= 0) & (rows < values.shape[0])
    values[rows[ok], cols[ok]] = np.asarray(cells["v"], dtype=np.float64)[ok]
    return Grid(
        hours=np.asarray(hours["h"]),
        local_hour=np.asarray(hours["lh"], dtype=np.int64),
        local_dow=np.asarray(hours["dow"], dtype=np.int64),
        series=series,
        values=values,
    )


# ---- 2. cleaning and residuals -----------------------------------------------------


def ffill(values: FloatArray, limit: int = FFILL_LIMIT) -> FloatArray:
    """Carry the last value forward at most ``limit`` rows (per column), like pandas
    ``ffill(limit=...)``: longer gaps keep their remaining rows missing."""
    out = values.copy()
    for _ in range(limit):
        prev = np.vstack([np.full((1, out.shape[1]), np.nan), out[:-1]])
        fill = np.isnan(out) & ~np.isnan(prev)
        if not fill.any():
            break
        out[fill] = prev[fill]
    return out


def _drop_excluded(
    grid: Grid, excluded: frozenset[str], keep_entity: str | None
) -> tuple[Grid, int]:
    """Leave out series of entities labelled for no analysis (ADR 044); the outside
    temperature stays a driver."""
    idx = [
        i
        for i, s in enumerate(grid.series)
        if s.entity_id not in excluded or s.entity_id == keep_entity
    ]
    dropped = len(grid.series) - len(idx)
    if not dropped:
        return grid, 0
    return (
        Grid(
            hours=grid.hours,
            local_hour=grid.local_hour,
            local_dow=grid.local_dow,
            series=[grid.series[i] for i in idx],
            values=grid.values[:, idx],
        ),
        dropped,
    )


def clean(grid: Grid, min_coverage: float, keep_entity: str | None = None) -> Grid:
    """Keep series with enough, dense, current and changing data.

    Coverage is measured over the series' own span (first to last known hour), not the
    whole grid: an entity added half a year ago still takes part in a yearly window;
    pairs are then compared only on the hours both have (``MIN_PAIR_HOURS``).

    A series must reach the last ``MAX_STALE_HOURS`` of the window: entities that were
    removed (statistics of a deleted integration stay in HA) are not current findings.
    Clocks (an uptime: the hourly mean grows by the same step nearly every hour) only
    correlate through common restarts and are left out. ``keep_entity`` (the outside
    temperature) is exempt from both.
    """
    values = ffill(grid.values)
    known = ~np.isnan(values)
    n_known = known.sum(axis=0)
    t_len = values.shape[0]
    first = np.where(n_known > 0, known.argmax(axis=0), 0)
    last = np.where(n_known > 0, t_len - 1 - known[::-1].argmax(axis=0), -1)
    span = np.maximum(last - first + 1, 1)
    coverage = n_known / span
    both = known[1:] & known[:-1]
    with np.errstate(invalid="ignore"):
        changed = both & (np.abs(np.diff(values, axis=0)) > 1e-9)
        change_share = changed.sum(axis=0) / np.maximum(both.sum(axis=0), 1)
    current = last >= t_len - MAX_STALE_HOURS
    exempt = np.array([s.entity_id == keep_entity for s in grid.series], dtype=bool)
    keep = (
        (n_known >= MIN_SERIES_HOURS)
        & (coverage >= min_coverage)
        & (change_share >= MIN_CHANGE_SHARE)
        & ((current & ~_clocks(values, grid.series)) | exempt)
    )
    keep = np.asarray(keep, dtype=bool)
    if keep.sum() > MAX_SERIES:
        # Keep the longest series; the order is stable (load order breaks ties).
        order = np.argsort(-n_known, kind="stable")
        chosen = np.zeros_like(keep)
        chosen[order[keep[order]][:MAX_SERIES]] = True
        keep = chosen
    idx = np.flatnonzero(keep)
    return Grid(
        hours=grid.hours,
        local_hour=grid.local_hour,
        local_dow=grid.local_dow,
        series=[grid.series[i] for i in idx],
        values=values[:, idx],
    )


def _clocks(values: FloatArray, series: list[Series]) -> npt.NDArray[np.bool_]:
    """Mean series whose hourly step equals their median positive step in at least
    ``CLOCK_SHARE`` of the hours (uptime counters)."""
    out = np.zeros(len(series), dtype=bool)
    with np.errstate(invalid="ignore"):
        diff = np.diff(values, axis=0)
    for i, s in enumerate(series):
        if s.metric != "mean":
            continue
        d = diff[:, i]
        d = d[~np.isnan(d)]
        pos = d[d > 0]
        if len(d) < MIN_SERIES_HOURS or len(pos) == 0:
            continue
        step = float(np.median(pos))
        out[i] = float(np.mean(np.abs(d - step) <= 0.01 * step)) >= CLOCK_SHARE
    return out


def residuals(grid: Grid, outside_temp: str | None) -> tuple[FloatArray, list[Series]]:
    """Residuals after hour-of-day, day-of-week and outside temperature (if present)."""
    n = len(grid.hours)
    cols: list[FloatArray] = [np.ones(n)]
    cols += [(grid.local_hour == h).astype(float) for h in range(1, 24)]
    cols += [(grid.local_dow == d).astype(float) for d in range(2, 8)]
    driver = None
    if outside_temp:
        driver = next(
            (
                i
                for i, s in enumerate(grid.series)
                if s.entity_id == outside_temp and s.metric == "mean"
            ),
            None,
        )
    # Hours without a (forward-filled) outside temperature are left out: a guessed
    # temperature would leave the weather in the residuals of exactly those hours.
    known = np.ones(n, dtype=bool)
    if driver is not None:
        known = ~np.isnan(grid.values[:, driver])
        t = np.nan_to_num(grid.values[:, driver])
        cols += [t, t**2]
    x = np.column_stack(cols)
    out = np.full(grid.values.shape, np.nan)
    keep: list[int] = []
    for j in range(grid.values.shape[1]):
        if j == driver:
            continue  # the driver is explained away, not a candidate
        y = grid.values[:, j]
        ok = ~np.isnan(y) & known
        if ok.sum() < x.shape[1] * 5:
            continue
        beta, *_ = np.linalg.lstsq(x[ok], y[ok], rcond=None)
        out[ok, j] = y[ok] - x[ok] @ beta
        keep.append(j)
    return out[:, keep], [grid.series[j] for j in keep]


# ---- 3.+4. lagged correlations and significance ---------------------------------------


@dataclass(slots=True)
class Pairs:
    leader: npt.NDArray[np.int64]
    follower: npt.NDArray[np.int64]
    lag: npt.NDArray[np.int64]
    r: FloatArray
    n_eff: FloatArray
    q: FloatArray
    # Group of each column: columns joined by a lag-0 correlation >= DUPLICATE_R share
    # the smallest index of their group (near-duplicates: one quantity measured twice).
    groups: npt.NDArray[np.int64] | None = None
    # v2 (ADR 062): calibrated p-values, structural flags and explaining columns.
    p: FloatArray | None = None
    flags: list[list[str]] | None = None
    explained_by: list[list[int]] | None = None


@dataclass(slots=True)
class _Scan:
    z: FloatArray  # standardised, clipped residuals, missing = 0
    mask: FloatArray
    r1: FloatArray  # lag-1 autocorrelation of each column
    best_r: FloatArray  # [i, j]: strongest corr(x_i(t), x_j(t + k)) over k >= 0
    best_lag: npt.NDArray[np.int64]
    best_n: FloatArray  # common hours at that lag
    groups: npt.NDArray[np.int64]


def _scan(resid: FloatArray, max_lag: int) -> _Scan:
    """Best-lag correlation of every ordered pair of columns (pairwise complete hours)."""
    t_len, n = resid.shape
    mask = (~np.isnan(resid)).astype(float)
    mean = np.nanmean(resid, axis=0)
    std = np.nanstd(resid, axis=0)
    std[std == 0] = 1.0
    # Winsorised: a few spikes (a backup, a reboot) must not carry a correlation alone.
    z = np.clip(np.nan_to_num((resid - mean) / std), -Z_CLIP, Z_CLIP)
    z2 = z**2

    r1 = np.zeros(n)
    for i in range(n):
        both = (mask[:-1, i] * mask[1:, i]).astype(bool)
        if both.sum() > 2:
            a, b = z[:-1, i][both], z[1:, i][both]
            if a.std() > 0 and b.std() > 0:
                r1[i] = float(np.corrcoef(a, b)[0, 1])
    r1 = np.clip(np.nan_to_num(r1), -0.99, 0.99)

    best_r = np.zeros((n, n))
    best_lag = np.zeros((n, n), dtype=np.int64)
    best_n = np.zeros((n, n))
    groups = np.arange(n, dtype=np.int64)
    for k in range(max_lag + 1):
        if t_len - k < MIN_PAIR_HOURS:
            break
        a, b = z[: t_len - k], z[k:]
        ma, mb = mask[: t_len - k], mask[k:]
        cnt = ma.T @ mb
        c = _pearson(
            cnt,
            a.T @ b,
            sa=a.T @ mb,
            sb=ma.T @ b,
            saa=z2[: t_len - k].T @ mb,
            sbb=ma.T @ z2[k:],
        )
        if k == 0:
            groups = _groups(np.triu(np.abs(c) >= DUPLICATE_R, 1) & (cnt >= MIN_PAIR_HOURS))
            c = np.triu(c, 1)
        better = np.abs(c) > np.abs(best_r)
        best_r[better] = c[better]
        best_lag[better] = k
        best_n[better] = cnt[better]
    np.fill_diagonal(best_r, 0)
    return _Scan(z, mask, r1, best_r, best_lag, best_n, groups)


def lagged_correlations(resid: FloatArray, max_lag: int) -> Pairs:
    """Best-lag correlation of every pair of columns, with FDR-corrected q-values
    (method v1, ADR 042: lag-1 n_eff and Bonferroni over lags).

    ``r[i, j]`` at lag ``k`` is corr(x_i(t), x_j(t + k)): column ``i`` leads by ``k`` hours.
    Correlations use only hours both series have (pairwise complete observations).
    """
    sc = _scan(resid, max_lag)
    lead, follow = np.nonzero(sc.best_r)
    r = sc.best_r[lead, follow]
    lag = sc.best_lag[lead, follow]
    phi = sc.r1[lead] * sc.r1[follow]
    n_eff = np.maximum(sc.best_n[lead, follow] * (1 - phi) / (1 + phi), 10)
    t_stat = r * np.sqrt((n_eff - 2) / np.maximum(1 - r**2, 1e-12))
    p = np.array([math.erfc(abs(float(x)) / math.sqrt(2)) for x in t_stat])
    p = np.minimum(p * (max_lag + 1), 1.0)  # Bonferroni over the tested lags
    return Pairs(
        leader=lead.astype(np.int64),
        follower=follow.astype(np.int64),
        lag=lag,
        r=r,
        n_eff=n_eff,
        q=benjamini_hochberg(p),
        groups=sc.groups,
        p=p,
    )


def lagged_correlations_v2(
    resid: FloatArray,
    max_lag: int,
    *,
    min_r: float = 0.3,
    max_q: float = 0.01,
    seed: int = 0,
) -> tuple[Pairs, dict[str, Any]]:
    """Best-lag correlations with calibrated p-values and structural flags (ADR 062).

    Significance (``significance`` module): ``z = r / sd`` with the Bartlett/xDF
    variance under independence, p-value of the pair's max ``|z|`` over both directions
    and all lags from the empirical null of circularly shifted pairs, Benjamini-Hochberg
    over pairs. Flags of significant relations: ``common_factor``, ``indirect`` (with
    the explaining column), ``unstable``. Returns the pairs and run statistics (no
    entity names: null size, factors removed, histogram of p and q).
    """
    sc = _scan(resid, max_lag)
    n = sc.z.shape[1]
    rho = sig.autocorrelations(sc.z, sc.mask, sig.taper_lags(sc.z.shape[0]))
    factor = sig.variance_factor(rho)
    lead, follow = np.nonzero(sc.best_r)
    r = sc.best_r[lead, follow]
    lag = sc.best_lag[lead, follow]
    n_obs = np.maximum(sc.best_n[lead, follow], 2.0)
    zstat = np.abs(r) / np.sqrt(factor[lead, follow] / n_obs)
    n_eff = n_obs / factor[lead, follow]

    # One statistic per unordered pair: the stronger direction (as in the null).
    pair_z = np.zeros((n, n))
    pair_z[lead, follow] = zstat
    pair_z = np.maximum(pair_z, pair_z.T)
    iu, ju = np.triu_indices(n, 1)
    tested = pair_z[iu, ju] > 0
    rng = np.random.default_rng(seed)
    null = sig.circular_null(sc.z, sc.mask, factor, max_lag, rng)
    p_pair = np.ones((n, n))
    q_pair = np.ones((n, n))
    if tested.any():
        p_u = null.pvalues(pair_z[iu, ju][tested])
        q_u = benjamini_hochberg(p_u)
        p_pair[iu[tested], ju[tested]] = p_u
        q_pair[iu[tested], ju[tested]] = q_u
        p_pair = np.minimum(p_pair, p_pair.T)
        q_pair = np.minimum(q_pair, q_pair.T)
    p = p_pair[lead, follow]
    q = q_pair[lead, follow]
    accepted = q <= max_q
    p_cut = float(p[accepted].max()) if accepted.any() else 0.0

    flags: list[list[str]] = [[] for _ in range(len(r))]
    explained: list[list[int]] = [[] for _ in range(len(r))]
    sel = np.flatnonzero(accepted & (np.abs(r) >= min_r))
    stats: dict[str, Any] = {
        "null_size": null.size,
        "factors_removed": 0,
        "p_histogram": np.histogram(p, bins=10, range=(0, 1))[0].tolist(),
        "q_histogram": np.histogram(q, bins=10, range=(0, 1))[0].tolist(),
    }
    if len(sel):
        # Common factors: the relation largely disappears without them.
        zf, k = sig.remove_factors(sc.z)
        stats["factors_removed"] = k
        if k:
            r_f = sig.lagged_r(zf, lead[sel], follow[sel], lag[sel])
            gone = (np.abs(r_f) < min_r) & (np.abs(r_f) < sig.FACTOR_KEEP_SHARE * np.abs(r[sel]))
            for j in sel[gone]:
                flags[j].append("common_factor")
        # Indirect: explained by a third significant series with consistent lags.
        structure = _explained_by(
            sel,
            known=np.flatnonzero(accepted),
            groups=sc.groups,
            lead=lead,
            follow=follow,
            lag=lag,
            r=r,
            factor=factor,
            n_obs=n_obs,
            p_cut=p_cut,
            n=n,
        )
        _apply_structure(structure, flags, explained)
        bad = sig.unstable(sc.z, sc.mask, a=lead[sel], b=follow[sel], lag=lag[sel], r=r[sel])
        for j in sel[bad]:
            flags[j].append("unstable")
    return (
        Pairs(
            leader=lead.astype(np.int64),
            follower=follow.astype(np.int64),
            lag=lag,
            r=r,
            n_eff=n_eff,
            q=q,
            groups=sc.groups,
            p=p,
            flags=flags,
            explained_by=explained,
        ),
        stats,
    )


@dataclass(slots=True)
class _Structure:
    indirect: list[tuple[int, int]]  # (entry, explaining column)
    latent: list[tuple[int, list[int]]]  # (entry, columns sharing the hidden driver)
    hub: list[int]  # entries where a or b is itself the driver of the neighbours


def _apply_structure(
    structure: _Structure, flags: list[list[str]], explained: list[list[int]]
) -> None:
    for j, c in structure.indirect:
        flags[j].append("indirect")
        explained[j].append(c)
    for j, _members in structure.latent:
        if "common_factor" not in flags[j]:
            flags[j].append("common_factor")
    # Global factors also absorb a hub with its followers: those relations are
    # direct, the hub is observed.
    for j in structure.hub:
        if "common_factor" in flags[j]:
            flags[j].remove("common_factor")


def _nonzero(x: FloatArray) -> FloatArray:
    return np.where(np.abs(x) < 1e-6, 1e-6, x)


def _explained_by(
    sel: npt.NDArray[np.int64],
    *,
    known: npt.NDArray[np.int64],
    groups: npt.NDArray[np.int64],
    lead: npt.NDArray[np.int64],
    follow: npt.NDArray[np.int64],
    lag: npt.NDArray[np.int64],
    r: FloatArray,
    factor: FloatArray,
    n_obs: FloatArray,
    p_cut: float,
    n: int,
) -> _Structure:
    """Structure of the significant relations a - b among their common neighbours C
    (significant with both at the run's level, lags consistent along a -> c -> b):

    * **indirect** (PC step of order 1): the partial correlation of a and b given one
      c is no longer significant at the level the run accepted;
    * **latent** (local common driver, e.g. the occupancy of one room): with at least
      two such neighbours, a one-factor model explains r_ab: the tetrad estimate
      median(r_ac r_bd / r_cd) is within two standard errors (at least 0.05) of r_ab.
      A direct relation on top of a shared driver makes r_ab larger than that; a or b
      with a loading near 1 is itself the driver (a hub), not a hidden cause. With
      only three series sharing a driver the model is not identifiable (no tetrad).
    """
    out = _Structure([], [], [])
    if not len(sel) or p_cut <= 0:
        return out
    # Strongest direction of every unordered pair: r and signed lead of x over y (h).
    mag = np.zeros((n, n))
    mag[lead, follow] = np.abs(r)
    strongest = (mag[lead, follow] > mag[follow, lead]) | (
        (mag[lead, follow] == mag[follow, lead]) & (lead < follow)
    )
    a_s, b_s = lead[strongest], follow[strongest]
    big_r = np.zeros((n, n))
    big_d = np.zeros((n, n), dtype=np.int64)
    big_r[a_s, b_s] = big_r[b_s, a_s] = r[strongest]
    big_d[a_s, b_s], big_d[b_s, a_s] = lag[strongest], -lag[strongest]
    # Neighbours: significant at the run's level, whatever their strength.
    sig_m = np.zeros((n, n), dtype=bool)
    sig_m[lead[known], follow[known]] = True
    sig_m |= sig_m.T
    for j in sel:
        a, b = int(lead[j]), int(follow[j])
        # A near-duplicate of a or b explains nothing: it is the same measurement.
        cands = np.flatnonzero(sig_m[a] & sig_m[b] & (groups != groups[a]) & (groups != groups[b]))
        if not len(cands):
            continue
        consistent = np.abs(big_d[a, cands] + big_d[cands, b] - lag[j]) <= sig.LAG_TOLERANCE
        cands = cands[consistent]
        if not len(cands):
            continue
        sd = math.sqrt(factor[a, b] / n_obs[j])
        rac, rcb = big_r[a, cands], big_r[cands, b]
        den = np.sqrt(np.maximum((1 - rac**2) * (1 - rcb**2), 1e-12))
        partial = (r[j] - rac * rcb) / den
        p_p = np.array([math.erfc(abs(float(x)) / sd / math.sqrt(2)) for x in partial])
        best = int(np.argmax(p_p))
        # Not significant any more at the level the run accepted (a single test at a
        # fixed lag: the normal tail is the conservative choice here).
        if p_p[best] > max(p_cut, 1e-12) and abs(partial[best]) < abs(r[j]):
            out.indirect.append((int(j), int(cands[best])))
            continue
        if _is_hub(big_r, a=a, b=b, r_ab=float(r[j]), cands=cands):
            out.hub.append(int(j))
            continue
        members = _one_factor(
            big_r, a=a, b=b, r_ab=float(r[j]), sd=sd, cands=cands[np.argsort(-np.abs(rac * rcb))]
        )
        if members:
            out.latent.append((int(j), members))
    return out


def _is_hub(
    big_r: FloatArray, *, a: int, b: int, r_ab: float, cands: npt.NDArray[np.int64]
) -> bool:
    """Squared loading of a (or b) on what it shares with b and the neighbours close
    to 1: a is itself the driver, a hub with several direct followers, not a series
    driven by a hidden cause (r_ab r_ac / r_bc = 1 when a drives b and c)."""
    load_a = float(np.median(r_ab * big_r[a, cands] / _nonzero(big_r[b, cands])))
    load_b = float(np.median(r_ab * big_r[b, cands] / _nonzero(big_r[a, cands])))
    return max(load_a, load_b) >= sig.HUB_LOADING


def _one_factor(
    big_r: FloatArray, *, a: int, b: int, r_ab: float, sd: float, cands: npt.NDArray[np.int64]
) -> list[int]:
    """Columns sharing one hidden driver with a and b when the tetrad estimate of r_ab
    fits (see ``_explained_by``), else an empty list."""
    if len(cands) < 2:
        return []
    cands = cands[: sig.MAX_TETRAD_NEIGHBOURS]
    ci, di = np.triu_indices(len(cands), 1)
    c, d = cands[ci], cands[di]
    linked = np.abs(big_r[c, d]) >= sig.TETRAD_MIN_LINK
    if not linked.any():
        return []
    c, d = c[linked], d[linked]
    r_cd = big_r[c, d]
    terms = ((big_r[a, c], big_r[b, d]), (big_r[a, d], big_r[b, c]))
    est = np.concatenate([x * y / r_cd for x, y in terms])
    rel = np.concatenate([np.sqrt(1 / x**2 + 1 / y**2 + 1 / r_cd**2) for x, y in terms])
    r_hat = float(np.median(est))
    # Standard error of the tetrad estimate (delta method, every r with the sd of r_ab;
    # the estimates share their terms, so only the neighbours count).
    se_hat = abs(r_hat) * sd * float(np.median(rel)) / math.sqrt(max(1, len(cands) - 1))
    tolerance = max(sig.TETRAD_MIN_TOLERANCE, 2 * math.hypot(sd, se_hat))
    if abs(r_ab - r_hat) > tolerance:
        return []
    return sorted({int(x) for x in np.concatenate([c, d])})


def _pearson(
    cnt: FloatArray,
    sab: FloatArray,
    *,
    sa: FloatArray,
    sb: FloatArray,
    saa: FloatArray,
    sbb: FloatArray,
) -> FloatArray:
    """Pearson correlation over the hours both series have, from sums over those hours
    (the overlap of two series can have its own mean and variance)."""
    n = np.maximum(cnt, 1)
    cov = sab - sa * sb / n
    va = saa - sa**2 / n
    vb = sbb - sb**2 / n
    # A series (nearly) constant over the overlap correlates with nothing there.
    ok = (cnt >= MIN_PAIR_HOURS) & (va > MIN_OVERLAP_VAR * n) & (vb > MIN_OVERLAP_VAR * n)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.where(ok, cov / np.sqrt(np.maximum(va * vb, 1e-300)), 0.0)
    return np.clip(np.nan_to_num(r), -1.0, 1.0)


def _groups(linked: npt.NDArray[np.bool_]) -> npt.NDArray[np.int64]:
    """Connected components (union-find) of a boolean adjacency matrix; each column
    gets the smallest index of its component."""
    parent = np.arange(linked.shape[0], dtype=np.int64)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = int(parent[x])
        return x

    for i, j in zip(*np.nonzero(linked), strict=True):
        a, b = find(int(i)), find(int(j))
        if a != b:
            parent[max(a, b)] = min(a, b)
    return np.array([find(i) for i in range(len(parent))], dtype=np.int64)


def highpass(resid: FloatArray, window: int = HIGHPASS_HOURS) -> FloatArray:
    """Subtract the centred moving average over ``window`` hours (NaN-aware); hours
    whose window holds less than a quarter of known values become missing."""
    if window <= 1 or resid.shape[0] == 0:
        return resid
    known = ~np.isnan(resid)
    vals = np.where(known, resid, 0.0)
    zeros = np.zeros((1, resid.shape[1]))
    csum = np.vstack([zeros, np.cumsum(vals, axis=0)])
    ccnt = np.vstack([zeros, np.cumsum(known, axis=0)])
    t_len = resid.shape[0]
    # Symmetric window, shrunk near the ends so a trend does not leak into the edges.
    t = np.arange(t_len)
    half = np.minimum(window // 2, np.minimum(t, t_len - 1 - t))
    lo = t - half
    hi = t + half + 1
    total = csum[hi] - csum[lo]
    count = ccnt[hi] - ccnt[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = total / count
    out: FloatArray = resid - mean
    out[count < window / 4] = np.nan
    return out


def benjamini_hochberg(p: FloatArray) -> FloatArray:
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p, kind="stable")
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.empty(m)
    q[order] = np.minimum(np.minimum.accumulate(ranked[::-1])[::-1], 1.0)
    return q


# ---- 5. findings -------------------------------------------------------------------


def finding_id(window_days: int, a: Series, b: Series) -> str:
    raw = f"{FINDING_TYPE}|{window_days}|{a.key}|{a.metric}|{b.key}|{b.metric}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _devices(con: duckdb.DuckDBPyConnection, alias: str) -> dict[str, str]:
    if not _exists(con, alias, "silver", "entities"):
        return {}
    rows = con.execute(
        f"SELECT entity_id, device_id FROM {alias}.silver.entities WHERE device_id IS NOT NULL"
    ).fetchall()
    return {str(e): str(d) for e, d in rows}


def select_findings(
    pairs: Pairs,
    series: list[Series],
    params: CorrelationParams,
    devices: dict[str, str],
) -> list[dict[str, Any]]:
    """Significant pairs, unflagged ones first, each part by ``|r|``.

    Near-duplicate series form groups (``Pairs.groups``); a relation between two groups
    is reported once, by its strongest pair, with the other members listed in
    ``entity_group``/``related_group``. A pair inside one group is ``duplicate_suspect``.
    Every pair of entities appears at most once (strongest direction, lag and metric).
    """
    groups = pairs.groups if pairs.groups is not None else np.arange(len(series))
    members: dict[int, list[str]] = {}
    for i, g in enumerate(groups):
        ent = series[i].entity_id
        if ent not in members.setdefault(int(g), []):
            members[int(g)].append(ent)
    keep = (np.abs(pairs.r) >= params.min_r) & (pairs.q <= params.max_q)
    idx = np.flatnonzero(keep)
    idx = idx[np.argsort(-np.abs(pairs.r[idx]), kind="stable")]
    candidates: list[dict[str, Any]] = []
    seen: set[frozenset[Any]] = set()
    for k in idx:
        ia, ib = int(pairs.leader[k]), int(pairs.follower[k])
        a, b = series[ia], series[ib]
        if a.entity_id == b.entity_id:
            continue  # two states or metrics of one entity
        ga, gb = int(groups[ia]), int(groups[ib])
        same_group = ga == gb
        key: frozenset[Any] = (
            frozenset((a.entity_id, b.entity_id))
            if same_group
            else frozenset((("g", ga), ("g", gb)))
        )
        if key in seen:
            continue  # the same relation the other way round, at another lag or metric
        seen.add(key)
        lag = int(pairs.lag[k])
        r = float(pairs.r[k])
        flags = list(pairs.flags[k]) if pairs.flags is not None else []
        if same_group:
            flags.append("duplicate_suspect")
        dev = devices.get(a.entity_id)
        if dev is not None and dev == devices.get(b.entity_id):
            flags.append("same_device")
        candidates.append(
            {
                "finding_id": finding_id(params.window_days, a, b),
                "entity_id": a.entity_id,
                "metric": a.metric,
                "state": a.state,
                "related_entity_id": b.entity_id,
                "related_metric": b.metric,
                "related_state": b.state,
                "lag_hours": lag,
                "score": round(r, 4),
                "q_value": float(pairs.q[k]),
                "p_value": float(pairs.p[k]) if pairs.p is not None else None,
                "n_eff": round(float(pairs.n_eff[k])),
                "flags": flags,
                "explained_by": [series[c].entity_id for c in pairs.explained_by[k]]
                if pairs.explained_by is not None
                else [],
                "entity_group": [] if same_group else _others(members[ga], a.entity_id),
                "related_group": [] if same_group else _others(members[gb], b.entity_id),
            }
        )
    # Trivial pairs (one quantity twice, two sensors of one device) go last.
    candidates.sort(key=lambda f: bool(f["flags"]))  # stable: |r| order kept within
    out = candidates[: params.top]
    for rank, f in enumerate(out, start=1):
        f["rank"] = rank
    return out


def _others(group: list[str], entity_id: str) -> list[str]:
    return sorted(e for e in group if e != entity_id)[:MAX_GROUP_MEMBERS]


def _pairs(resid: FloatArray, params: CorrelationParams, result: CorrelationResult) -> Pairs:
    """Lagged correlations with the configured significance method."""
    if params.method == "v1":
        return lagged_correlations(resid, params.max_lag)
    pairs, stats = lagged_correlations_v2(
        resid, params.max_lag, min_r=params.min_r, max_q=params.max_q, seed=params.seed
    )
    # Distribution of p and q of all tested pairs (ADR 062 point 10), no entity names.
    result.significance = stats
    logger.info("Correlations (%d d) significance: %s", params.window_days, stats)
    return pairs


def _method(params: CorrelationParams, used_temp: bool) -> str:
    drivers = "hour of day, day of week" + (
        f", outside temperature ({params.outside_temp})" if used_temp else ""
    )
    if params.method == "v1":
        test = "lag-1 n_eff, Bonferroni over lags, BH-FDR q-values (v1)"
    else:
        test = (
            "Bartlett/xDF variance, p-value of max |z| over lags from circularly shifted "
            "pairs (GPD tail), BH-FDR over pairs; flags common_factor, indirect "
            "(explained_by), unstable (v2, ADR 062)"
        )
    return (
        f"hourly residuals after {drivers}, minus their {HIGHPASS_HOURS} h moving average; "
        f"lag 0..{params.max_lag} h; near-duplicates (lag-0 |r| >= {DUPLICATE_R}) grouped; "
        f"entity_id leads related_entity_id by lag_hours; {test}"
    )


def write_findings(
    con: duckdb.DuckDBPyConnection,
    findings: list[dict[str, Any]],
    *,
    window_days: int,
    period_start: datetime | None,
    period_end: datetime | None,
    method: str,
    now: datetime,
    finding_type: str = FINDING_TYPE,
    alias: str = LAKE_ALIAS,
) -> None:
    """Replace the findings of ``finding_type`` and ``window_days`` in one transaction.

    ``period_start``/``period_end`` of a finding default to the given ones (the data
    window); an anomaly or a change point carries its own.
    """
    ensure_schema(con, alias)
    t = f"{alias}.{SCHEMA}.{FINDING}"
    con.execute(
        "CREATE OR REPLACE TEMP TABLE new_finding AS "
        f"SELECT * EXCLUDE (finding_type, window_days, method, first_detected_at, "
        f"detected_at) FROM {t} WHERE FALSE"
    )
    try:
        if findings:
            con.executemany(
                "INSERT INTO new_finding BY NAME SELECT $finding_id AS finding_id, "
                "$entity_id AS entity_id, $metric AS metric, $state AS state, "
                "$related_entity_id AS related_entity_id, $related_metric AS related_metric, "
                "$related_state AS related_state, $lag_hours::INTEGER AS lag_hours, "
                "$score::DOUBLE AS score, $q_value::DOUBLE AS q_value, "
                "$n_eff::INTEGER AS n_eff, $rank AS rank, $flags::VARCHAR[] AS flags, "
                "$entity_group::VARCHAR[] AS entity_group, "
                "$related_group::VARCHAR[] AS related_group, "
                "$observed::DOUBLE AS observed, $expected::DOUBLE AS expected, "
                "$p_value::DOUBLE AS p_value, $explained_by::VARCHAR[] AS explained_by, "
                "coalesce($period_start::TIMESTAMPTZ, $ps::TIMESTAMPTZ) AS period_start, "
                "coalesce($period_end::TIMESTAMPTZ, $pe::TIMESTAMPTZ) AS period_end",
                [
                    {
                        **_ROW_DEFAULTS,
                        "period_start": None,
                        "period_end": None,
                        **{k: v for k, v in f.items() if k in _ROW_KEYS},
                        "ps": period_start,
                        "pe": period_end,
                    }
                    for f in findings
                ],
            )
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                f"""
INSERT INTO {t} BY NAME
SELECT n.*, $type AS finding_type, $window AS window_days, $method AS method,
       coalesce(o.first_detected_at, $now) AS first_detected_at, $now AS detected_at
FROM new_finding AS n
LEFT JOIN (
  SELECT finding_id, min(first_detected_at) AS first_detected_at FROM {t}
  WHERE finding_type = $type AND window_days = $window GROUP BY ALL
) AS o USING (finding_id)
""",
                {
                    "type": finding_type,
                    "window": window_days,
                    "method": method,
                    "now": now,
                },
            )
            con.execute(
                f"DELETE FROM {t} WHERE finding_type = $type AND window_days = $window "
                "AND detected_at <> $now",
                {"type": finding_type, "window": window_days, "now": now},
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.execute("DROP TABLE IF EXISTS new_finding")


def run(
    con: duckdb.DuckDBPyConnection,
    params: CorrelationParams,
    *,
    now: datetime | None = None,
    alias: str = LAKE_ALIAS,
) -> CorrelationResult:
    """Compute correlations for one window and replace its findings."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    result = CorrelationResult(window_days=params.window_days)
    ensure_schema(con, alias)
    grid = load_grid(con, params, now=now, alias=alias)
    if grid is None or len(grid.hours) < MIN_PAIR_HOURS:
        result.notes.append("not enough data in the window")
        logger.info("Correlations (%d d): not enough data", params.window_days)
        return result
    grid, result.series_excluded = _drop_excluded(
        grid, load_no_analysis(con, alias, params.labels), params.outside_temp
    )
    if params.outside_temp:
        result.temperature_archive_hours = fill_from_archive(
            con, grid, params.outside_temp, alias=alias
        )
    result.series_total = len(grid.series)
    temp_coverage = _coverage(grid, params.outside_temp)
    grid = clean(grid, params.min_coverage, params.outside_temp)
    if temp_coverage < params.min_coverage:
        # The driver must cover the whole grid (hours without it are left out of the
        # regression); a short one is not used, nor taken as a candidate.
        drop = [
            i
            for i, s in enumerate(grid.series)
            if s.entity_id == params.outside_temp and s.metric == "mean"
        ]
        if drop:
            keep = [i for i in range(len(grid.series)) if i not in drop]
            grid.series = [grid.series[i] for i in keep]
            grid.values = grid.values[:, keep]
    resid, series = residuals(grid, params.outside_temp)
    resid = highpass(resid)
    used_temp = bool(params.outside_temp) and any(
        s.entity_id == params.outside_temp and s.metric == "mean" for s in grid.series
    )
    if params.outside_temp and not used_temp:
        if temp_coverage:
            # e.g. a weather entity whose history is shorter than the window
            result.notes.append(
                f"outside temperature {params.outside_temp} covers only "
                f"{temp_coverage:.0%} of the window, not used"
            )
        else:
            result.notes.append(f"outside temperature {params.outside_temp} not in the grid")
    result.series_used = len(series)
    result.hours = len(grid.hours)
    result.period_start = _to_dt(grid.hours[0])
    result.period_end = _to_dt(grid.hours[-1]) + timedelta(hours=1)
    findings: list[dict[str, Any]] = []
    if len(series) >= 2:
        pairs = _pairs(resid, params, result)
        result.pairs_tested = len(pairs.r)
        findings = select_findings(pairs, series, params, _devices(con, alias))
    write_findings(
        con,
        findings,
        window_days=params.window_days,
        period_start=result.period_start,
        period_end=result.period_end,
        method=_method(params, used_temp),
        now=now,
        alias=alias,
    )
    result.findings = len(findings)
    logger.info("Correlations: %s", result.as_dict())
    return result


def fill_from_archive(
    con: duckdb.DuckDBPyConnection, grid: Grid, entity_id: str, *, alias: str = LAKE_ALIAS
) -> int:
    """Fill the hours the outside temperature series lacks from the stored Open-Meteo
    archive (ADR 043); add the series if the entity has no data at all. Returns the
    number of filled hours."""
    if len(grid.hours) == 0:
        return 0
    hours = [_to_dt(h) for h in grid.hours]
    archive = weather.load(con, hours[0], hours[-1] + timedelta(hours=1), alias)
    if not archive:
        return 0
    j = next(
        (i for i, s in enumerate(grid.series) if s.entity_id == entity_id and s.metric == "mean"),
        None,
    )
    if j is None:
        grid.series.append(Series(entity_id, "mean"))
        grid.values = np.hstack([grid.values, np.full((len(hours), 1), np.nan)])
        j = len(grid.series) - 1
    filled = 0
    column = grid.values[:, j]
    for i, h in enumerate(hours):
        if np.isnan(column[i]) and h in archive:
            column[i] = archive[h]
            filled += 1
    return filled


def _coverage(grid: Grid, entity_id: str | None) -> float:
    for j, s in enumerate(grid.series):
        if s.entity_id == entity_id and s.metric == "mean":
            return float((~np.isnan(ffill(grid.values[:, [j]]))).mean())
    return 0.0


def _to_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    ts: datetime = np.datetime64(value, "us").astype(datetime)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
