"""Linear wave theory, the v2 nearshore model and the uncertainty model."""

from __future__ import annotations

import math

import pytest

from app.services.surf_quality import nearshore as ns
from app.services.surf_quality.uncertainty import (
    LEAD_TABLE,
    SIGMA_FLOOR,
    SIGMA_TRANSLATION_UNCALIBRATED,
    EnsembleInfo,
    breaking_uncertainty,
    confidence_index,
    lead_sigma,
)

PROFILE = ns.Profile(shore_normal_deg=270.0, swell_window_min=200.0, swell_window_max=340.0)


def _bisect_wavenumber(period: float, depth: float) -> float:
    """Reference solve of ω² = g k tanh(k h) by bisection to full double precision."""
    omega2 = (2 * math.pi / period) ** 2
    lo, hi = 1e-9, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if ns.G * mid * math.tanh(mid * depth) < omega2:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class TestLinearTheory:
    @pytest.mark.parametrize("period", [5.0, 9.0, 14.0, 20.0, 25.0])
    @pytest.mark.parametrize("depth", [0.5, 3.0, 12.0, 50.0, 200.0, 2000.0])
    def test_wavenumber_matches_high_precision_solve(self, period: float, depth: float) -> None:
        k = ns.wavenumber(period, depth)
        assert k == pytest.approx(_bisect_wavenumber(period, depth), rel=1e-10)

    def test_deep_and_shallow_limits(self) -> None:
        t = 12.0
        assert ns.shoaling_coefficient(t, 5000.0) == 1.0
        # shallow water: Cg -> sqrt(g h), so Ks -> sqrt(Cg0 / sqrt(g h))
        h = 0.05
        expected = math.sqrt(ns.deep_water_group_velocity(t) / math.sqrt(ns.G * h))
        assert ns.shoaling_coefficient(t, h) == pytest.approx(expected, rel=1e-3)

    @pytest.mark.parametrize(
        ("period", "depth", "ks"),
        # values computed in the research notes from the same linear-theory formulas
        [(14.0, 50.0, 0.913), (18.0, 100.0, 0.918), (22.0, 100.0, 0.916)],
    )
    def test_shoaling_coefficients_from_research(
        self, period: float, depth: float, ks: float
    ) -> None:
        assert ns.shoaling_coefficient(period, depth) == pytest.approx(ks, abs=0.001)

    def test_deshoaling_raises_height_over_intermediate_depth(self) -> None:
        h0 = ns.deep_water_height(2.0, 18.0, 100.0)
        assert h0 == pytest.approx(2.0 / 0.918, rel=2e-3)
        assert ns.deep_water_height(2.0, 18.0, None) == 2.0

    @pytest.mark.parametrize(
        ("h0", "t", "hb"), [(1.0, 12.0, 1.66), (2.0, 16.0, 3.25), (4.0, 18.0, 5.93)]
    )
    def test_komar_gaughan_reference_values(self, h0: float, t: float, hb: float) -> None:
        assert ns.komar_gaughan_m(h0, t) == pytest.approx(hb, abs=0.01)


