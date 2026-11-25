"""Calibrated significance of lagged correlations between autocorrelated series
(ADR 062 points 1-3).

* **Variance of r under independence** (Bartlett; the null part of xDF, Afyouni et al.
  2019): ``Var(r) = (1 + 2 * sum_k w_k rho_a(k) rho_b(k)) / n`` with the
  autocorrelations of both series and a Tukey taper over ``2 * sqrt(T)`` lags. Two
  smooth series correlate by chance far more often than two white ones; this puts their
  correlation on a common scale ``z = r / sd``.
* **Empirical null** of the statistic actually used, the maximum ``|z|`` over the
  tested lags: correlations of a sample of pairs after a circular shift of one series
  by whole days (at least three, so the daily cycle stays aligned and no real relation
  survives), read off one FFT cross-correlation per pair. The p-value of an observed
  pair is its exceedance probability under this null; beyond the sample a generalised
  Pareto tail fitted to the top 10 % of the null (probability weighted moments). This
  replaces the Bonferroni correction over lags.
* **Common factors** (point 2): the largest principal components of the residuals above
  the Marchenko-Pastur edge (at most five) are removed and the correlation of a pair
  recomputed; a relation that loses most of its strength is flagged ``common_factor``.
* **Indirect relations** (PC step of order 1): a pair explained by a third series C
  (partial correlation not significant, lags consistent along a -> C -> b) is flagged
  ``indirect`` with C in ``explained_by``.
* **Local common drivers** (a room's occupancy, one appliance driving a few sensors):
  when a pair and at least two common neighbours fit a one-factor model (tetrad
  constraint ``r_ab r_cd = r_ac r_bd``), the pair is flagged ``common_factor`` too.
* **Replication** (point 3): in a window of at least 180 days a relation must keep its
  sign and ``|r| >= 0.2`` in both halves, else it is flagged ``unstable``.

Everything is numpy, blockwise and float32 where it matters (lite edition, ADR 050).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int64]

# Circular shifts of the null: whole days, at least this many from either end.
MIN_SHIFT_DAYS = 3
NULL_PAIRS = 20_000
NULL_SHIFTS = 20
# The generalised Pareto tail is fitted above this quantile of the null.
TAIL_QUANTILE = 0.90
# Empirical p-values need this many null values at least as large; else the tail fit.
MIN_EXCEEDANCES = 10
MAX_FACTORS = 5
# A relation is common_factor when, after removing the factors, |r| falls below the
# reporting threshold and to less than this share of its value.
FACTOR_KEEP_SHARE = 0.6
# Lags along a -> c -> b must add up within this many hours to explain a -> b.
LAG_TOLERANCE = 2
# Local common drivers (one-factor tetrad test): neighbours used and minimum tolerance.
MAX_TETRAD_NEIGHBOURS = 12
TETRAD_MIN_TOLERANCE = 0.05
# Neighbour pairs c, d with a weaker |r_cd| make the ratio r_ac r_bd / r_cd unstable.
TETRAD_MIN_LINK = 0.1
# A series whose estimated loading on the shared driver reaches this is the driver.
HUB_LOADING = 0.85
UNSTABLE_MIN_DAYS = 180
UNSTABLE_MIN_R = 0.2
BLOCK = 512


def standardise(resid: FloatArray, clip: float = 4.0) -> tuple[FloatArray, FloatArray]:
    """Standardised, clipped residuals with missing hours as 0, and the 0/1 mask."""
    mask = (~np.isnan(resid)).astype(np.float64)
    mean = np.nanmean(resid, axis=0)
    std = np.nanstd(resid, axis=0)
    std[~(std > 0)] = 1.0
    z = np.clip(np.nan_to_num((resid - mean) / std), -clip, clip)
    return z, mask


def taper_lags(t_len: int) -> int:
    return max(1, min(t_len // 4, int(2 * math.sqrt(t_len))))


def autocorrelations(z: FloatArray, mask: FloatArray, max_lag: int) -> FloatArray:
    """Autocorrelation of each column at lags 1..max_lag (columns x lags), counting only
    hours where both ends are known."""
    t_len = z.shape[0]
    size = 1 << (2 * t_len - 1).bit_length()
    fz = np.fft.rfft(z, size, axis=0)
    fm = np.fft.rfft(mask, size, axis=0)
    acov = np.fft.irfft(fz * np.conj(fz), size, axis=0)[: max_lag + 1]
    cnt = np.fft.irfft(fm * np.conj(fm), size, axis=0)[: max_lag + 1]
    cnt = np.maximum(np.round(cnt), 1.0)
    acov = acov / cnt
    var = np.where(acov[0] > 0, acov[0], 1.0)
    rho = (acov[1:] / var).T
    return np.clip(rho, -1.0, 1.0)


def variance_factor(rho: FloatArray) -> FloatArray:
    """``1 + 2 sum_k w_k rho_a(k) rho_b(k)`` for all pairs (series x series)."""
    m = rho.shape[1]
    k = np.arange(1, m + 1)
    w = 0.5 * (1 + np.cos(np.pi * k / (m + 1)))
    f: FloatArray = np.maximum(1 + 2 * (rho * w) @ rho.T, 0.05)
    return f


@dataclass(slots=True)
class Null:
    """Sample of the null distribution of max |z| over the tested lags."""

    values: FloatArray  # sorted ascending
    threshold: float
    xi: float
    sigma: float

    @property
    def size(self) -> int:
        return len(self.values)

    def pvalues(self, stat: FloatArray) -> FloatArray:
        stat = np.asarray(stat, dtype=np.float64)
        n = len(self.values)
        if n == 0:
            return np.ones_like(stat)
        exceed = n - np.searchsorted(self.values, stat, side="left")
        empirical = (1.0 + exceed) / (1.0 + n)
        tail_share = 1.0 - TAIL_QUANTILE
        y = np.maximum(stat - self.threshold, 0.0)
        if abs(self.xi) < 1e-6:
            surv = np.exp(-y / self.sigma)
        else:
            base = np.maximum(1 + self.xi * y / self.sigma, 0.0)
            with np.errstate(divide="ignore"):
                surv = np.where(base > 0, base ** (-1 / self.xi), 0.0)
        tail = tail_share * surv
        use_tail = (exceed < MIN_EXCEEDANCES) & (stat > self.threshold)
        p = np.where(use_tail, tail, empirical)
        return np.clip(p, 1e-300, 1.0)


def fit_gpd(exceedances: FloatArray) -> tuple[float, float]:
    """Shape and scale of a generalised Pareto distribution by probability weighted
    moments (Hosking & Wallis 1987); shape clipped to [-0.5, 0.5]."""
    y = np.sort(exceedances)
    n = len(y)
    if n < 5 or float(y.mean()) <= 0:
        return 0.0, max(float(y.mean()) if n else 1.0, 1e-6)
    i = np.arange(1, n + 1)
    a0 = float(y.mean())
    a1 = float(np.mean(y * (n - i) / (n - 1)))
    denom = a0 - 2 * a1
    if denom <= 0:
        return 0.0, a0
    k = a0 / denom - 2
    sigma = 2 * a0 * a1 / denom
    xi = float(np.clip(-k, -0.5, 0.5))
    return xi, max(sigma, 1e-6)


def circular_null(
    z: FloatArray,
    mask: FloatArray,
    factor: FloatArray,
    max_lag: int,
    rng: np.random.Generator,
    *,
    pairs: int = NULL_PAIRS,
    shifts: int = NULL_SHIFTS,
) -> Null:
    """Null distribution of max |z| over lags -max_lag..max_lag from circularly
    shifted pairs (see the module docstring)."""
    t_len, n = z.shape
    days = t_len // 24
    offsets_all = np.arange(MIN_SHIFT_DAYS, days - MIN_SHIFT_DAYS + 1) * 24
    if n < 2 or len(offsets_all) == 0:
        return Null(np.array([]), math.inf, 0.0, 1.0)
    total = n * (n - 1)
    if total <= pairs:
        ii, jj = np.nonzero(~np.eye(n, dtype=bool))
    else:
        ii = rng.integers(0, n, pairs)
        jj = rng.integers(0, n - 1, pairs)
        jj = jj + (jj >= ii)
    offsets = rng.choice(offsets_all, size=(len(ii), min(shifts, len(offsets_all))))
    zf = np.fft.rfft(z.astype(np.float32), axis=0)
    norm = np.sqrt((z**2).sum(axis=0))
    norm[norm == 0] = 1.0
    known = mask.sum(axis=0)
    lags = np.arange(-max_lag, max_lag + 1)
    out: list[FloatArray] = []
    for start in range(0, len(ii), BLOCK):
        a, b = ii[start : start + BLOCK], jj[start : start + BLOCK]
        # c[t] = sum_s z_a(s) z_b(s + t): b shifted back by t against a.
        cc = np.fft.irfft(np.conj(zf[:, a]) * zf[:, b], t_len, axis=0)
        r = cc / (norm[a] * norm[b])
        n_ab = np.minimum(known[a], known[b])
        sd = np.sqrt(factor[a, b] / np.maximum(n_ab, 2.0))
        off = offsets[start : start + BLOCK]
        idx = (off[:, :, None] + lags[None, None, :]) % t_len  # pairs x shifts x lags
        vals = np.take_along_axis(r.T[:, None, :], idx, axis=2)
        stat = np.abs(vals).max(axis=2) / sd[:, None]
        out.append(stat.ravel().astype(np.float64))
    values = np.sort(np.concatenate(out))
    threshold = float(np.quantile(values, TAIL_QUANTILE))
    xi, sigma = fit_gpd(values[values > threshold] - threshold)
    return Null(values, threshold, xi, sigma)


def lagged_r(z: FloatArray, a: IntArray, b: IntArray, lag: IntArray) -> FloatArray:
    """corr(z_a(t), z_b(t + lag)) for each triple (zero-filled residuals)."""
    t_len = z.shape[0]
    out = np.empty(len(a))
    for k in range(len(a)):
        lg = int(lag[k])
        if lg >= 0:
            x, y = z[: t_len - lg, a[k]], z[lg:, b[k]]
        else:
            x, y = z[-lg:, a[k]], z[: t_len + lg, b[k]]
        den = math.sqrt(float(x @ x) * float(y @ y))
        out[k] = float(x @ y) / den if den > 0 else 0.0
    return out


def remove_factors(z: FloatArray, max_factors: int = MAX_FACTORS) -> tuple[FloatArray, int]:
    """Residuals without their principal components above the Marchenko-Pastur edge."""
    t_len, n = z.shape
    if n < 10 or max_factors <= 0:
        return z, 0
    norm = np.sqrt((z**2).sum(axis=0) / t_len)
    norm[norm == 0] = 1.0
    x = z / norm
    cov = (x.T @ x) / t_len
    eigval, eigvec = np.linalg.eigh(cov)
    order = np.argsort(eigval)[::-1]
    edge = (1 + math.sqrt(n / t_len)) ** 2
    k = int(min(max_factors, int((eigval[order] > edge).sum())))
    if k == 0:
        return z, 0
    u = x @ eigvec[:, order[:k]]
    u, _ = np.linalg.qr(u)
    return z - u @ (u.T @ z), k


def unstable(
    z: FloatArray,
    mask: FloatArray,
    *,
    a: IntArray,
    b: IntArray,
    lag: IntArray,
    r: FloatArray,
) -> npt.NDArray[np.bool_]:
    """Relations that do not replicate in both halves of the window (sign, |r|)."""
    t_len = z.shape[0]
    out = np.zeros(len(a), dtype=bool)
    if t_len < UNSTABLE_MIN_DAYS * 24:
        return out
    half = t_len // 2
    for part in (slice(0, half), slice(half, t_len)):
        zp = z[part]
        zp = zp - (zp.sum(axis=0) / np.maximum(mask[part].sum(axis=0), 1)) * mask[part]
        rp = lagged_r(zp, a, b, lag)
        out |= (np.sign(rp) != np.sign(r)) | (np.abs(rp) < UNSTABLE_MIN_R)
    return out
