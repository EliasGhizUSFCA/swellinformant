import json
from dataclasses import replace

import pytest

from app.models.enums import ConfidenceLabel, QualityLabel
from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality.breaking import (
    METHOD_CALIBRATED,
    METHOD_PHYSICAL,
    estimate_breaking_height,
)
from app.services.surf_quality.confidence import completeness, confidence_label, run_agreement
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.nearshore import (
    SETS_RATIO,
    deep_water_height_for_breaking,
    komar_gaughan_m,
)
from app.services.surf_quality.scoring import (
    ALGORITHM_VERSION,
    evaluate,
    label_for_score,
    min_score_for_label,
    score_direction,
    score_period,
    score_size,
    score_tide,
    score_wind,
    wind_relation,
)
from app.services.units import ft_to_m

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
        assert est.method == METHOD_PHYSICAL and not est.calibrated
        assert est.min_ft < est.max_ft
        # typical (significant breaking height) to sets (1.27 x)
        assert est.max_ft == pytest.approx(est.mid_ft * SETS_RATIO, abs=0.5)
        assert "shore normal estimated" in est.assumptions

    def test_well_aligned_groundswell_keeps_the_komar_gaughan_size(self) -> None:
        est = estimate_breaking_height(
            sea(primary_swell_height_m=2.0, primary_swell_period_s=16.0), JBAY
        )
        # 9.6 ft is version 1's value for this swell (the same Komar-Gaughan core); v2 only
        # loses the small spreading/refraction factor.
        assert est.mid_ft == pytest.approx(9.6, rel=0.03)

    def test_blocked_swell_direction_produces_tiny_surf(self) -> None:
        est = estimate_breaking_height(sea(primary_swell_direction_deg=100.0), JBAY)
        assert est.max_ft <= 0.5

    def test_calibrated_coefficients_are_used_when_present(self) -> None:
        calibrated = SpotParams(
            **{
                **JBAY.__dict__,
                "calibration": {"a": 3.0, "b": 1.0, "c": 0.0, "d": 0.0, "n_observations": 400},
            }
        )
        est = estimate_breaking_height(sea(primary_swell_height_m=2.0), calibrated)
        assert est.method == METHOD_CALIBRATED and est.calibrated
        assert est.mid_ft == pytest.approx(6.0)
        # the directional exponent (default 0.4) can only reduce a well-aligned swell
        with_dir = SpotParams(**{**JBAY.__dict__, "calibration": {"a": 3.0, "b": 1.0, "c": 0.0}})
        assert estimate_breaking_height(sea(primary_swell_height_m=2.0), with_dir).mid_ft < 6.0

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
        # 2.1.0: continuous in speed (2.0.0 jumped to exactly 1 at 5 km/h and to 0 for any
        # onshore wind of 20 km/h or more).
        assert score_wind(3, 110, JBAY) == pytest.approx(0.986, abs=0.001)
        assert score_wind(0, None, JBAY) == 1.0
        # Moderate offshore is still a full fit; strong offshore no longer is (2.0.0 gave
        # 1.0 up to 35 km/h, and never less than 0.4).
        assert score_wind(25, 290, JBAY) == pytest.approx(1.0)
        assert score_wind(50, 290, JBAY) < 0.5
        assert score_wind(25, 110, JBAY) < 0.15
        assert score_wind(50, 110, JBAY) < score_wind(25, 110, JBAY)
        # Cross-shore: V_e = 0.5 x 15 km/h, so Q = 1 / (1 + (7.5 / 12.5)^3) = 0.822.
        assert score_wind(15, 200, JBAY) == pytest.approx(1 / (1 + 0.6**3), abs=1e-6)
        assert score_wind(None, 200, JBAY) is None

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
        # 2.1.0 has no floor at 0, so the gate approaches 0.5 instead of sitting on it.
        assert 0.5 <= r.components["wind_gate"] < 0.6

    def test_strong_and_cross_shore_wind_on_an_ideal_swell(self) -> None:
        ideal = {"primary_swell_height_m": 2.0, "primary_swell_period_s": 16.0}
        light_off = evaluate(sea(**ideal, wind_speed_kmh=10.0, wind_direction_deg=290.0), JBAY)
        assert light_off.label in ("excellent", "exceptional")
        cross = evaluate(sea(**ideal, wind_speed_kmh=35.0, wind_direction_deg=200.0), JBAY)
        assert cross.wind_relation == "cross-shore"
        assert cross.score < min_score_for_label("good")
        strong_off = evaluate(sea(**ideal, wind_speed_kmh=60.0, wind_direction_deg=290.0), JBAY)
        assert strong_off.wind_relation == "offshore"
        assert strong_off.score < min_score_for_label("very_good")
        at_45 = evaluate(sea(**ideal, wind_speed_kmh=45.0, wind_direction_deg=290.0), JBAY)
        assert at_45.score < min_score_for_label("very_good")
        # Moderate offshore keeps an ideal swell excellent.
        at_25 = evaluate(sea(**ideal, wind_speed_kmh=25.0, wind_direction_deg=290.0), JBAY)
        assert at_25.score >= min_score_for_label("excellent")

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
    def test_labels_and_helpers(self) -> None:
        assert confidence_label(83) == ConfidenceLabel.HIGH
        assert confidence_label(47) == ConfidenceLabel.MODERATE
        assert confidence_label(30) == ConfidenceLabel.LOW
        assert completeness({"primary_swell_height_m": 1, "wind_speed_kmh": 3}) == pytest.approx(
            0.4
        )
        assert run_agreement(2.0, 2.0) == 1.0
        assert run_agreement(2.0, 1.0) == pytest.approx(0.5)
        assert run_agreement(None, 1.0) is None


