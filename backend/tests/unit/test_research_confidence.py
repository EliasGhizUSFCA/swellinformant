"""Research confidence: evidence validation, agreement and the research-v1 rules.

All evidence here is SYNTHETIC (tests/research_synthetic.py); no real source is cited.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models import SurfSpot
from app.models.enums import BreakType
from app.models.research import RESEARCH_VARIABLES
from app.services.research import assess
from app.services.research.assess import (
    NO_EVIDENCE_GAPS,
    VARIABLE_LABELS,
    compute_assessment,
    evidence_fingerprint,
    independence_groups,
    overall_class,
)
from app.services.research.evidence import EvidenceValidationError, parse_evidence
from app.services.research.values import compare_value, is_sand_bottom, validate_value
from app.services.seed import SeedValidationError
from tests.research_synthetic import (
    AGREEING_VALUES,
    SYNTHETIC_PROFILE,
    parsed_sources,
    synthetic_claim,
    synthetic_file,
    synthetic_source,
)


def _claims(variable: str, agrees: bool | None = True, value: Any = "default") -> list[dict]:
    v = AGREEING_VALUES[variable] if value == "default" else value
    return [synthetic_claim(variable, v, agrees)]


def _assess(sources: list[dict], profile: dict | None = None) -> assess.ComputedAssessment:
    return compute_assessment(parsed_sources(sources), profile or SYNTHETIC_PROFILE)


def _var(sources: list[dict], variable: str, profile: dict | None = None) -> dict:
    return _assess(sources, profile).per_variable[variable]


# --- validation ----------------------------------------------------------------------


def test_value_shapes() -> None:
    assert validate_value("swell_direction", {"min": 270, "max": 20}) == []
    assert validate_value("swell_direction", {"min": 270}) != []
    assert validate_value("swell_direction", {"min": 400, "max": 20}) != []
    assert validate_value("wind", {"offshore_from_deg": 90}) == []
    assert validate_value("wind", {"offshore_from_deg": True}) != []  # bool is not a number
    assert validate_value("tide", {"tide": "low_to_mid"}) == []
    assert validate_value("tide", {"tide": "spring"}) != []
    assert validate_value("swell_size", {"min_ft": 4, "max_ft": None}) == []
    assert validate_value("swell_size", {"min_ft": None, "max_ft": None}) != []
    assert validate_value("swell_size", {"min_ft": 10, "max_ft": 4}) != []
    assert validate_value("swell_period", {"min_s": 12, "max_s": 18}) == []
    assert validate_value("swell_period", {"min_s": 12, "max_s": 45}) != []
    assert validate_value("seasonality", {"months": [11, 12, 1]}) == []
    assert validate_value("seasonality", {"months": [0, 13]}) != []
    assert validate_value("seasonality", {"months": [1, 1]}) != []
    assert validate_value("bathymetry", None) == []
    assert validate_value("bathymetry", {"bottom": "reef"}) != []  # descriptive only
    assert validate_value("wind", {"min": 1, "max": 2}) != []  # wrong shape for the variable


def test_validation_lists_every_problem() -> None:
    bad_source = synthetic_source(
        "dup",
        "blog",  # not a SOURCE_TYPE
        [
            synthetic_claim("wave_quality"),  # not a RESEARCH_VARIABLE
            {**synthetic_claim("tide", {"tide": "low"}), "excerpt": "x" * 301},
            synthetic_claim("swell_direction", {"min": 10}),  # bad shape
        ],
        url="ftp://example.org/x",
    )
    raw = synthetic_file(
        {
            "pipeline": [
                bad_source,
                synthetic_source("dup", "guidebook", _claims("wind")),
                {
                    **synthetic_source("future", "guidebook", _claims("wind")),
                    "accessed_at": "2099-01-01",
                },
                {**synthetic_source("extra", "guidebook", _claims("wind")), "pages": 3},
                synthetic_source("empty", "guidebook", []),
            ],
            "not-a-spot": [],
        }
    )
    raw["version"] = 2
    with pytest.raises(EvidenceValidationError) as info:
        parse_evidence(raw, {"pipeline"})
    assert isinstance(info.value, SeedValidationError)
    text = "\n".join(info.value.problems)
    for expected in (
        "version must be 1",
        "not-a-spot: unknown spot slug",
        "duplicate source key 'dup'",
        "source_type 'blog'",
        "variable 'wave_quality'",
        "excerpt is longer than 300",
        "value for swell_direction must have exactly the keys",
        "url must be an http(s) URL",
        "accessed_at is in the future",
        "unknown fields ['pages']",
        "claims must be a non-empty list",
    ):
        assert expected in text, expected
    assert len(info.value.problems) >= 11


def test_valid_file_parses() -> None:
    raw = synthetic_file({"pipeline": [synthetic_source("a", "guidebook", _claims("wind"))]})
    parsed = parse_evidence(raw, {"pipeline"})
    src = parsed.spots["pipeline"].sources[0]
    assert src.key == "a" and src.accessed_at is not None and src.accessed_at.year == 2026
    assert src.claims[0].value == {"offshore_from_deg": 120}


# --- agreement ------------------------------------------------------------------------


def _profile(**kw: Any) -> dict[str, Any]:
    return {**SYNTHETIC_PROFILE, **kw}


def test_direction_window_overlap_with_tolerance_and_wrap() -> None:
    p = _profile(optimal_swell_direction_min=295.0, optimal_swell_direction_max=320.0)
    # 30 deg tolerance: the widened window is 265-350.
    assert compare_value("swell_direction", {"min": 340, "max": 10}, p).agrees is True
    assert compare_value("swell_direction", {"min": 0, "max": 20}, p).agrees is False
    assert compare_value("swell_direction", {"min": 200, "max": 262}, p).agrees is False
    # A profile window that wraps through north (340 -> 20, widened 310 -> 50).
    north = _profile(optimal_swell_direction_min=340.0, optimal_swell_direction_max=20.0)
    assert compare_value("swell_direction", {"min": 30, "max": 40}, north).agrees is True
    assert compare_value("swell_direction", {"min": 350, "max": 5}, north).agrees is True
    assert compare_value("swell_direction", {"min": 300, "max": 305}, north).agrees is False
    assert compare_value("swell_direction", {"min": 60, "max": 90}, north).agrees is False
    # A source window wrapping through north against a southern profile.
    south = _profile(optimal_swell_direction_min=200.0, optimal_swell_direction_max=230.0)
    assert compare_value("swell_direction", {"min": 350, "max": 10}, south).agrees is False
    # Exact = both ends within 11.25 deg of the profile's.
    assert compare_value("swell_direction", {"min": 290, "max": 325}, p).exact is True
    assert compare_value("swell_direction", {"min": 270, "max": 325}, p).exact is False


def test_wind_tide_ranges_and_months() -> None:
    p = _profile(offshore_wind_direction=135.0)
    assert compare_value("wind", {"offshore_from_deg": 180}, p).agrees is True  # 45 deg
    assert compare_value("wind", {"offshore_from_deg": 181}, p).agrees is False
    north = _profile(offshore_wind_direction=10.0)
    assert compare_value("wind", {"offshore_from_deg": 330}, north).agrees is True  # 40 deg
    assert compare_value("wind", {"offshore_from_deg": 135}, p).exact is True

    mid = _profile(tide_preference="mid")
    assert compare_value("tide", {"tide": "mid"}, mid).exact is True
    assert compare_value("tide", {"tide": "low_to_mid"}, mid).agrees is True  # adjacent
    assert compare_value("tide", {"tide": "low"}, mid).agrees is False
    assert compare_value("tide", {"tide": "all"}, mid).agrees is False
    assert compare_value("tide", {"tide": "all"}, _profile(tide_preference="all")).agrees
    assert compare_value("tide", {"tide": "low"}, _profile(tide_preference=None)).agrees is None

    assert compare_value("swell_size", {"min_ft": 15, "max_ft": 30}, p).agrees is True
    assert compare_value("swell_size", {"min_ft": 16, "max_ft": None}, p).agrees is False
    assert compare_value("swell_size", {"min_ft": None, "max_ft": 5}, p).agrees is True
    assert compare_value("swell_size", {"min_ft": 4, "max_ft": 15}, p).exact is True
    assert compare_value("swell_period", {"min_s": 6, "max_s": 10}, p).agrees is False
    assert compare_value("swell_period", {"min_s": 14, "max_s": None}, p).agrees is True
    assert compare_value("seasonality", {"months": [6, 7]}, p).agrees is False
    assert compare_value("seasonality", {"months": [12, 4]}, p).agrees is True
    assert compare_value("seasonality", {"months": [12]}, _profile(best_months=[])).agrees is None


def test_sand_bottom_detection() -> None:
    assert is_sand_bottom(SurfSpot(break_type=BreakType.BEACH, description=""))
    assert is_sand_bottom(SurfSpot(break_type=BreakType.RIVERMOUTH, description=""))
    assert is_sand_bottom(SurfSpot(break_type=BreakType.POINT, description="A sand-bottom right"))
    assert not is_sand_bottom(SurfSpot(break_type=BreakType.REEF, description="lava reef"))


def test_computed_agreement_overrides_the_researcher_flag() -> None:
    # The researcher marked agreement, but the value (SW) is outside 295-320 +- 30.
    sources = [
        synthetic_source(
            "a", "peer_reviewed", [synthetic_claim("swell_direction", {"min": 200, "max": 240})]
        ),
        synthetic_source("b", "guidebook", _claims("swell_direction")),
    ]
    entry = _var(sources, "swell_direction")
    assert entry["agreement"] == {"supporting": 1, "contradicting": 1, "not_comparable": 0}
    assert entry["flag_differences"] == 1
    first = next(e for e in entry["evidence"] if e["source_key"] == "a")
    assert first["researcher_agrees"] is True and first["computed_agrees"] is False
    assert first["used_agrees"] is False and first["basis"] == "computed"
    user_text = " ".join([entry["summary"], *entry["gaps"]]).lower()
    assert "flag" not in user_text and "researcher" not in user_text
    # Without a structured value the researcher's flag is used.
    flagged = _var([synthetic_source("c", "guidebook", _claims("tide", False, None))], "tide")
    assert flagged["agreement"]["contradicting"] == 1


# --- per-variable rules -----------------------------------------------------------------


def test_tier_a_plus_tier_b_is_high() -> None:
    entry = _var(
        [
            synthetic_source("a", "peer_reviewed", _claims("swell_direction")),
            synthetic_source("b", "guidebook", _claims("swell_direction")),
        ],
        "swell_direction",
    )
    assert entry["class"] == "high"
    assert entry["independent_sources"] == 2 and entry["strongest_source_type"] == "peer_reviewed"
    assert entry["summary"] == (
        "Supported by 2 independent sources, including a peer-reviewed study."
    )
    assert entry["gaps"] == []


def test_three_tier_b_is_high_and_two_is_moderate() -> None:
    three = [synthetic_source(k, "guidebook", _claims("wind")) for k in ("a", "b", "c")]
    assert _var(three, "wind")["class"] == "high"
    two = _var(three[:2], "wind")
    assert two["class"] == "moderate"
    assert two["gaps"] == [
        "No survey, scientific or official source confirms the best wind direction."
    ]


def test_pages_of_one_publisher_or_site_count_once() -> None:
    same_publisher = [
        synthetic_source("a", "encyclopedia", _claims("wind"), publisher="Wikipedia"),
        synthetic_source("b", "encyclopedia", _claims("wind"), publisher=" wikipedia "),
        synthetic_source("c", "guidebook", _claims("wind")),
    ]
    entry = _var(same_publisher, "wind")
    assert entry["sources"] == 3 and entry["independent_sources"] == 2
    assert entry["class"] == "moderate"
    same_host = [
        synthetic_source("a", "guidebook", _claims("wind"), url="https://www.guide.example.org/1"),
        synthetic_source("b", "guidebook", _claims("wind"), url="https://guide.example.org/2"),
        synthetic_source("c", "guidebook", _claims("wind")),
    ]
    assert _var(same_host, "wind")["class"] == "moderate"
    groups = independence_groups(parsed_sources(same_host))
    assert groups["a"] == groups["b"] != groups["c"]


def test_single_sources() -> None:
    community = _var([synthetic_source("a", "community", _claims("tide"))], "tide")
    assert community["class"] == "low"
    assert community["gaps"] == [
        "Limited information is available about this break's preferred tide conditions."
    ]
    official = _var([synthetic_source("a", "official", _claims("tide"))], "tide")
    assert official["class"] == "moderate"
    assert official["gaps"] == ["Only one independent source confirms the best tide."]
    # One tier-B source: Moderate only when its value matches the profile exactly.
    exact = [synthetic_source("a", "guidebook", _claims("tide", value={"tide": "mid"}))]
    assert _var(exact, "tide")["class"] == "moderate"
    adjacent = [synthetic_source("a", "guidebook", _claims("tide", value={"tide": "mid_to_high"}))]
    entry = _var(adjacent, "tide")
    assert entry["class"] == "low" and entry["agreement"]["supporting"] == 1


def test_same_tier_conflict_downgrades_one_class() -> None:
    support = [synthetic_source(k, "guidebook", _claims("tide")) for k in ("a", "b", "c")]
    against = synthetic_source("d", "magazine", _claims("tide", value={"tide": "low"}))
    entry = _var([*support, against], "tide")
    assert entry["class"] == "moderate"  # High without the conflict
    assert "conflict:same_tier" in entry["rules"]
    assert entry["gaps"] == ["Sources disagree about the best tide."]
    assert entry["summary"].endswith("; 1 disagrees.")
    # Two tier-B supporters (Moderate) and a tier-B contradiction -> Low.
    assert _var([*support[:2], against], "tide")["class"] == "low"


def test_stronger_contradiction_is_low_and_weaker_is_outweighed() -> None:
    support = [synthetic_source(k, "guidebook", _claims("wind")) for k in ("a", "b", "c")]
    survey = synthetic_source("d", "official", _claims("wind", value={"offshore_from_deg": 300}))
    stronger = _var([*support, survey], "wind")
    assert stronger["class"] == "low" and stronger["gaps"] == [
        "Sources disagree about the best wind direction."
    ]
    blog = synthetic_source("e", "community", _claims("wind", value={"offshore_from_deg": 300}))
    outweighed = _var([*support, blog], "wind")
    assert outweighed["class"] == "high" and "conflict:outweighed" in outweighed["rules"]
    assert outweighed["gaps"] == [] and outweighed["summary"].endswith("1 disagrees.")
    only_against = _var([blog], "wind")
    assert only_against["class"] == "low"
    assert only_against["gaps"] == [
        "The sources found do not support this break's listed offshore wind direction."
    ]


def test_bathymetry_from_guidebooks_is_capped_at_moderate() -> None:
    guides = [synthetic_source(k, "guidebook", _claims("bathymetry", None)) for k in "abc"]
    entry = _var(guides, "bathymetry")
    assert entry["class"] == "moderate" and "cap:bathymetry_unmeasured" in entry["rules"]
    assert entry["gaps"] == ["No survey or peer-reviewed study of the bottom was found."]
    assert entry["summary"].startswith("Described by 3 independent sources")
    official = [synthetic_source("o", "official", _claims("bathymetry", None)), guides[0]]
    assert _var(official, "bathymetry")["class"] == "moderate"  # official is not a survey
    survey = [synthetic_source("s", "survey_data", _claims("bathymetry", None)), guides[0]]
    assert _var(survey, "bathymetry")["class"] == "high"


def test_recency_downgrade_at_sand_bottom_breaks() -> None:
    old = [
        synthetic_source("s", "survey_data", _claims("bathymetry", None), year=2005),
        synthetic_source("p", "peer_reviewed", _claims("bathymetry", None), year=2008),
    ]
    sand = _profile(break_type="beach", sand_bottom=True)
    entry = _var(old, "bathymetry", sand)
    assert entry["class"] == "moderate" and "recency:sand_bottom" in entry["rules"]
    assert entry["gaps"] == [
        "The newest source on the shape of the bottom is more than 15 years old, and sandbars move."
    ]
    assert _var(old, "bathymetry")["class"] == "high"  # reef: bottoms do not move
    recent = [*old, synthetic_source("r", "guidebook", _claims("bathymetry", None), year=2015)]
    assert _var(recent, "bathymetry", sand)["class"] == "high"  # 2026 - 2015 <= 15
    unknown = [
        synthetic_source("s", "survey_data", _claims("local_behavior", None), year=None),
        synthetic_source("p", "peer_reviewed", _claims("local_behavior", None), year=None),
    ]
    assert _var(unknown, "local_behavior", sand)["class"] == "moderate"  # unknown never helps
    # Other variables are not affected by the age of sources.
    dated = [
        synthetic_source("s", "survey_data", _claims("tide"), year=1990),
        synthetic_source("p", "peer_reviewed", _claims("tide"), year=1990),
    ]
    assert _var(dated, "tide", sand)["class"] == "high"


# --- overall --------------------------------------------------------------------------


def test_no_evidence_is_low_with_every_gap() -> None:
    result = compute_assessment([], SYNTHETIC_PROFILE)
    assert result.overall_class == "low"
    assert result.research_updated_at is None
    for v in RESEARCH_VARIABLES:
        entry = result.per_variable[v]
        assert entry["class"] == "low" and entry["sources"] == 0
        assert entry["gaps"] == [NO_EVIDENCE_GAPS[v]]
        assert entry["summary"] == "No sources found."
        assert entry["label"] == VARIABLE_LABELS[v]
    assert result.per_variable["tide"]["gaps"] == [
        "Limited information is available about this break's preferred tide conditions."
    ]
    assert len(result.gaps) == 4
    assert result.summary.startswith("No research sources have been recorded")


def test_complete_profile_with_one_weak_source_stays_low() -> None:
    claims = [synthetic_claim(v, AGREEING_VALUES[v], True) for v in RESEARCH_VARIABLES]
    result = _assess([synthetic_source("forum", "community", claims)])
    assert result.overall_class == "low"
    assert {e["class"] for e in result.per_variable.values()} == {"low"}
    assert all(e["agreement"]["supporting"] == 1 for e in result.per_variable.values())


def test_overall_rule() -> None:
    base = dict.fromkeys(RESEARCH_VARIABLES, "high")
    assert overall_class(base) == "high"
    assert overall_class({**base, "swell_period": "low", "seasonality": "low"}) == "high"
    assert overall_class({**base, "tide": "low"}) == "moderate"
    assert overall_class({**base, "swell_size": "moderate"}) == "moderate"
    assert overall_class({**base, "wind": "low"}) == "low"
    assert overall_class({**base, "swell_direction": "low"}) == "low"
    four_low = {
        **base,
        "tide": "low",
        "bathymetry": "low",
        "seasonality": "low",
        "swell_period": "low",
    }
    assert overall_class(four_low) == "low"


def test_well_researched_spot_is_high() -> None:
    def both(v: str) -> list[dict]:
        return [synthetic_claim(v, AGREEING_VALUES[v], True)]

    core = ["swell_direction", "swell_size", "wind", "tide", "bathymetry", "local_behavior"]
    claims = [c for v in core for c in both(v)]
    sources = [
        synthetic_source("survey", "survey_data", claims),
        synthetic_source("guide", "guidebook", claims),
    ]
    result = _assess(sources)
    assert result.overall_class == "high"
    assert result.per_variable["swell_period"]["class"] == "low"  # not researched: a gap
    assert result.summary.startswith("Independent sources confirm")
    assert result.gaps[0] == NO_EVIDENCE_GAPS["swell_period"]


def test_fingerprint_is_order_independent_and_tracks_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = synthetic_source("a", "guidebook", [*_claims("wind"), *_claims("tide")])
    b = synthetic_source("b", "official", _claims("swell_direction"))
    one = parsed_sources([a, b])
    reordered = parsed_sources([b, {**a, "claims": list(reversed(a["claims"]))}])
    fp = evidence_fingerprint(one, SYNTHETIC_PROFILE, 2026)
    assert fp == evidence_fingerprint(reordered, SYNTHETIC_PROFILE, 2026)
    assert fp != evidence_fingerprint(one[:1], SYNTHETIC_PROFILE, 2026)
    assert fp != evidence_fingerprint(one, {**SYNTHETIC_PROFILE, "tide_preference": "low"}, 2026)
    monkeypatch.setattr(assess, "METHODOLOGY_VERSION", "research-test")
    assert fp != evidence_fingerprint(one, SYNTHETIC_PROFILE, 2026)
