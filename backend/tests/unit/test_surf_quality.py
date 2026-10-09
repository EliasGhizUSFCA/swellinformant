import pytest

from app.models.enums import ConfidenceLabel, QualityLabel
from app.services.surf_quality.breaking import (
    METHOD_APPROX,
    METHOD_CALIBRATED,
    estimate_breaking_height,
    exposure_factor,
    komar_gaughan_m,
)
from app.services.surf_quality.confidence import (
    completeness,
    confidence_label,
    confidence_score,
    run_agreement,
)
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import (
    evaluate,
    label_for_score,
    min_score_for_label,
    score_period,
    score_size,
    score_tide,
    score_wind,
    wind_relation,
)

JBAY = SpotParams(
    slug="jbay",
    latitude=-34.03,
    longitude=24.93,
    swell_window_min=180,
    swell_window_max=250,
    optimal_swell_direction_min=200,
    optimal_swell_direction_max=230,
    offshore_wind_direction=290,
    min_swell_period_s=11,
    ideal_swell_period_s=15,
    wave_height_min_ft=4,
    wave_height_max_ft=12,
    height_factor=0.9,
    tide_preference="all",
)


def sea(**kw: float) -> SeaState:
    base = {
        "primary_swell_height_m": 2.0,
        "primary_swell_period_s": 15.0,
        "primary_swell_direction_deg": 215.0,
        "wind_speed_kmh": 8.0,
        "wind_direction_deg": 290.0,
    }
    base.update(kw)
    return SeaState(**base)  # type: ignore[arg-type]


class TestBreakingHeight:
    def test_komar_gaughan_reference_value(self) -> None:
        # Hb = 0.39 g^0.2 (T H0^2)^0.4 ; H0 = 2 m, T = 14 s → ≈ 3.08 m
        assert komar_gaughan_m(2.0, 14.0) == pytest.approx(3.08, abs=0.02)
        assert komar_gaughan_m(0, 10) == 0.0

    def test_longer_period_gives_bigger_surf(self) -> None:
        assert komar_gaughan_m(2.0, 18.0) > komar_gaughan_m(2.0, 10.0)

    def test_breaking_differs_from_offshore_significant_height(self) -> None:
        est = estimate_breaking_height(
            sea(primary_swell_height_m=2.0, primary_swell_period_s=16.0), JBAY
        )
        offshore_ft = 2.0 / 0.3048
        assert est.mid_ft != pytest.approx(offshore_ft, rel=0.05)
        assert est.method == METHOD_APPROX and not est.calibrated
        assert est.min_ft < est.max_ft

    def test_blocked_swell_direction_produces_tiny_surf(self) -> None:
        assert exposure_factor(215, JBAY) == pytest.approx(1.0)
        assert exposure_factor(100, JBAY) < 0.01
        assert exposure_factor(None, JBAY) == 0.6
        est = estimate_breaking_height(sea(primary_swell_direction_deg=100.0), JBAY)
        assert est.max_ft <= 0.5

    def test_calibrated_coefficients_are_used_when_present(self) -> None:
        calibrated = SpotParams(
            **{
                **JBAY.__dict__,
                "calibration": {"a": 3.0, "b": 1.0, "c": 0.0, "n_observations": 400},
            }
        )
        est = estimate_breaking_height(sea(primary_swell_height_m=2.0), calibrated)
        assert est.method == METHOD_CALIBRATED and est.calibrated
        assert est.mid_ft == pytest.approx(6.0)

    def test_falls_back_to_bulk_parameters(self) -> None:
        est = estimate_breaking_height(
            SeaState(sig_wave_height_m=1.5, wave_period_s=12, wave_direction_deg=215), JBAY
        )
        assert est.mid_ft > 0