SPOTS = json.loads((DEFAULT_DATA_DIR / "spots.json").read_text())


def spot_params(raw: dict[str, object]) -> SpotParams:
    fields = (
        "latitude",
        "longitude",
        "swell_window_min",
        "swell_window_max",
        "optimal_swell_direction_min",
        "optimal_swell_direction_max",
        "offshore_wind_direction",
        "min_swell_period_s",
        "ideal_swell_period_s",
        "wave_height_min_ft",
        "wave_height_max_ft",
        "height_factor",
    )
    return SpotParams(slug=str(raw["slug"]), **{f: float(raw[f]) for f in fields})  # type: ignore[arg-type]


class TestSwellWindowTaper:
    @pytest.mark.parametrize("raw", SPOTS, ids=[s["slug"] for s in SPOTS])
    def test_score_is_continuous_at_both_window_edges(self, raw: dict[str, object]) -> None:
        """2.0.0 dropped the direction fit to 0 just outside the window (up to 17 points per
        0.5 deg at Rincon). A swell at the ideal period, sized so that its breaking height at
        the edge is the spot's sweet spot (where the size fit is flat, so the size term's
        slope, which follows the breaking height, is not what is measured), 10 km/h offshore
        wind."""
        spot = spot_params(raw)
        sweet_m = ft_to_m(
            spot.wave_height_min_ft + 0.65 * (spot.wave_height_max_ft - spot.wave_height_min_ft)
        )
        for edge, outward in ((spot.swell_window_min, -1.0), (spot.swell_window_max, 1.0)):
            h0 = deep_water_height_for_breaking(
                sweet_m, spot.ideal_swell_period_s, edge, spot.physical_profile()
            )

            def at(direction: float, h0: float = h0) -> int:
                s = SeaState(
                    primary_swell_height_m=h0,
                    primary_swell_period_s=spot.ideal_swell_period_s,
                    primary_swell_direction_deg=direction % 360,
                    wind_speed_kmh=10.0,
                    wind_direction_deg=spot.offshore_wind_direction,
                )
                return evaluate(s, spot).score

            assert abs(at(edge) - at(edge + outward)) <= 2, (raw["slug"], edge)
            assert score_direction((edge + 30 * outward) % 360, spot) <= 0.01
            inside = score_direction(edge, spot)
            assert inside is not None
            assert score_direction((edge + 0.01 * outward) % 360, spot) == pytest.approx(
                inside, abs=1e-3
            )


