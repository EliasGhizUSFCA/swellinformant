"""The tide is scored only where its preference is established (algorithm 2.1.0).

Spot parameters come from the committed spots.json, with the tide research classes the
committed research evidence gives (Dungeons and Mundaka high, Rincon low); the tide stages
are SYNTHETIC.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import (
    NO_TIDE_PREDICTION,
    TIDE_DISPUTED,
    TIDE_NOT_RESEARCHED,
    TIDE_RESEARCH_LOW,
    evaluate,
    tide_excluded_reason,
)

SPOTS = {s["slug"]: s for s in json.loads((DEFAULT_DATA_DIR / "spots.json").read_text())}
FIELDS = (
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


def spot(slug: str, cls: str | None, decision: str | None = None) -> SpotParams:
    raw = SPOTS[slug]
    return SpotParams(
        slug=slug,
        **{f: float(raw[f]) for f in FIELDS},  # type: ignore[arg-type]
        tide_preference=raw["tide_preference"],
        tide_research_class=cls,
        tide_decision=decision,
    )


def sea_for(s: SpotParams, stage: float | None) -> SeaState:
    centre = (s.optimal_swell_direction_min + s.optimal_swell_direction_max) / 2
    return SeaState(
        primary_swell_height_m=3.0,
        primary_swell_period_s=s.ideal_swell_period_s,
        primary_swell_direction_deg=centre,
        wind_speed_kmh=8.0,
        wind_direction_deg=s.offshore_wind_direction,
        tide_stage=stage,
    )


LOW_EVENT = {"stage": 0.0, "rising": True, "range_m": 1.4, "source": "synthetic"}
HIGH_EVENT = {"stage": 1.0, "rising": False, "range_m": 1.4, "source": "synthetic"}


def test_a_low_research_class_leaves_the_tide_out_and_says_so() -> None:
    rincon = spot("rincon", "low")
    assert rincon.tide_preference == "low_to_mid"
    r = evaluate(sea_for(rincon, 0.2), rincon)
    assert "tide" not in r.components and "tide" not in r.weights_used
    stored = r.stored_components()
    assert stored["tide_excluded_reason"] == TIDE_RESEARCH_LOW
    assert "tide_detail" not in stored
    assert "tide not scored (tide preference not well established" in r.explanation
    # The score is the same at every stage: the tide does not count.
    assert evaluate(sea_for(rincon, 0.9), rincon).score == r.score


def test_dungeons_prefers_a_low_tide_when_its_preference_is_established() -> None:
    dungeons = spot("dungeons", "high", "kept")
    assert dungeons.tide_preference == "low_to_mid"
    low = evaluate(sea_for(dungeons, 0.0), dungeons, tide_detail=LOW_EVENT)
    high = evaluate(sea_for(dungeons, 1.0), dungeons, tide_detail=HIGH_EVENT)
    assert low.components["tide"] > high.components["tide"]
    assert low.score > high.score
    assert low.stored_components()["tide_detail"] == LOW_EVENT
    assert "tide_excluded_reason" not in low.stored_components()
    assert "unfavourable tide" in high.explanation


def test_hours_without_tide_data_are_unchanged() -> None:
    mundaka = spot("mundaka", "high")
    with_pref = evaluate(sea_for(mundaka, None), mundaka)
    without = evaluate(sea_for(mundaka, None), replace(mundaka, tide_preference=None))
    assert with_pref.score == without.score
    assert with_pref.components == without.components
    assert with_pref.stored_components()["tide_excluded_reason"] == NO_TIDE_PREDICTION
    assert "tide" not in with_pref.explanation


@pytest.mark.parametrize(
    ("cls", "decision", "preference", "reason"),
    [
        ("high", None, "mid", None),
        ("moderate", "kept", "mid", None),
        ("moderate", "changed", "all", None),
        ("low", None, "mid", TIDE_RESEARCH_LOW),
        (None, None, "mid", TIDE_NOT_RESEARCHED),
        ("high", "disputed", "mid", TIDE_DISPUTED),
        ("high", None, None, "Tide preference not well established: no preference recorded."),
    ],
)
def test_gate(
    cls: str | None, decision: str | None, preference: str | None, reason: str | None
) -> None:
    s = replace(spot("mundaka", cls, decision), tide_preference=preference)
    assert tide_excluded_reason(s) == reason
    r = evaluate(sea_for(s, 0.5), s)
    assert ("tide" in r.components) == (reason is None)
    if reason is not None:
        assert r.stored_components()["tide_excluded_reason"] == reason
        assert reason.startswith("Tide preference not well established")
