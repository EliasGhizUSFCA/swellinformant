"""Forecast confidence and local confidence, methodology forecast-v1 (pure rules).

Every input here is a SYNTHETIC test value; nothing is a real forecast or verification.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import replace

import pytest

from app.services.surf_quality import forecast_confidence as fc
from app.services.surf_quality.forecast_confidence import (
    HIGH,
    LOW,
    MODERATE,
    Disagreement,
    EnsembleSignal,
    ForecastInputs,
    ResearchBasisInput,
    Verification,
    classify_forecast,
    classify_local,
    rank,
)
from app.services.surf_quality.uncertainty import (
    EnsembleInfo,
    breaking_uncertainty,
    confidence_index,
)

GFS, ECMWF, GEFS = "ncep_gfs", "ecmwf_ifs", "ncep_gefs"
BOTH = (GFS, ECMWF)
SMALL = EnsembleSignal(hs_mean_m=2.0, hs_spread_m=0.2)  # relative spread 0.10


def inputs(**kw: object) -> ForecastInputs:
    base: dict[str, object] = {
        "index": 85,
        "sigma_source": "ensemble_spread",
        "lead_hours": 24.0,
        "source_code": "blend",
        "families": BOTH,
        "ensemble": SMALL,
        "disagreement": Disagreement(hs_log_ratio=0.05),
    }
    base.update(kw)
    return ForecastInputs(**base)  # type: ignore[arg-type]


def classify(**kw: object) -> fc.ForecastConfidence:
    return classify_forecast(inputs(**kw))


def high_research(**overrides: str) -> ResearchBasisInput:
    classes = {v: HIGH for v in fc.RESEARCH_BASIS_VARIABLES} | overrides
    return ResearchBasisInput(assessment_id=7, overall_class=HIGH, variable_classes=classes)


def low_research() -> ResearchBasisInput:
    classes = {v: LOW for v in fc.RESEARCH_BASIS_VARIABLES}
    return ResearchBasisInput(assessment_id=8, overall_class=LOW, variable_classes=classes)


def derived_inputs(
    lead_hours: float,
    *,
    rel_spread: float | None = None,
    hs_log_ratio: float | None = None,
    families: tuple[str, ...] = BOTH,
    flags: tuple[str, ...] = (),
) -> ForecastInputs:
    """Inputs whose index is computed the way the prediction job computes it."""
    ens = None if rel_spread is None else EnsembleInfo(hs_mean_m=2.0, hs_spread_m=2.0 * rel_spread)
    between = None if hs_log_ratio is None else hs_log_ratio / 2.0
    unc = breaking_uncertainty(5.0, lead_hours, ensemble=ens, between_model_sigma=between)
    return ForecastInputs(
        index=confidence_index(unc.sigma_forecast, 1.0, False),
        sigma_source=unc.source,
        lead_hours=lead_hours,
        source_code="blend",
        families=families,
        ensemble=None
        if ens is None
        else EnsembleSignal(ens.hs_mean_m, ens.hs_spread_m, members=31),
        disagreement=None if hs_log_ratio is None else Disagreement(hs_log_ratio, flags=flags),
    )


# --------------------------------------------------------------------------- basics
def test_index_class_boundaries_match_the_confidence_label() -> None:
    assert fc.index_class(70) == HIGH and fc.index_class(69) == MODERATE
    assert fc.index_class(45) == MODERATE and fc.index_class(44) == LOW


def test_clean_short_lead_keeps_the_index_class_with_positive_reasons() -> None:
    out = classify()
    assert out.cls == HIGH
    assert "Ensemble members agree closely." in out.reasons
    assert "GFS and ECMWF agree on swell size." in out.reasons
    assert "Not yet checked against buoys near this spot." in out.reasons


def test_reasons_are_short_and_ordered_by_importance() -> None:
    out = classify(
        is_demo=True,
        lead_hours=200.0,
        missing_wind=True,
        disagreement=Disagreement(hs_log_ratio=math.log(1.6), flags=("hs_disagree",)),
    )
    assert out.reasons[0] == "Demo data, not a real forecast."
    assert all(len(r) <= fc.MAX_REASON_CHARS for r in out.reasons)
    assert out.reasons.index("An 8-day forecast — forecasts this far out often change.") < (
        out.reasons.index("Wind data missing for this hour.")
    )


def test_rules_only_lower_the_index_class_over_a_grid() -> None:
    leads = (6.0, 60.0, 100.0, 180.0, 260.0)
    spreads: tuple[EnsembleSignal | None, ...] = (
        None,
        SMALL,
        EnsembleSignal(2.0, 0.5),
        EnsembleSignal(2.0, 0.7),
        EnsembleSignal(2.0, 0.9),
    )
    for index, lead, ens, ratio, change, stale, wind in itertools.product(
        (30, 50, 80),
        leads,
        spreads,
        (None, 0.1, 0.4, 0.8),
        (None, 1.1, 1.6),
        (False, True),
        (False, True),
    ):
        out = classify(
            index=index,
            lead_hours=lead,
            ensemble=ens,
            disagreement=Disagreement(hs_log_ratio=ratio),
            run_change_ratio=change,
            stale=stale,
            missing_wind=wind,
        )
        assert rank(out.cls) <= rank(fc.index_class(index))
        assert out.index_class == fc.index_class(index)


def test_lead_wording() -> None:
    assert fc.forecast_phrase(10.0) == "A forecast less than a day ahead"
    assert fc.forecast_phrase(60.0) == "A 3-day forecast"  # 2.5 days rounds up
    assert fc.forecast_phrase(190.0) == "An 8-day forecast"
    assert fc.forecast_phrase(260.0) == "An 11-day forecast"
    assert fc.lead_phrase(None) is None and fc.lead_phrase(5.0) == "less than a day away"
    assert (
        fc.lead_phrase(24.0) == "about 1 day away" and fc.lead_phrase(146.0) == "about 6 days away"
    )


# --------------------------------------------------------------------------- per rule
def test_demo_data_is_always_low() -> None:
    out = classify(is_demo=True, families=("demo",), disagreement=None)
    assert out.cls == LOW and out.reasons[0] == "Demo data, not a real forecast."
    assert "cap:low:demo" in [r.as_str() for r in out.rules]


def test_missing_variables() -> None:
    assert classify(missing_wind=True).cls == MODERATE
    assert "Wind data missing for this hour." in classify(missing_wind=True).reasons
    out = classify(missing_swell=True)
    assert out.cls == LOW and out.reasons[0].startswith("Swell period or direction missing")


@pytest.mark.parametrize(
    ("lead", "expected", "reason"),
    [
        (66.0, HIGH, "A 3-day forecast."),
        (168.0, HIGH, "A 7-day forecast."),
        (169.0, MODERATE, "A 7-day forecast — forecasts this far out often change."),
        (240.0, MODERATE, "A 10-day forecast — forecasts this far out often change."),
        (241.0, LOW, "A 10-day forecast — too far ahead to be dependable."),
    ],
)
def test_lead_time_caps(lead: float, expected: str, reason: str) -> None:
    out = classify(lead_hours=lead)
    assert out.cls == expected and reason in out.reasons


def test_3_to_7_days_high_only_with_a_small_ensemble_spread() -> None:
    assert classify(lead_hours=120.0).cls == HIGH  # small spread, two independent families
    no_ens = classify(lead_hours=120.0, ensemble=None)
    assert no_ens.cls == MODERATE
    assert "A 5-day forecast with no ensemble data to check it." in no_ens.reasons
    wide = classify(lead_hours=120.0, ensemble=EnsembleSignal(2.0, 0.4))  # 0.20
    assert wide.cls == MODERATE
    assert "A 5-day forecast, and ensemble members do not agree closely." in wide.reasons


def test_no_ensemble_inside_3_days_is_a_reason_not_a_penalty() -> None:
    out = classify(ensemble=None, sigma_source="lead_table", index=82)
    assert out.cls == HIGH and "No ensemble data for this hour." in out.reasons
    assert all(r.effect == "note" for r in out.rules)


def test_ensemble_spread_downgrades_when_large() -> None:
    assert classify(ensemble=EnsembleSignal(2.0, 0.62)).cls == MODERATE  # 0.31: -1
    out = classify(ensemble=EnsembleSignal(2.0, 0.9))  # 0.45
    assert out.cls == LOW and "Ensemble members disagree widely on swell size." in out.reasons
    # flat sea: spread / mean is not meaningful, so the ensemble counts as unavailable
    assert EnsembleSignal(0.04, 0.03).rel_spread is None


def test_independent_disagreement_caps() -> None:
    size = classify(disagreement=Disagreement(hs_log_ratio=math.log(1.5), flags=("hs_disagree",)))
    assert size.cls == MODERATE and size.reasons[0] == "GFS and ECMWF disagree on swell size."
    strong = classify(disagreement=Disagreement(hs_log_ratio=math.log(2.2)))
    assert strong.cls == LOW
    assert strong.reasons[0] == "GFS and ECMWF disagree strongly on swell size."
    for flag, text in (
        ("period_disagree", "GFS and ECMWF disagree on swell period."),
        ("direction_disagree", "GFS and ECMWF disagree on swell direction."),
        ("wind_disagree", "GFS and ECMWF disagree on the wind."),
    ):
        out = classify(disagreement=Disagreement(hs_log_ratio=0.05, flags=(flag,)))
        assert out.cls == MODERATE and text in out.reasons
        assert "GFS and ECMWF agree on swell size." not in out.reasons


def test_disagreement_threshold_is_the_blends_flag() -> None:
    from app.services.forecasts import blend

    assert fc.HS_DISAGREE_LOG_RATIO == blend.HS_LOG_RATIO_FLAG


def test_same_family_mismatch_is_an_integrity_cap() -> None:
    out = classify(disagreement=Disagreement(0.05, flags=("same_family_mismatch:t_copy",)))
    assert out.cls == MODERATE


def test_run_change_downgrades_one_class() -> None:
    assert fc.run_change_ratio(2.0, 1.0) == pytest.approx(2.0)
    assert fc.run_change_ratio(0.2, 0.3) == pytest.approx(0.6)  # below the 0.5 m floor
    out = classify(run_change_ratio=1.5)
    assert out.cls == MODERATE
    assert "Forecast changed a lot since the previous model run." in out.reasons
    assert classify(run_change_ratio=1.39).cls == HIGH


def test_verification() -> None:
    near_ok = Verification("station", "aodn:x", "gfswave_grid", 24, 900, 0.15, 0.8, True)
    out = classify(verification=(near_ok,))
    assert out.cls == HIGH and "Not yet checked against buoys near this spot." not in out.reasons
    far_ok = replace(near_ok, scope="global", scope_key="all", near_spot=False)
    assert (
        "Not yet checked against buoys near this spot." in classify(verification=(far_ok,)).reasons
    )
    for poor in (replace(near_ok, scatter_index=0.6), replace(near_ok, skill_vs_persistence=-0.1)):
        out = classify(verification=(poor,))
        assert (
            out.cls == MODERATE and "Recent checks against buoys show large errors." in out.reasons
        )


def test_stale_and_single_source() -> None:
    out = classify(stale=True)
    assert out.cls == MODERATE and fc.STALE_REASON in out.reasons
    single = classify(lead_hours=100.0, families=(GFS,), disagreement=None)
    assert single.cls == MODERATE
    assert "Only one independent forecast model is available this far out." in single.reasons
    assert classify(lead_hours=48.0, families=(GFS,), disagreement=None).cls == HIGH


def test_inputs_record_every_value_and_rule() -> None:
    out = classify(lead_hours=200.0, run_change_ratio=1.6)
    local = classify_local(out.cls, None)
    stored = fc.confidence_inputs(out, local)
    assert stored["method"] == "forecast-v1"
    assert stored["lead_h"] == 200.0 and stored["independent_families"] == 2
    assert stored["ensemble"] == {"rel_spread": 0.1, "members": None}
    assert (
        "cap:moderate:lead_7_10d" in stored["rules"] and "downgrade:run_change" in stored["rules"]
    )
    assert stored["class"] == out.cls == LOW
    assert stored["local"]["reason"] == fc.NO_RESEARCH_REASON
    assert fc.stored_reasons(stored)[-1] == fc.NO_RESEARCH_REASON


# --------------------------------------------------------------------------- local
def test_local_confidence_is_min_of_forecast_and_research_basis() -> None:
    assert classify_local(HIGH, high_research()).cls == HIGH
    assert classify_local(MODERATE, high_research()).cls == MODERATE
    # basis = min(direction, size, max(bathymetry, local_behavior))
    assert fc.research_basis(high_research(bathymetry=LOW).variable_classes) == HIGH
    assert (
        fc.research_basis(high_research(bathymetry=LOW, local_behavior=MODERATE).variable_classes)
        == MODERATE
    )
    assert fc.research_basis(high_research(swell_size=LOW).variable_classes) == LOW
    assert fc.research_basis({}) == LOW  # unassessed variables count as Low
    partial = classify_local(HIGH, high_research(swell_direction=MODERATE))
    assert partial.cls == MODERATE
    assert partial.reason == "How this break responds to swell is only partly documented."


def test_no_research_assessment_gives_low_local_confidence() -> None:
    out = classify_local(HIGH, None)
    assert out.cls == LOW and out.research_class is None and out.assessment_id is None
    assert out.reason == "How this break responds to swell is not well documented yet."


def test_read_time_staleness_caps_both_classes() -> None:
    class Stored:
        forecast_confidence_class = HIGH
        local_confidence_class = HIGH
        confidence_inputs = {"reasons": ["Ensemble members agree closely."], "local": {}}
        lead_hours = 30.0

    fresh = fc.api_fields(Stored())
    assert fresh["forecast_confidence_class"] == HIGH and fresh["lead_hours"] == 30.0
    stale = fc.api_fields(Stored(), stale=True)
    assert stale["forecast_confidence_class"] == stale["local_confidence_class"] == MODERATE
    assert stale["confidence_reasons"][0] == fc.STALE_REASON


# --------------------------------------------------------------------------- owner's requirements
def test_requirement_2_well_researched_spot_low_or_moderate_at_9_days_or_strong_disagreement() -> (
    None
):
    research = high_research()
    nine_days = classify(lead_hours=216.0, index=90)
    assert nine_days.cls in (LOW, MODERATE)
    assert classify_local(nine_days.cls, research).cls in (LOW, MODERATE)
    strong = classify_forecast(derived_inputs(30.0, hs_log_ratio=math.log(2.5)))
    assert strong.cls in (LOW, MODERATE)
    assert "GFS and ECMWF disagree strongly on swell size." in strong.reasons
    assert rank(classify_local(strong.cls, research).cls) <= rank(strong.cls)


def test_requirement_3_poorly_researched_spot_high_forecast_low_local() -> None:
    forecast = classify_forecast(derived_inputs(30.0, rel_spread=0.08, hs_log_ratio=0.05))
    assert forecast.cls == HIGH
    local = classify_local(forecast.cls, low_research())
    assert local.cls == LOW
    assert local.reason == "How this break responds to swell is not well documented yet."
    without = classify_local(forecast.cls, None)
    assert without.cls == LOW and without.reason == local.reason


def test_requirement_5_monotone_in_lead_time() -> None:
    for rel, ratio, fams in itertools.product(
        (None, 0.05, 0.12, 0.2, 0.33, 0.45), (None, 0.05, 0.3, 0.5, 0.8), (BOTH, (GFS,))
    ):
        classes = [
            rank(
                classify_forecast(
                    derived_inputs(h, rel_spread=rel, hs_log_ratio=ratio, families=fams)
                ).cls
            )
            for h in range(0, 300, 3)
        ]
        assert classes == sorted(classes, reverse=True), (rel, ratio, fams)


def test_requirement_5_monotone_in_ensemble_spread_and_independent_disagreement() -> None:
    spreads = [i / 100 for i in range(0, 61, 2)]
    ratios = [i / 50 for i in range(0, 51)]
    for lead in (6.0, 48.0, 100.0, 160.0, 200.0, 250.0):
        for ratio in (None, 0.1, 0.4, 0.9):
            classes = [
                rank(classify_forecast(derived_inputs(lead, rel_spread=s, hs_log_ratio=ratio)).cls)
                for s in spreads
            ]
            assert classes == sorted(classes, reverse=True), (lead, ratio)
        for rel in (None, 0.08, 0.25):
            classes = [
                rank(classify_forecast(derived_inputs(lead, rel_spread=rel, hs_log_ratio=r)).cls)
                for r in ratios
            ]
            assert classes == sorted(classes, reverse=True), (lead, rel)
        clean = rank(classify_forecast(derived_inputs(lead, rel_spread=0.08, hs_log_ratio=0.1)).cls)
        flagged = derived_inputs(
            lead, rel_spread=0.08, hs_log_ratio=0.1, flags=("period_disagree", "wind_disagree")
        )
        assert rank(classify_forecast(flagged).cls) <= clean


def test_requirement_5_gfs_and_gefs_agreement_alone_never_raises_confidence() -> None:
    assert fc.independent_family_count((GFS, GEFS)) == 1
    assert fc.independent_family_count((GFS, ECMWF)) == 2
    assert fc.independent_family_count((GFS, "unknown")) == 1
    for lead, rel in itertools.product(range(0, 300, 6), (None, 0.05, 0.12, 0.25, 0.4)):
        alone = derived_inputs(lead, rel_spread=rel, families=(GFS,))
        # GEFS listed as if it were a second model, agreeing perfectly with GFS
        paired = replace(alone, families=(GFS, GEFS), disagreement=Disagreement(hs_log_ratio=0.0))
        a, b = classify_forecast(alone), classify_forecast(paired)
        assert rank(b.cls) <= rank(a.cls), (lead, rel)
        assert not any("agree on swell size" in r and "Ensemble" not in r for r in b.reasons)
    # beyond 3 days only an independent family (ECMWF) can lift the single-source cap
    gefs = classify_forecast(replace(derived_inputs(100.0, rel_spread=0.05), families=(GFS, GEFS)))
    ecmwf = classify_forecast(derived_inputs(100.0, rel_spread=0.05, hs_log_ratio=0.02))
    assert gefs.cls == MODERATE and ecmwf.cls == HIGH