class TestStoredComponents:
    def test_contract_keys_and_contributions_add_up(self) -> None:
        cases = [
            sea(),
            sea(wind_speed_kmh=40.0, wind_direction_deg=200.0),
            sea(primary_swell_height_m=0.4, primary_swell_period_s=8.0),
            sea(primary_swell_height_m=7.0, primary_swell_period_s=18.0),
            sea(wind_speed_kmh=None, wind_direction_deg=None),
            sea(primary_swell_direction_deg=100.0),
        ]
        for s in cases:
            r = evaluate(s, JBAY)
            stored = r.stored_components()
            for key in ("weights_used", "contributions"):
                assert key in stored
            assert sum(stored["weights_used"].values()) == pytest.approx(1.0, abs=0.005)
            lost = sum(stored["contributions"].values())
            assert lost + r.score == pytest.approx(100.0, abs=0.6), (s, stored["contributions"])
            assert ("tide_detail" in stored) != ("tide_excluded_reason" in stored)
            if s.wind_speed_kmh is not None:
                detail = stored["wind_detail"]
                assert detail["q"] == stored["wind"]
                assert detail["strength"] in ("glassy", "light", "moderate", "strong")
            else:
                assert "wind_detail" not in stored and "wind" not in r.weights_used

    def test_wind_detail_of_a_strong_offshore_hour(self) -> None:
        r = evaluate(sea(wind_speed_kmh=45.0, wind_direction_deg=290.0), JBAY)
        detail = r.stored_components()["wind_detail"]
        assert detail["v_offshore_kmh"] == pytest.approx(45.0, abs=0.05)
        assert detail["v_cross_kmh"] == pytest.approx(0.0, abs=0.05)
        assert detail["strength"] == "strong" and detail["favourability"] == "workable"
        assert r.wind_relation == "offshore"
        assert "strong offshore wind 45 km/h from WNW" in r.explanation
        # The wind is what costs the points.
        lost = r.stored_components()["contributions"]
        assert max(lost, key=lambda k: lost[k]) in ("wind", "wind_gate")

    def test_explanation_rounds_halves_up_like_the_charts(self) -> None:
        # Live 2026-10-16, Pipeline 17Z: 34.5 km/h was "34 km/h" in the sentence and "35 km/h"
        # in the summary above it (toFixed(0)). Both now say 35; a 10.5 s period says 11 s.
        r = evaluate(sea(wind_speed_kmh=34.5, wind_direction_deg=290.0), JBAY)
        assert "wind 35 km/h from WNW" in r.explanation
        r = evaluate(sea(primary_swell_period_s=10.5, wave_period_s=10.5), JBAY)
        assert " @ 11 s " in r.explanation
        r = evaluate(sea(wind_speed_kmh=34.4, wind_direction_deg=290.0), JBAY)
        assert "wind 34 km/h from WNW" in r.explanation

    def test_version(self) -> None:
        assert ALGORITHM_VERSION == "2.1.0"

    def test_gusts_are_display_only_unless_scored_with_a_documented_definition(self) -> None:
        gusty = replace(
            sea(wind_speed_kmh=20.0, wind_direction_deg=200.0),
            wind_gust_kmh=45.0,
            wind_gust_kind="gfs_instantaneous",
        )
        plain = evaluate(gusty, JBAY)
        assert plain.stored_components()["wind_detail"]["gust_used"] is False
        assert plain.components == evaluate(replace(gusty, wind_gust_kmh=None), JBAY).components
        scored = evaluate(gusty, JBAY, gust_scoring=True)
        assert scored.stored_components()["wind_detail"]["gust_used"] is True
        assert scored.components["wind"] < plain.components["wind"]
        unknown = evaluate(replace(gusty, wind_gust_kind="unknown"), JBAY, gust_scoring=True)
        assert unknown.stored_components()["wind_detail"]["gust_used"] is False
