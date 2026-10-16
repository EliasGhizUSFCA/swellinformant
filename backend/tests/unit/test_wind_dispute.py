"""Disputed offshore wind (forecast-v1.1): explanation, stored inputs and reasons.

Every sea state and research entry here is SYNTHETIC test data.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.services.research.decisions import WindDispute
from app.services.surf_quality import forecast_confidence as fc
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import disputed_wind_relation, evaluate, wind_relation

SPOT = SpotParams(
    slug="synthetic",
    latitude=-8.0,
    longitude=115.0,
    swell_window_min=180,
    swell_window_max=260,
    optimal_swell_direction_min=200,
    optimal_swell_direction_max=230,
    offshore_wind_direction=150,
    min_swell_period_s=11,
    ideal_swell_period_s=15,
    wave_height_min_ft=3,
    wave_height_max_ft=12,
)
SEA = SeaState(
    primary_swell_height_m=1.5,
    primary_swell_period_s=14.0,
    primary_swell_direction_deg=215.0,
    wind_speed_kmh=15.0,
    wind_direction_deg=150.0,
)
DISPUTE = WindDispute(150.0, 67.5, ("conflict:stronger",), ("a", "b"))


def test_both_relations_are_named_when_they_differ_and_the_score_is_unchanged() -> None:
    plain = evaluate(SEA, SPOT)
    disputed = evaluate(SEA, SPOT, alternative_offshore_deg=67.5)
    assert disputed.score == plain.score and disputed.components == plain.components
    # The stored wind breakdown (2.1.0) uses the profile's direction too.
    assert disputed.stored_components() == plain.stored_components()
    assert plain.stored_components()["wind_detail"]["params"]["offshore_wind_direction"] == {
        "value": 150.0,
        "provenance": "research",
    }
    assert disputed.wind_relation == plain.wind_relation == "offshore"
    alt = disputed_wind_relation(SEA.wind_speed_kmh, SEA.wind_direction_deg, SPOT, 67.5)
    assert alt == "cross-shore"  # 82.5 deg from the alternative direction
    assert "moderate wind 15 km/h from SSE, offshore or cross-shore: sources disagree" in (
        disputed.explanation
    )
    # 2.1.0 names the wind's strength class (Beaufort-based) before the relation.
    assert "moderate offshore wind 15 km/h from SSE" in plain.explanation
    # The rest of the explanation is untouched (the frontend parses the size part).
    assert disputed.explanation.split("; ")[0] == plain.explanation.split("; ")[0]
    assert disputed.explanation.split("; ")[-1] == plain.explanation.split("; ")[-1]


def test_no_note_when_the_relations_agree_or_the_wind_is_calm() -> None:
    same = evaluate(SEA, SPOT, alternative_offshore_deg=160.0)  # also offshore
    assert "sources disagree" not in same.explanation
    calm = replace(SEA, wind_speed_kmh=3.0)
    glassy = evaluate(calm, SPOT, alternative_offshore_deg=67.5)
    assert glassy.wind_relation == "glassy" and "sources disagree" not in glassy.explanation
    assert disputed_wind_relation(15.0, 150.0, SPOT, None) is None
    assert wind_relation(15.0, 150.0, SPOT) == "offshore"


def _forecast() -> fc.ForecastConfidence:
    return fc.classify_forecast(
        fc.ForecastInputs(index=80, sigma_source="lead_table", lead_hours=30, source_code="test")
    )


def test_the_dispute_is_stored_and_listed_between_forecast_and_local_reasons() -> None:
    forecast = _forecast()
    research = fc.ResearchBasisInput(
        7, "moderate", {v: "moderate" for v in fc.RESEARCH_BASIS_VARIABLES}
    )
    local = fc.classify_local(forecast.cls, research)
    entry = fc.wind_dispute_inputs(DISPUTE, "offshore", "cross-onshore")
    stored = fc.confidence_inputs(forecast, local, wind_dispute=entry)
    assert stored["wind_dispute"] == {
        "profile_offshore_deg": 150.0,
        "alternative_offshore_deg": 67.5,
        "basis": ["conflict:stronger"],
        "sources": ["a", "b"],
        "relation": "offshore",
        "relation_alternative": "cross-onshore",
        "differs": True,
        "reason": fc.WIND_DISPUTE_REASON,
    }
    assert fc.WIND_DISPUTE_REASON == "Sources disagree about which wind is offshore here."
    reasons = fc.stored_reasons(stored)
    assert reasons[: len(forecast.reasons)] == forecast.reasons
    assert reasons[len(forecast.reasons)] == fc.WIND_DISPUTE_REASON
    assert reasons[-1] == local.reason
    # Classes are exactly those of forecast-v1: the note changes neither.
    without = fc.confidence_inputs(forecast, local)
    assert {k: v for k, v in stored.items() if k != "wind_dispute"} == without
    assert stored["local"]["class"] == local.cls == fc.lower(forecast.cls, "moderate")


def test_api_fields_carry_the_reason_and_the_staleness_cap_keeps_it() -> None:
    forecast = _forecast()
    local = fc.classify_local(forecast.cls, None)
    inputs = fc.confidence_inputs(
        forecast, local, wind_dispute=fc.wind_dispute_inputs(DISPUTE, "offshore", None)
    )
    assert inputs["wind_dispute"]["differs"] is False
    pred = SimpleNamespace(
        forecast_confidence_class=forecast.cls,
        local_confidence_class=local.cls,
        confidence_inputs=inputs,
        lead_hours=30.0,
    )
    fresh = fc.api_fields(pred)
    assert fc.WIND_DISPUTE_REASON in fresh["confidence_reasons"]
    stale = fc.api_fields(pred, stale=True)
    assert stale["confidence_reasons"][0] == fc.STALE_REASON
    assert fc.WIND_DISPUTE_REASON in stale["confidence_reasons"]
    # The local reason is still the research sentence, never the wind note.
    assert fresh["local_confidence_reason"] in (None, local.reason)


def test_research_input_keeps_the_wind_entry_for_the_dispute_only() -> None:
    wind = {"class": "low", "rules": ["conflict:same_tier"], "evidence": []}
    row = SimpleNamespace(
        id=3,
        overall_class="low",
        per_variable={"wind": wind, "swell_direction": {"class": "high"}},
    )
    research = fc.research_input(row)
    assert research is not None and research.wind == wind
    assert "wind" not in research.variable_classes  # wind is not in the research basis
