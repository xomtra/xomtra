import math

import numpy as np
import pytest

from hacore.public import ratings as r

NAN = math.nan


def test_alpha_ordinal_matches_reference_values() -> None:
    # Values computed with the `krippendorff` package (ordinal, value domain 1-4).
    rng = np.random.default_rng(1)
    expected = [0.037207, 0.357686, 0.062684]
    for want in expected:
        d = rng.integers(1, 5, (40, 3)).astype(float)
        d[rng.random(d.shape) < 0.15] = np.nan
        d[:20, 1] = d[:20, 0]
        assert r.krippendorff_alpha_ordinal(d) == pytest.approx(want, abs=1e-6)


def test_alpha_extremes() -> None:
    same = np.array([[1, 1, 1], [2, 2, 2], [4, 4, 4], [3, 3, NAN]])
    assert r.krippendorff_alpha_ordinal(same) == pytest.approx(1.0)
    assert math.isnan(r.krippendorff_alpha_ordinal([[1, NAN], [2, NAN]]))
    # one value only: no variation to agree on
    assert math.isnan(r.krippendorff_alpha_ordinal([[2, 2], [2, 2]]))


def test_weighted_kappa() -> None:
    assert r.weighted_kappa([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    near = r.weighted_kappa([1, 2, 3, 4, 2], [2, 2, 3, 4, 1])
    far = r.weighted_kappa([1, 2, 3, 4, 2], [4, 2, 3, 1, 1])
    assert 0 < near < 1
    assert far < near
    assert r.weighted_kappa([1, NAN], [1, 3]) != r.weighted_kappa([1, 2], [1, 3])


def test_ndcg_and_precision() -> None:
    assert r.ndcg_at_k([4, 3, 2, 1]) == pytest.approx(1.0)
    assert r.ndcg_at_k([1, 2, 3, 4]) < 1
    assert math.isnan(r.ndcg_at_k([1, 1, 1]))
    assert r.precision_at_k([4, 3, 2, 1]) == 0.5
    assert r.precision_at_k([3] * 12 + [1]) == 1.0
    assert r.precision_at_k([1] * 10 + [4]) == 0.0  # beyond k does not count


def test_cluster_bootstrap_keeps_households_together() -> None:
    values = np.array([1.0, 1.0, 5.0, 5.0])
    houses = ["a", "a", "b", "b"]
    point, lo, hi = r.cluster_bootstrap(houses, lambda i: float(values[i].mean()), n_boot=300)
    assert point == 3.0
    assert lo == pytest.approx(1.0)
    assert hi == pytest.approx(5.0)


def test_power_simulation_runs() -> None:
    res = r.simulate_alpha_power(60, households=6, simulations=3, n_boot=50)
    assert 0 < res.alpha_mean < 1
    assert res.half_width_mean > 0
    assert 0 <= res.lower_bound_above_060 <= 1
