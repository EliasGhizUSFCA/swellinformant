"""Verification metrics: hand-computed examples, circular statistics, CRPS, bootstrap.

All numbers below are synthetic test values chosen so the results can be worked out by
hand; none of them is an observation or a forecast.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest

from app.services.verification import metrics as vm

# m = [2, 3, 5], o = [1, 3, 4]: d = m - o = [1, 0, 1]; sum(d^2) = 2, sum(o^2) = 26,
# sum(m*o) = 31; anomalies (m - 10/3) - (o - 8/3) = [1/3, -2/3, 1/3] -> sum of squares 2/3.
MOD = [2.0, 3.0, 5.0]
OBS = [1.0, 3.0, 4.0]


def test_continuous_scores_hand_computed() -> None:
    assert vm.n_pairs(MOD, OBS) == 3
    assert vm.bias(MOD, OBS) == pytest.approx(2 / 3)
    assert vm.mae(MOD, OBS) == pytest.approx(2 / 3)
    assert vm.mse(MOD, OBS) == pytest.approx(2 / 3)
    assert vm.rmse(MOD, OBS) == pytest.approx(math.sqrt(2 / 3))
    assert vm.nrmse(MOD, OBS) == pytest.approx(math.sqrt(2 / 26))
    assert vm.scatter_index(MOD, OBS) == pytest.approx(math.sqrt((2 / 3) / 26))
    assert vm.hanna_heinold(MOD, OBS) == pytest.approx(math.sqrt(2 / 31))
    # sum(dm*do) = 13/3, sum(dm^2) = sum(do^2) = 42/9 -> CC = 117/126
    assert vm.pearson_cc(MOD, OBS) == pytest.approx(117 / 126)
    summary = vm.continuous_scores(MOD, OBS)
    assert summary["n"] == 3 and summary["hh"] == pytest.approx(math.sqrt(2 / 31))


def test_skill_score_vs_reference() -> None:
    ref = [1.0, 1.0, 1.0]  # MSE_ref = (0 + 4 + 9) / 3 = 13/3
    assert vm.skill_score(MOD, OBS, ref) == pytest.approx(1 - (2 / 3) / (13 / 3))
    assert vm.skill_score(OBS, OBS, ref) == pytest.approx(1.0)
    assert vm.skill_score(ref, OBS, ref) == pytest.approx(0.0)
    assert math.isnan(vm.skill_score(MOD, OBS, OBS))  # perfect reference: undefined


def test_non_finite_pairs_are_ignored_and_empty_is_nan() -> None:
    assert vm.rmse([2.0, np.nan, 5.0, 3.0], [1.0, 2.0, 4.0, np.inf]) == pytest.approx(1.0)
    assert math.isnan(vm.rmse([], []))
    assert math.isnan(vm.pearson_cc([1.0], [1.0]))
    assert math.isnan(vm.hanna_heinold([0.0, 0.0], [1.0, 2.0]))  # sum(m*o) = 0
    with pytest.raises(ValueError):
        vm.rmse([1.0, 2.0], [1.0])


def test_rmse_and_si_favour_damped_models_but_hh_does_not() -> None:
    """Mentaschi et al. (2013): shrinking a noisy forecast lowers RMSE; HH can rise."""
    o = np.array([1.0, 1.0])
    m = np.array([2.0, 0.5])
    damped = 0.4 * m
    assert vm.rmse(damped, o) < vm.rmse(m, o)
    assert vm.hanna_heinold(damped, o) > vm.hanna_heinold(m, o)
    assert vm.bias(damped, o) < 0


def test_wrap_and_circular_difference() -> None:
    np.testing.assert_allclose(
        vm.wrap_degrees([180, -180, 540, 190, -190, 360, 0, 179.5]),
        [180, 180, 180, -170, 170, 0, 0, 179.5],
    )
    np.testing.assert_allclose(vm.circular_difference([350, 10, 5], [10, 350, 355]), [-20, 20, 10])


def test_circular_statistics() -> None:
    # Errors -20 and +20 degrees cancel; their RMSE is 20.
    assert vm.circular_bias([350, 10], [10, 350]) == pytest.approx(0.0, abs=1e-9)
    assert vm.circular_rmse([350, 10], [10, 350]) == pytest.approx(20.0)
    assert vm.circular_mae([350, 10], [10, 350]) == pytest.approx(20.0)
    assert vm.circular_bias([5, 15], [355, 5]) == pytest.approx(10.0)
    scores = vm.direction_scores([350, 10], [10, 350])
    assert scores["n"] == 2 and scores["rmse"] == pytest.approx(20.0)


def _numerical_crps(
    cdf: Callable[[np.ndarray], np.ndarray], y: float, lo: float, hi: float, n: int = 200_001
) -> float:
    """CRPS = ∫ (F(x) - 1{x >= y})^2 dx, integrated separately below and above y so the
    trapezoid rule never straddles the step."""
    below = np.linspace(lo, y, n) if y > lo else np.array([lo, lo])
    above = np.linspace(max(y, lo), hi, n)
    return float(np.trapezoid(cdf(below) ** 2, below) + np.trapezoid((1 - cdf(above)) ** 2, above))


def test_crps_normal_closed_form() -> None:
    # Known value: CRPS(N(0, 1), 0) = (sqrt(2) - 1) / sqrt(pi)
    assert vm.crps_normal(0.0, 1.0, 0.0) == pytest.approx((math.sqrt(2) - 1) / math.sqrt(math.pi))
    for mu, sigma, y in [(1.5, 0.4, 2.1), (2.0, 1.3, -0.5), (0.3, 0.05, 0.31)]:

        def cdf(x: np.ndarray, mu: float = mu, sigma: float = sigma) -> np.ndarray:
            return vm.norm_cdf((x - mu) / sigma)

        numeric = _numerical_crps(
            cdf,
            y,
            mu - 12 * sigma - abs(y),
            mu + 12 * sigma + abs(y),
        )
        assert float(vm.crps_normal(mu, sigma, y)) == pytest.approx(numeric, rel=1e-4, abs=1e-7)
    # sigma -> 0 is the absolute error of a point forecast
    assert float(vm.crps_normal(1.0, 0.0, 3.5)) == pytest.approx(2.5)
    np.testing.assert_allclose(vm.crps_normal([0, 0], [1, 1], [0, 0]), [0.2336949] * 2, rtol=1e-6)
    with pytest.raises(ValueError):
        vm.crps_normal(0.0, -1.0, 0.0)


def test_crps_lognormal_closed_form() -> None:
    for mu, sigma, y in [(0.5, 0.4, 1.3), (0.0, 0.25, 0.6), (1.0, 0.8, 9.0)]:

        def cdf(x: np.ndarray, mu: float = mu, sigma: float = sigma) -> np.ndarray:
            out = np.zeros_like(x)
            pos = x > 0
            out[pos] = vm.norm_cdf((np.log(x[pos]) - mu) / sigma)
            return out

        hi = math.exp(mu + 10 * sigma) + y
        numeric = _numerical_crps(cdf, y, 0.0, hi)
        assert float(vm.crps_lognormal(mu, sigma, y)) == pytest.approx(numeric, rel=1e-4)
    # y <= 0: CRPS = -y + 2 exp(mu + s^2/2) (1 - Phi(s / sqrt 2))
    mu, s = 0.2, 0.5
    expected = 0.3 + 2 * math.exp(mu + s * s / 2) * (1 - float(vm.norm_cdf(s / math.sqrt(2))))
    assert float(vm.crps_lognormal(mu, s, -0.3)) == pytest.approx(expected)
    with pytest.raises(ValueError):
        vm.crps_lognormal(0.0, 0.0, 1.0)


def test_interval_coverage_and_width() -> None:
    lower, upper, o = [0.0, 0.0, 0.0], [1.0, 2.0, 3.0], [0.5, 2.5, 3.0]
    assert vm.interval_coverage(lower, upper, o) == pytest.approx(2 / 3)
    assert vm.mean_interval_width(lower, upper) == pytest.approx(2.0)


def test_event_scores() -> None:
    m = [1.0, 3.0, 4.0, 0.5, 2.5]
    o = [2.5, 3.0, 1.0, 0.2, 2.6]
    table = vm.contingency_table(m, o, 2.0)
    assert (table.hits, table.misses, table.false_alarms, table.correct_negatives) == (2, 1, 1, 1)
    scores = vm.event_scores(m, o, 2.0)
    assert scores["pod"] == pytest.approx(2 / 3)
    assert scores["far"] == pytest.approx(1 / 3)
    assert scores["csi"] == pytest.approx(0.5)
    assert scores["frequency_bias"] == pytest.approx(1.0)
    assert scores["base_rate"] == pytest.approx(3 / 5)
    empty = vm.contingency_table([0.1], [0.2], 2.0)
    assert math.isnan(empty.pod) and math.isnan(empty.far) and math.isnan(empty.csi)


@pytest.mark.parametrize(
    ("lead", "bucket"),
    [
        (0, None),
        (17.9, None),
        (18, 24),
        (24, 24),
        (30, 24),
        (31, None),
        (42, 48),
        (54, 48),
        (66, 72),
        (78, 72),
        (79, None),
        (107.9, None),
        (108, 120),
        (132, 120),
        (133, None),
        (156, 168),
        (180, 168),
        (228, 240),
        (252, 240),
        (253, None),
    ],
)
def test_lead_bucket_membership(lead: float, bucket: int | None) -> None:
    assert vm.lead_bucket_array([lead])[0] == (bucket or vm.NO_BUCKET)


def test_lead_bucket_arrays() -> None:
    leads = np.array([24.0, 31.0, 120.0, 250.0, np.nan, 0.0, 2.9, 3.0])
    np.testing.assert_array_equal(
        vm.lead_bucket_array(leads), [24, vm.NO_BUCKET, 120, 240, 0, 0, 0, 0]
    )
    # The pooled bucket holds every forecast lead >= 3 h (analysis-time rows excluded).
    np.testing.assert_array_equal(
        vm.in_lead_bucket(leads, vm.ALL_LEADS_BUCKET),
        [True, True, True, True, False, False, False, True],
    )
    assert vm.bucket_half_width(72) == 6.0 and vm.bucket_half_width(120) == 12.0


def test_bootstrap_resamples_whole_cycles() -> None:
    """Replicates are unions of whole blocks: block sizes 10 and 1 give 2, 11 or 20 rows."""
    blocks = np.array([0] * 10 + [1])
    values = np.arange(11, dtype=float)
    sizes: list[int] = []

    def stat(a: np.ndarray) -> float:
        sizes.append(len(a))
        return float(a.mean())

    ci = vm.cycle_block_bootstrap(stat, blocks, values, n_boot=200, seed=1)
    replicate_sizes = set(sizes[1:])  # sizes[0] is the full-sample estimate
    assert replicate_sizes <= {2, 11, 20}
    assert replicate_sizes == {2, 11, 20}
    assert ci.n_blocks == 2 and ci.available


def test_bootstrap_is_reproducible_and_seeded() -> None:
    rng = np.random.default_rng(7)  # synthetic sample
    cycles = np.repeat(np.arange(40), 12)
    o = rng.gamma(4.0, 0.5, size=cycles.size)
    m = o * 1.1 + rng.normal(0, 0.2, size=cycles.size)
    a = vm.rmse_ci(m, o, cycles, n_boot=300, seed=42)
    b = vm.rmse_ci(m, o, cycles, n_boot=300, seed=42)
    c = vm.rmse_ci(m, o, cycles, n_boot=300, seed=43)
    assert a == b
    assert (a.lower, a.upper) != (c.lower, c.upper)
    assert a.lower < a.estimate < a.upper
    assert a.estimate == pytest.approx(vm.rmse(m, o))
    diff = vm.rmse_difference_ci(o, m, o, cycles, n_boot=300, seed=1)  # perfect vs noisy
    assert diff.upper < 0
    skill = vm.skill_score_ci(o + 0.01, o, m, cycles, n_boot=100)
    assert skill.lower > 0.9


def test_bootstrap_needs_two_blocks() -> None:
    ci = vm.rmse_ci([1.0, 2.0, 3.0], [1.0, 2.5, 2.0], [5, 5, 5])
    assert not ci.available
    assert ci.as_dict()["lower"] is None
    with pytest.raises(ValueError):
        vm.cycle_block_bootstrap(vm.rmse, [1, 2], [1.0, 2.0], [1.0, 2.0], confidence=1.5)
