import numpy as np
import pytest

from hacore import correlations as corr
from hacore import significance as sig

T = 90 * 24


def ar1(rng: np.random.Generator, phi: float, n: int = 1, t: int = T) -> np.ndarray:
    e = rng.standard_normal((t, n))
    x = np.empty_like(e)
    x[0] = e[0]
    for i in range(1, t):
        x[i] = phi * x[i - 1] + np.sqrt(1 - phi * phi) * e[i]
    return x


def test_variance_factor_grows_with_autocorrelation() -> None:
    rng = np.random.default_rng(0)
    x = np.hstack([rng.standard_normal((T, 2)), ar1(rng, 0.9, 2)])
    z, mask = sig.standardise(x)
    f = sig.variance_factor(sig.autocorrelations(z, mask, sig.taper_lags(T)))
    assert f[0, 1] == pytest.approx(1.0, abs=0.15)
    # Bartlett: (1 + phi^2) / (1 - phi^2) ~ 9.5 for two AR(1) with phi 0.9 (tapered).
    assert 5 < f[2, 3] < 11


def test_fit_gpd_on_exponential_tail() -> None:
    y = np.random.default_rng(1).exponential(2.0, 5000)
    xi, sigma = sig.fit_gpd(y)
    assert abs(xi) < 0.1
    assert sigma == pytest.approx(2.0, rel=0.1)


def test_null_is_calibrated_for_independent_smooth_series() -> None:
    """Independent autocorrelated series with a daily cycle: few p-values below 5 %
    (Bonferroni over lags on naive z would flag far more)."""
    rng = np.random.default_rng(2)
    n = 30
    hour = np.arange(T) % 24
    x = ar1(rng, 0.9, n) + 0.5 * np.sin(2 * np.pi * hour / 24)[:, None]
    pairs, stats = corr.lagged_correlations_v2(corr.highpass(x), 24, seed=0)
    assert stats["null_size"] > 1000
    iu = pairs.leader < pairs.follower
    p = pairs.p[iu]
    assert float(np.mean(p < 0.05)) < 0.1
    assert float(np.mean(pairs.q <= 0.01)) == 0.0


def test_pvalues_use_the_tail_beyond_the_sample() -> None:
    values = np.sort(np.random.default_rng(3).exponential(1.0, 2000))
    threshold = float(np.quantile(values, sig.TAIL_QUANTILE))
    xi, sigma = sig.fit_gpd(values[values > threshold] - threshold)
    null = sig.Null(values, threshold, xi, sigma)
    p = null.pvalues(np.array([0.0, threshold, values[-1] + 5, values[-1] + 10]))
    assert p[0] == pytest.approx(1.0)
    assert p[1] == pytest.approx(1 - sig.TAIL_QUANTILE, abs=0.01)
    assert 0 < p[3] < p[2] < 1 / len(values)


def test_remove_factors_takes_out_a_shared_component() -> None:
    rng = np.random.default_rng(4)
    common = rng.standard_normal((T, 1))
    x = 0.7 * common + 0.7 * rng.standard_normal((T, 30))
    z, _ = sig.standardise(x)
    zf, k = sig.remove_factors(z)
    assert k == 1
    assert abs(np.corrcoef(zf[:, 0], zf[:, 1])[0, 1]) < 0.1


def test_unstable_needs_both_halves() -> None:
    rng = np.random.default_rng(5)
    t = 200 * 24
    a = rng.standard_normal(t)
    b = np.where(np.arange(t) < t // 2, a, -a) + 0.5 * rng.standard_normal(t)
    c = a + 0.5 * rng.standard_normal(t)
    z, mask = sig.standardise(np.column_stack([a, b, c]))
    idx = np.array([0, 0])
    out = sig.unstable(
        z, mask, a=idx, b=np.array([1, 2]), lag=np.array([0, 0]), r=np.array([0.1, 0.9])
    )
    assert out.tolist() == [True, False]


def strongest(pairs: corr.Pairs, x: int, y: int) -> tuple[list[str], list[int]]:
    js = [
        j for j in range(len(pairs.r)) if {int(pairs.leader[j]), int(pairs.follower[j])} == {x, y}
    ]
    j = max(js, key=lambda j: abs(pairs.r[j]))
    return pairs.flags[j], pairs.explained_by[j]


def structure(seed: int) -> np.ndarray:
    """Columns: 0 -> 1 -> 2 (chain, lags 2 and 3 h); 3..7 children of a hidden driver;
    8 a hub with direct followers 9..11; 12..19 unrelated."""
    rng = np.random.default_rng(seed)
    noise = ar1(rng, 0.5, 20)
    x = noise.copy()
    src = ar1(rng, 0.5)[:, 0]
    x[:, 0] = src
    x[2:, 1] = 0.8 * x[:-2, 0] + 0.6 * noise[2:, 1]
    x[3:, 2] = 0.8 * x[:-3, 1] + 0.6 * noise[3:, 2]
    hidden = ar1(rng, 0.5)[:, 0]
    for c, w in zip(range(3, 8), (0.8, 0.75, 0.7, 0.65, 0.7), strict=True):
        x[:, c] = w * hidden + np.sqrt(1 - w * w) * noise[:, c]
    for c, w in zip(range(9, 12), (0.7, 0.6, 0.65), strict=True):
        x[:, c] = w * x[:, 8] + np.sqrt(1 - w * w) * noise[:, c]
    return x


def test_chain_hidden_driver_and_hub() -> None:
    pairs, stats = corr.lagged_correlations_v2(structure(6), 24, min_r=0.2, seed=0)
    assert stats["factors_removed"] >= 0
    # Chain: 0 -> 2 is explained by 1.
    flags, explained = strongest(pairs, 0, 2)
    assert "indirect" in flags
    assert explained == [1]
    # The direct links of the chain stay unflagged.
    assert not strongest(pairs, 0, 1)[0]
    assert not strongest(pairs, 1, 2)[0]
    # Five children of one hidden driver: a one-factor model.
    hidden = [strongest(pairs, a, b)[0] for a in range(3, 8) for b in range(a + 1, 8)]
    assert sum("common_factor" in f for f in hidden) >= 0.8 * len(hidden)
    # A hub is the driver itself: its relations are direct.
    for c in range(9, 12):
        assert "common_factor" not in strongest(pairs, 8, c)[0]
    # Followers of the hub are explained by it.
    assert "indirect" in strongest(pairs, 9, 10)[0]
    assert strongest(pairs, 9, 10)[1] == [8]


def test_a_duplicate_explains_nothing() -> None:
    rng = np.random.default_rng(7)
    a = ar1(rng, 0.5)[:, 0]
    b = 0.7 * a + 0.7 * ar1(rng, 0.5)[:, 0]
    x = np.column_stack([a, a * 1.0001 + 1e-3 * rng.standard_normal(T), b])
    x = np.hstack([x, ar1(rng, 0.5, 5)])
    pairs, _ = corr.lagged_correlations_v2(x, 24, seed=0)
    assert not strongest(pairs, 0, 2)[0]
    assert not strongest(pairs, 1, 2)[0]


def test_v2_is_deterministic_per_seed() -> None:
    x = structure(8)
    p1, _ = corr.lagged_correlations_v2(x, 24, seed=3)
    p2, _ = corr.lagged_correlations_v2(x, 24, seed=3)
    np.testing.assert_array_equal(p1.p, p2.p)
    assert p1.flags == p2.flags
