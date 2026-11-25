"""Metrics of the usefulness evaluation (methodology 8.7-8.8, preregistered in
``docs/evaluation/usefulness-prereg.md``).

* agreement of raters: Krippendorff's alpha with the ordinal metric (primary; three
  raters, an ordinal scale, missing ratings) and quadratic-weighted Cohen's kappa
  (secondary, comparable with the literature);
* ranking quality per household: NDCG@10 with gain 2^(label-1) - 1 and precision@10
  (relevant = label >= 3);
* intervals: percentile bootstrap over households (cluster bootstrap: findings of one
  household stay together);
* the power simulation that fixed the size of the human sample.

Nothing here reads data: callers pass arrays of ratings.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]

SCALE = (1, 2, 3, 4)
RELEVANT = 3
K = 10


def krippendorff_alpha_ordinal(ratings: npt.ArrayLike, scale: Sequence[int] = SCALE) -> float:
    """Alpha of ``ratings`` (units x raters, NaN = missing) with the ordinal metric.

    Units rated by fewer than two raters are left out (they hold no pairable values).
    """
    data = np.asarray(ratings, dtype=float)
    values = list(scale)
    index = {v: i for i, v in enumerate(values)}
    m = len(values)
    coincidence = np.zeros((m, m))
    for row in data:
        known = [index[int(v)] for v in row if not math.isnan(v)]
        n_u = len(known)
        if n_u < 2:
            continue
        counts = np.bincount(known, minlength=m).astype(float)
        pairs = np.outer(counts, counts) - np.diag(counts)
        coincidence += pairs / (n_u - 1)
    n_c = coincidence.sum(axis=1)
    n = n_c.sum()
    if n <= 1:
        return math.nan
    cum = np.cumsum(n_c)
    delta = np.zeros((m, m))
    for c in range(m):
        for k in range(c, m):
            d = cum[k] - (cum[c - 1] if c else 0.0) - (n_c[c] + n_c[k]) / 2
            delta[c, k] = delta[k, c] = d * d
    observed = float((coincidence * delta).sum())
    expected = float((np.outer(n_c, n_c) * delta).sum()) / (n - 1)
    return math.nan if expected == 0 else 1 - observed / expected


def weighted_kappa(a: npt.ArrayLike, b: npt.ArrayLike, scale: Sequence[int] = SCALE) -> float:
    """Cohen's kappa with quadratic weights of two raters (pairs with a NaN dropped)."""
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = ~(np.isnan(x) | np.isnan(y))
    values = list(scale)
    index = {v: i for i, v in enumerate(values)}
    m = len(values)
    table = np.zeros((m, m))
    for u, v in zip(x[ok], y[ok], strict=True):
        table[index[int(u)], index[int(v)]] += 1
    total = table.sum()
    if total == 0:
        return math.nan
    i, j = np.indices((m, m))
    w = (i - j) ** 2 / (m - 1) ** 2
    expected = np.outer(table.sum(axis=1), table.sum(axis=0)) / total
    denominator = float((w * expected).sum())
    return math.nan if denominator == 0 else 1 - float((w * table).sum()) / denominator


def ndcg_at_k(labels_in_order: Sequence[float], k: int = K) -> float:
    """NDCG@k of one ranked list of labels 1-4 (gain 2^(label-1) - 1); NaN when no
    item has a positive gain."""
    labels = np.asarray(labels_in_order, dtype=float)
    gains = 2.0 ** (labels - 1) - 1
    discounts = 1 / np.log2(np.arange(2, k + 2))
    ideal = float((np.sort(gains)[::-1][:k] * discounts[: min(k, len(gains))]).sum())
    if ideal == 0:
        return math.nan
    return float((gains[:k] * discounts[: min(k, len(gains))]).sum()) / ideal


def precision_at_k(labels_in_order: Sequence[float], k: int = K) -> float:
    """Share of relevant items (label >= 3) among the first k (or all, if fewer)."""
    top = np.asarray(labels_in_order, dtype=float)[:k]
    return math.nan if not len(top) else float((top >= RELEVANT).mean())