class TestDirectionalFactor:
    def test_refraction_follows_cos_power_law(self) -> None:
        """Hb(α0)/Hb(0) ≈ cos(α0)^0.4 for a narrow spectrum (research notes check)."""
        base = ns.estimate([ns.Partition("s", 2.0, 14.0, 270.0)], _narrow()).breaking_m
        for alpha in (20.0, 40.0, 60.0):
            hb = ns.estimate([ns.Partition("s", 2.0, 14.0, 270.0 + alpha)], _narrow()).breaking_m
            assert hb / base == pytest.approx(math.cos(math.radians(alpha)) ** 0.4, rel=0.01)

    def test_spreading_and_shadow(self) -> None:
        r_long = ns.directional_factor(270.0, ns.spreading_sigma_deg(16.0, False), PROFILE)
        r_sea = ns.directional_factor(270.0, ns.spreading_sigma_deg(6.0, True), PROFILE)
        assert 0.97 < r_long < 1.0 and r_sea < r_long  # wider spread, more oblique energy
        # swell from behind the coast is fully blocked
        assert ns.directional_factor(90.0, 10.0, PROFILE) == 0.0
        # moving out of the window (edge at 340) energy falls steadily but smoothly: with a
        # 10° spread part of a swell centred just outside still arrives through the window
        r = [ns.directional_factor(d, 10.0, PROFILE) for d in (330.0, 340.0, 350.0, 360.0)]
        assert r == sorted(r, reverse=True) and r[-1] > 0.0
        assert r[2] < 0.5 * r[0] and r[3] < 0.1 * r[0]

    def test_shadow_table_is_used_and_interpolated(self) -> None:
        table = tuple(1.0 if 250 <= 10 * i <= 290 else 0.0 for i in range(36))
        p = ns.Profile(270.0, 0.0, 359.0, shadow_table=table)
        assert p.transmission(270.0) == 1.0
        assert p.transmission(295.0) == pytest.approx(0.5)
        assert p.transmission(320.0) == 0.0

    def test_unknown_direction_is_conservative(self) -> None:
        known = ns.estimate([ns.Partition("s", 2.0, 14.0, 270.0)], PROFILE).breaking_m
        unknown = ns.estimate([ns.Partition("s", 2.0, 14.0, None)], PROFILE).breaking_m
        assert unknown < known


def _narrow() -> ns.Profile:
    return ns.Profile(270.0, 180.0, 359.0, spreading_deg=0.5)


class TestEstimate:
    def test_flux_combination_of_identical_partitions(self) -> None:
        one = ns.estimate([ns.Partition("a", 1.5, 13.0, 270.0)], PROFILE).breaking_m
        two = ns.estimate(
            [ns.Partition("a", 1.5, 13.0, 270.0), ns.Partition("b", 1.5, 13.0, 270.0)], PROFILE
        ).breaking_m
        assert two == pytest.approx(one * 2**0.4, rel=1e-9)
        assert ns.combine_flux([]) == 0.0

    def test_focusing_grows_with_period_only_when_kappa_set(self) -> None:
        flat = ns.Profile(270.0, 200.0, 340.0, focusing_ref=1.2)
        assert flat.focusing(18.0) == flat.focusing(10.0) == 1.2
        canyon = ns.Profile(270.0, 200.0, 340.0, focusing_ref=1.2, focusing_kappa=0.5)
        assert canyon.focusing(10.0) == 1.2
        assert canyon.focusing(18.0) == pytest.approx(1.2 * (1 + 0.5 * 6 / 12))

    def test_depth_limit_applies_only_with_documented_depth(self) -> None:
        big = [ns.Partition("s", 4.0, 16.0, 270.0)]
        free = ns.estimate(big, PROFILE)
        capped = ns.estimate(
            big, ns.Profile(270.0, 200.0, 340.0, controlling_depth_m=3.0, breaker_index=0.55)
        )
        assert not free.depth_limited and capped.depth_limited
        assert capped.breaking_m == pytest.approx(0.55 * 3.0)
        assert capped.sets_m == pytest.approx(capped.breaking_m * ns.SETS_RATIO)

    def test_assumptions_are_reported(self) -> None:
        est = ns.estimate([ns.Partition("s", 1.0, 12.0, 270.0)], PROFILE)
        assert "forecast point assumed to be in deep water" in est.assumptions
        measured = ns.Profile(
            270.0,
            200.0,
            340.0,
            forecast_point_depth_m=80.0,
            data_quality={"shore_normal_deg": "measured", "focusing_ref": "calibrated"},
        )
        assert ns.estimate([ns.Partition("s", 1.0, 12.0, 270.0)], measured).assumptions == [
            "shadowing from the swell window (no shadow table)"
        ]

    def test_spectral_form_reduces_to_partition_form_for_a_delta_spectrum(self) -> None:
        """X = Hs² · Tp for a narrow spectrum at normal incidence (research unit test)."""
        import numpy as np

        hs, tp = 2.0, 14.0
        freqs = np.array([1 / 16, 1 / 14, 1 / 12])
        dirs = np.arange(0.0, 360.0, 10.0)
        df = np.full(3, 0.01)
        dtheta = 2 * math.pi / dirs.size
        energy = np.zeros((3, dirs.size))
        energy[1, 27] = (hs / 4) ** 2 / (df[1] * dtheta)  # all energy at Tp from 270° (= β)
        hb = ns.spectral_breaking_height(energy, freqs, dirs, PROFILE, df=df, dtheta_rad=dtheta)
        assert hb == pytest.approx(ns.komar_gaughan_m(hs, tp), rel=1e-9)
        # energy from behind the coast contributes nothing
        energy_back = np.zeros_like(energy)
        energy_back[1, 9] = energy[1, 27]
        assert ns.spectral_breaking_height(energy_back, freqs, dirs, PROFILE, df=df) == 0.0

    def test_inversion_round_trip(self) -> None:
        h0 = ns.deep_water_height_for_breaking(3.0, 15.0, 265.0, PROFILE)
        got = ns.estimate([ns.Partition("s", h0, 15.0, 265.0)], PROFILE).breaking_m
        assert got == pytest.approx(3.0, rel=1e-6)