class TestComponents:
    def test_period_curve(self) -> None:
        assert score_period(7, JBAY) == 0.0
        assert 0 < score_period(10, JBAY) < 0.4
        assert score_period(11, JBAY) == pytest.approx(0.4)
        assert score_period(15, JBAY) == 1.0
        assert score_period(None, JBAY) is None

    def test_size_curve_peaks_inside_working_range(self) -> None:
        assert score_size(1.0, JBAY) == 0.0
        assert score_size(4.0, JBAY) == pytest.approx(0.75)
        assert score_size(9.2, JBAY) == pytest.approx(1.0, abs=0.01)
        assert score_size(30.0, JBAY) == 0.0

    def test_wind(self) -> None:
        assert wind_relation(3, 100, JBAY) == "glassy"
        assert wind_relation(15, 290, JBAY) == "offshore"
        assert wind_relation(15, 110, JBAY) == "onshore"
        assert score_wind(3, 110, JBAY) == pytest.approx(1.0)  # glassy beats direction
        assert score_wind(25, 290, JBAY) == pytest.approx(1.0)
        assert score_wind(25, 110, JBAY) == pytest.approx(0.0)
        assert 0.3 < score_wind(15, 200, JBAY) < 0.8  # cross-shore

    def test_tide(self) -> None:
        assert score_tide(None, "mid") is None
        assert score_tide(0.5, "mid") == 1.0
        assert score_tide(0.0, "high") == pytest.approx(0.2)
        assert score_tide(0.3, "all") == 1.0


class TestScore:
    def test_classic_conditions_score_highly(self) -> None:
        r = evaluate(
            sea(primary_swell_height_m=2.0, primary_swell_period_s=16.0, wind_speed_kmh=10.0), JBAY
        )
        assert r.score >= 85
        assert r.label in ("excellent", "exceptional")
        assert "SW swell" in r.explanation and "faces" in r.explanation

    def test_onshore_wind_is_capped(self) -> None:
        r = evaluate(sea(wind_speed_kmh=30.0, wind_direction_deg=110.0), JBAY)
        assert r.score < 40
        assert r.components["wind_gate"] == 0.5

    def test_small_weak_glassy_surf_is_not_good(self) -> None:
        r = evaluate(
            sea(primary_swell_height_m=0.6, primary_swell_period_s=9.0, wind_speed_kmh=3.0), JBAY
        )
        assert r.score < 40

    def test_maxed_out_surf_scores_zero(self) -> None:
        r = evaluate(sea(primary_swell_height_m=7.0, primary_swell_period_s=18.0), JBAY)
        assert r.score == 0

    def test_missing_inputs_renormalise_weights(self) -> None:
        r = evaluate(
            SeaState(
                primary_swell_height_m=2.0,
                primary_swell_period_s=15,
                primary_swell_direction_deg=215,
            ),
            JBAY,
        )
        assert "wind" not in r.components
        assert sum(r.weights_used.values()) == pytest.approx(1.0, abs=0.01)

    def test_label_thresholds(self) -> None:
        assert label_for_score(96) == QualityLabel.EXCEPTIONAL
        assert label_for_score(85) == QualityLabel.EXCELLENT
        assert label_for_score(72) == QualityLabel.VERY_GOOD
        assert label_for_score(57) == QualityLabel.GOOD
        assert label_for_score(40) == QualityLabel.FAIR
        assert label_for_score(39) == QualityLabel.POOR
        assert min_score_for_label("very_good") == 72


class TestConfidence:
    def test_decays_with_lead_time(self) -> None:
        values = [confidence_score(h, 1.0) for h in (0, 72, 168, 240, 336)]
        assert values == sorted(values, reverse=True)
        assert confidence_label(confidence_score(24, 1.0, 1.0)) == ConfidenceLabel.HIGH
        assert confidence_label(confidence_score(168, 1.0, 0.9)) == ConfidenceLabel.MODERATE

    def test_staleness_incompleteness_and_disagreement_reduce_confidence(self) -> None:
        base = confidence_score(48, 1.0, 1.0)
        assert confidence_score(48, 1.0, 1.0, stale=True) < base
        assert confidence_score(48, 0.4, 1.0) < base
        assert confidence_score(48, 1.0, 0.2) < base

    def test_helpers(self) -> None:
        assert completeness({"primary_swell_height_m": 1, "wind_speed_kmh": 3}) == pytest.approx(
            0.4
        )
        assert run_agreement(2.0, 2.0) == 1.0
        assert run_agreement(2.0, 1.0) == pytest.approx(0.5)
        assert run_agreement(None, 1.0) is None