def cluster_bootstrap(
    clusters: Sequence[object],
    statistic: Callable[[npt.NDArray[np.int64]], float],
    *,
    n_boot: int = 2000,
    level: float = 0.95,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Point estimate and percentile interval of ``statistic`` (a function of the row
    indices it may use), resampling whole clusters (households) with replacement."""
    labels = np.asarray([str(c) for c in clusters])
    groups = [np.flatnonzero(labels == c) for c in np.unique(labels)]
    rng = np.random.default_rng(seed)
    point = statistic(np.arange(len(labels)))
    stats = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(groups), len(groups))
        value = statistic(np.concatenate([groups[i] for i in pick]))
        if not math.isnan(value):
            stats.append(value)
    if not stats:
        return point, math.nan, math.nan
    tail = (1 - level) / 2 * 100
    lo, hi = np.percentile(stats, [tail, 100 - tail])
    return point, float(lo), float(hi)


# ---- power simulation (methodology 8.4) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerResult:
    n_findings: int
    households: int
    alpha_mean: float
    half_width_mean: float
    lower_bound_above_060: float  # share of simulations whose lower bound is >= 0.60


def simulate_alpha_power(
    n_findings: int,
    *,
    households: int = 20,
    true_noise: float = 0.55,
    household_sd: float = 0.3,
    raters: int = 3,
    simulations: int = 200,
    n_boot: int = 500,
    seed: int = 0,
) -> PowerResult:
    """Interval of alpha(AI, human consensus) for a human sample of ``n_findings``.

    Each finding has a latent usefulness (normal, with a household effect); every human
    rater and the AI see it with independent normal noise ``true_noise`` and round it to
    the scale 1-4 by fixed cut points. The consensus is the median of the human raters.
    """
    rng = np.random.default_rng(seed)
    cuts = np.array([-0.8, 0.0, 0.8])
    alphas, widths, passed = [], [], 0
    for s in range(simulations):
        house = rng.integers(0, households, n_findings)
        effect = rng.normal(scale=household_sd, size=households)[house]
        latent = rng.normal(size=n_findings) + effect

        def rate(scale: float, latent: FloatArray = latent) -> FloatArray:
            noisy = latent + rng.normal(scale=scale, size=len(latent))
            return np.searchsorted(cuts, noisy).astype(float) + 1

        humans = np.column_stack([rate(true_noise) for _ in range(raters)])
        consensus = np.round(np.median(humans, axis=1))
        ai = rate(true_noise)
        pair = np.column_stack([ai, consensus])

        def alpha(idx: npt.NDArray[np.int64], pair: FloatArray = pair) -> float:
            return krippendorff_alpha_ordinal(pair[idx])

        point, lo, hi = cluster_bootstrap(
            house.tolist(),
            alpha,
            n_boot=n_boot,
            seed=seed + s,
        )
        alphas.append(point)
        widths.append((hi - lo) / 2)
        passed += int(lo >= 0.60)
    return PowerResult(
        n_findings,
        households,
        float(np.mean(alphas)),
        float(np.mean(widths)),
        passed / simulations,
    )


@dataclass(frozen=True, slots=True)
class PoolPowerResult:
    households: int
    model_sd: float
    difference_mean: float  # mean NDCG@10(model) - NDCG@10(R0) on the human pool
    passed: float  # share of simulations meeting the non-inferiority condition (b)


def simulate_pool_power(
    households: int,
    *,
    model_sd: float,
    r0_sd: float = 1.0,
    per_household: int = 50,
    true_noise: float = 0.55,
    raters: int = 3,
    margin: float = -0.05,
    simulations: int = 200,
    n_boot: int = 500,
    seed: int = 0,
) -> PoolPowerResult:
    """Power of the human pool condition (prereg section 6 (b), ADR 069).

    Each household has ``per_household`` findings with a latent usefulness (standard
    normal). R0 and the model rank them by the latent value plus normal noise (``r0_sd``,
    ``model_sd``; a smaller noise is a better ranking). The pool is the union of the top
    10 of the model, R0 and a random order; three raters rate the pool as in
    ``simulate_alpha_power`` and the consensus is their median. NDCG@10 is computed with
    the ideal DCG of the pool. Condition (b): the mean paired difference over households
    is >= 0 and the lower bound of its cluster-bootstrap interval is > ``margin``.
    """
    rng = np.random.default_rng(seed)
    cuts = np.array([-0.8, 0.0, 0.8])
    discounts = 1 / np.log2(np.arange(2, K + 2))
    means, passed = [], 0
    for s in range(simulations):
        diffs = []
        for _ in range(households):
            latent = rng.normal(size=per_household)

            def top(noise: float, latent: FloatArray = latent) -> npt.NDArray[np.int64]:
                score = latent + rng.normal(scale=noise, size=len(latent))
                return np.argsort(-score)[:K]

            r0, model = top(r0_sd), top(model_sd)
            pool = np.union1d(np.union1d(r0, model), rng.permutation(per_household)[:K])
            ratings = np.column_stack(
                [
                    np.searchsorted(cuts, latent + rng.normal(scale=true_noise, size=len(latent)))
                    + 1
                    for _ in range(raters)
                ]
            )
            gains: FloatArray = np.asarray(2.0 ** (np.round(np.median(ratings, axis=1)) - 1) - 1)
            ideal = float((np.sort(gains[pool])[::-1][:K] * discounts).sum())
            if ideal == 0:
                continue
            diffs.append(float(((gains[model] - gains[r0]) * discounts).sum()) / ideal)
        d = np.array(diffs)
        means.append(float(d.mean()))

        def mean(idx: npt.NDArray[np.int64], d: FloatArray = d) -> float:
            return float(d[idx].mean())

        point, lo, _ = cluster_bootstrap(list(range(len(d))), mean, n_boot=n_boot, seed=seed + s)
        passed += int(point >= 0 and lo > margin)
    return PoolPowerResult(households, model_sd, float(np.mean(means)), passed / simulations)