class TestUncertainty:
    def test_lead_table_interpolation(self) -> None:
        assert lead_sigma(0) == LEAD_TABLE[0][1]
        assert lead_sigma(72) == pytest.approx(0.192)
        assert lead_sigma(96) == pytest.approx((0.192 + 0.276) / 2)
        values = [lead_sigma(h) for h in range(0, 16 * 24, 6)]
        assert values == sorted(values)
        assert lead_sigma(10_000) <= 0.8

    def test_range_widens_with_lead_and_is_lognormal(self) -> None:
        d1 = breaking_uncertainty(6.0, 24)
        d7 = breaking_uncertainty(6.0, 168)
        assert d1.p10_ft < 6.0 < d1.p90_ft
        assert (d7.p90_ft - d7.p10_ft) > (d1.p90_ft - d1.p10_ft)
        assert d1.p10_ft * d1.p90_ft == pytest.approx(36.0, rel=0.03)  # symmetric in log space
        assert d1.kind == "heuristic" and d1.source == "lead_table"
        # an uncalibrated translation dominates at short leads
        assert d1.sigma_translation == SIGMA_TRANSLATION_UNCALIBRATED
        assert d1.sigma_offshore == pytest.approx(math.hypot(0.080, SIGMA_FLOOR))

    def test_ensemble_spread_and_disagreement(self) -> None:
        calm = breaking_uncertainty(6.0, 168, ensemble=EnsembleInfo(2.0, 0.1))
        stormy = breaking_uncertainty(6.0, 168, ensemble=EnsembleInfo(2.0, 0.8))
        assert calm.kind == stormy.kind == "ensemble_heuristic"
        assert calm.sigma_forecast == pytest.approx(1.05 * 0.05)
        assert stormy.p90_ft > calm.p90_ft
        disagree = breaking_uncertainty(6.0, 24, between_model_sigma=0.4)
        assert disagree.source == "model_disagreement" and disagree.sigma_forecast == 0.4
        # a flat sea makes spread/mean meaningless: keep the lead table
        flat = breaking_uncertainty(1.0, 24, ensemble=EnsembleInfo(0.02, 0.01))
        assert flat.source == "lead_table"

    def test_only_validated_calibration_is_calibrated(self) -> None:
        assert breaking_uncertainty(6.0, 24, calibration={"sigma_ln": 0.1}).kind == "heuristic"
        cal = breaking_uncertainty(6.0, 24, calibration={"sigma_ln": 0.1, "validated": True})
        assert cal.kind == "calibrated" and cal.sigma_translation == 0.1

    def test_confidence_index(self) -> None:
        day1 = confidence_index(lead_sigma(24), 1.0, False)
        day5 = confidence_index(lead_sigma(120), 1.0, False)
        day7 = confidence_index(lead_sigma(168), 1.0, False)
        # high for ~2 days, moderate to ~day 6, low beyond (skill is lost between days 4-10)
        assert day1 >= 70 > day5 >= 45 > day7
        assert confidence_index(lead_sigma(24), 1.0, True) < day1
        assert confidence_index(lead_sigma(24), 0.4, False) < day1
        assert 0 <= confidence_index(5.0, 0.0, True) <= 100
