"""Profile decisions (ADR-018), wind disputes and ``research lint``.

The rule tests use SYNTHETIC evidence entries (no real source); the last tests read the
REAL committed data files and check that the recorded decisions are the rule's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.models import SurfSpot
from app.services.research.assess import compute_assessment
from app.services.research.decisions import (
    FIELD_OF_VARIABLE,
    Entry,
    geometry_gap,
    lint,
    profile_from_spot,
    propose,
    propose_all,
    validate_decisions,
    wind_dispute,
)
from app.services.research.evidence import parse_evidence
from app.services.research.values import profile_values
from app.services.seed import DEFAULT_DATA_DIR, SPOT_FIELDS
from tests.research_synthetic import SYNTHETIC_PROFILE

A, B, C = 0, 1, 2  # tiers


def entry(
    key: str,
    value: dict[str, Any] | None,
    agrees: bool | None,
    tier: int = B,
    retrieval: str = "fetched",
    publisher: str | None = None,
) -> Entry:
    publisher = publisher or f"Synthetic {key}"
    return Entry(key, publisher, f"https://{key}.example.org/", tier, retrieval, value, agrees)


def wind(deg: float) -> dict[str, Any]:
    return {"offshore_from_deg": deg}


P = {**SYNTHETIC_PROFILE, "offshore_wind_direction": 150.0}


# --------------------------------------------------------------------------- the rule
def test_two_direct_publishers_that_outrank_all_direct_support_change_the_value() -> None:
    p = propose(
        "wind",
        [
            entry("a", wind(67.5), False, B),
            entry("b", wind(90), False, B),
            entry("c", wind(135), True, C),  # supports, but of a lower tier than both
        ],
        P,
    )
    assert p is not None and p.decision == "changed" and p.reason == "criterion_met"
    # The new value is one a source states (the most central of the agreeing ones), never
    # an average of directions.
    assert p.new_value in (67.5, 90.0) and p.field_name == "offshore_wind_direction"


def test_same_tier_direct_support_blocks_the_change_and_the_value_is_disputed() -> None:
    entries = [
        entry("a", wind(67.5), False, B),
        entry("b", wind(90), False, C),
        entry("c", wind(135), True, C),  # same tier as the second agreeing publisher
    ]
    p = propose("wind", entries, P)
    assert p is not None and p.decision == "disputed"
    assert p.reason == "direct_support_of_the_same_or_higher_tier"
    assert p.blocking_keys == ["c"] and p.new_value is None and p.alternative == 67.5


def test_search_summaries_alone_never_change_or_dispute_a_value() -> None:
    entries = [
        entry("a", wind(67.5), False, B, retrieval="web_search"),
        entry("b", wind(70), False, B, retrieval="web_search"),
        entry("c", wind(150), True, B),
    ]
    p = propose("wind", entries, P)
    assert p is not None and (p.decision, p.reason) == ("kept", "search_only_contradiction")


def test_kept_when_outweighed_or_a_single_publisher_contradicts() -> None:
    outweighed = propose(
        "wind", [entry("a", wind(60), False, C), entry("b", wind(140), True, B)], P
    )
    assert outweighed is not None
    assert (outweighed.decision, outweighed.reason) == ("kept", "outweighed_by_direct_support")
    single = propose(
        "wind",
        [
            entry("a", wind(60), False, B),
            entry("b", wind(140), True, B),
            entry("c", wind(150), True, B),
        ],
        P,
    )
    assert single is not None
    assert (single.decision, single.reason) == ("kept", "single_publisher_contradicts")
    lone = propose("wind", [entry("a", wind(60), False, B), entry("b", wind(140), True, C)], P)
    assert lone is not None and lone.decision == "disputed"
    assert lone.reason == "fewer_than_two_direct_publishers_agree"


def test_a_publisher_does_not_outweigh_its_own_contradiction() -> None:
    # Publisher "One Site" supports on one page and contradicts on another; with one other
    # same-tier supporter that is not "two publishers support the profile" (anchor-point's
    # wind, Lobitos' swell window and Waimea's size range in the committed records).
    entries = [
        entry("a1", wind(120), True, B, publisher="One Site"),
        entry("a2", wind(60), False, B, publisher="One Site"),
        entry("b", wind(150), True, B),
    ]
    p = propose("wind", entries, P)
    assert p is not None and p.decision == "disputed"
    assert p.reason == "fewer_than_two_direct_publishers_agree"
    # Two OTHER same-tier supporters still keep the profile.
    kept = propose("wind", [*entries, entry("c", wind(140), True, B)], P)
    assert kept is not None and (kept.decision, kept.reason) == (
        "kept",
        "single_publisher_contradicts",
    )
    # Its own better-tier support does not outweigh its lower-tier contradiction either.
    mixed = [
        entry("d1", wind(150), True, A, publisher="Mixed Agency"),
        entry("d2", wind(60), False, C, publisher="Mixed Agency"),
    ]
    q = propose("wind", mixed, P)
    assert q is not None and q.decision == "disputed"
    outweighed = propose("wind", [*mixed, entry("e", wind(140), True, B)], P)
    assert outweighed is not None
    assert (outweighed.decision, outweighed.reason) == ("kept", "outweighed_by_direct_support")


def test_pages_of_one_publisher_count_once() -> None:
    same = [
        entry("a1", wind(67.5), False, B, publisher="One Site"),
        entry("a2", wind(70), False, B, publisher="One Site"),
    ]
    p = propose("wind", same, P)
    assert p is not None and p.decision == "disputed"  # one publisher is not two
    assert p.candidate is not None and len(p.candidate.fetched_tiers) == 1


def test_no_contradiction_means_no_record() -> None:
    assert propose("wind", [entry("a", wind(140), True, B)], P) is None
    assert propose("tide", [], P) is None


def test_size_candidates_replace_only_the_contradicted_bound() -> None:
    profile = {**P, "wave_height_min_ft": 4.0, "wave_height_max_ft": 12.0}
    p = propose(
        "swell_size",
        [
            entry("a", {"min_ft": 4, "max_ft": 20}, False, B),
            entry("b", {"max_ft": 20}, False, B),
        ],
        profile,
    )
    assert p is not None and p.decision == "changed"
    assert p.new_value == {"min": 4.0, "max": 20.0}  # the minimum is not touched


def test_a_single_direction_never_becomes_a_new_swell_window() -> None:
    profile = {**P, "optimal_swell_direction_min": 200.0, "optimal_swell_direction_max": 230.0}
    p = propose(
        "swell_direction",
        [entry("a", {"min": 300, "max": 300}, False, B), entry("b", {"min": 305}, False, B)],
        profile,
    )
    assert p is not None and p.decision == "disputed"
    assert p.reason == "single_direction_does_not_define_a_window"


def test_tide_changes_when_no_direct_source_supports_the_old_class() -> None:
    profile = {**P, "tide_preference": "mid"}
    p = propose(
        "tide",
        [
            entry("a", {"tide": "all"}, False, B),
            entry("b", {"tide": "all"}, False, C),
            entry("c", {"tide": "mid"}, True, B, retrieval="web_search"),
        ],
        profile,
    )
    assert p is not None and (p.decision, p.new_value) == ("changed", "all")


def test_the_alternative_is_the_most_central_stated_value() -> None:
    profile = {**P, "offshore_wind_direction": 290.0}
    p = propose(
        "wind",
        [
            entry("a", wind(236.25), False, B),
            entry("b", wind(202.5), False, B),
            entry("c", wind(225), False, C),
            entry("d", wind(247.5), True, B),
        ],
        profile,
    )
    assert p is not None and p.alternative == 225.0  # the medoid, not an average


# --------------------------------------------------------------------------- disputes
def _wind_entry(rules: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {"wind": {"class": "low", "rules": rules, "evidence": evidence}}


def _ev(key: str, value: float | None, agrees: bool | None, stype: str = "magazine") -> dict:
    return {
        "source_key": key,
        "publisher": f"Synthetic {key}",
        "url": f"https://{key}.example.org/",
        "source_type": stype,
        "retrieval": "fetched",
        "value": None if value is None else {"offshore_from_deg": value},
        "used_agrees": agrees,
    }


def test_a_wind_conflict_rule_is_a_dispute_with_the_main_contradicting_direction() -> None:
    per_variable = _wind_entry(
        ["base:moderate", "conflict:same_tier"],
        [_ev("a", 67.5, False), _ev("b", 90, False, "community"), _ev("c", 150, True)],
    )
    d = wind_dispute(per_variable, 150.0)
    assert d is not None and d.basis == ("conflict:same_tier",)
    assert d.alternative_offshore_deg == 67.5 and set(d.source_keys) == {"a", "b"}
    assert d.as_dict()["profile_offshore_deg"] == 150.0


def test_outweighed_contradictions_and_no_evidence_are_no_dispute() -> None:
    per_variable = _wind_entry(
        ["base:high", "conflict:outweighed"], [_ev("a", 67.5, False, "community")]
    )
    assert wind_dispute(per_variable, 150.0) is None
    assert wind_dispute(None, 150.0) is None


def test_a_disputed_decision_record_is_a_dispute_even_without_an_assessment() -> None:
    record = {"decision": "disputed", "alternative": 225.0, "sources": ["x"]}
    d = wind_dispute(None, 290.0, record)
    assert d is not None and d.basis == ("decision:disputed",)
    assert d.alternative_offshore_deg == 225.0 and d.source_keys == ("x",)
    assert wind_dispute(None, 290.0, {"decision": "kept", "alternative": 225.0}) is None
    # A conflict without any stated direction still disputes, with no alternative.
    no_value = _wind_entry(["conflict:unsupported"], [_ev("a", None, False)])
    d2 = wind_dispute(no_value, 150.0)
    assert d2 is not None and d2.alternative_offshore_deg is None


# --------------------------------------------------------------------------- the file
def _record(**kw: Any) -> dict[str, Any]:
    base = {
        "slug": "pipeline",
        "field": "offshore_wind_direction",
        "old_value": 135.0,
        "new_value": None,
        "decision": "kept",
        "sources": ["a"],
        "rationale": "SYNTHETIC rationale.",
        "decided_at": "2026-10-13",
    }
    return {**base, **kw}


def test_validate_decisions_lists_every_problem() -> None:
    spots = {"pipeline": {"offshore_wind_direction": 135, "tide_preference": "all"}}
    raw = {
        "version": 1,
        "decisions": [
            _record(),
            _record(decision="changed"),  # changed without a new value
            _record(field="colour"),
            _record(decided_at="yesterday", sources=["zzz"]),
            _record(old_value=90.0),  # not the profile's current value
            _record(reason="because"),
            {**_record(), "extra": 1},
            _record(slug="nowhere"),
        ],
    }
    problems = validate_decisions(raw, spots, {"pipeline": {"a"}})
    text = "\n".join(problems)
    for expected in (
        "new_value is set exactly when the decision is changed",
        "field must be one of",
        "decided_at must be a date",
        "unknown evidence keys ['zzz']",
        "is not the record's old value 90.0",
        "reason must be one of",
        "unknown fields ['extra']",
        "unknown spot slug",
        "duplicate record",
    ):
        assert expected in text, expected
    assert validate_decisions({"version": 2}) != []


def test_lint_lists_unexplained_geometry_missing_and_outdated_records() -> None:
    spot = {
        "offshore_wind_direction": 135,
        "optimal_swell_direction_min": 200,
        "optimal_swell_direction_max": 230,
        "tide_preference": "mid",
        "wave_height_min_ft": 4,
        "wave_height_max_ft": 12,
        "min_swell_period_s": 10,
        "ideal_swell_period_s": 14,
        "best_months": [6, 7],
    }
    assert round(geometry_gap(135.0, 215.0)) == 100
    per_variable = {
        "tide": {"evidence": [_tide_ev("a", "all", False), _tide_ev("b", "all", False)]},
    }
    issues = lint({"x": spot}, {"x": 215.0}, {"x": per_variable}, {})
    kinds = sorted(i.kind for i in issues)
    assert kinds == ["geometry", "missing_record"]
    note = _record(slug="x", reason="wave_face", old_value=135.0)
    stale = _record(slug="x", field="tide_preference", old_value="mid", decision="disputed")
    issues = lint({"x": spot}, {"x": 215.0}, {"x": per_variable}, {"x": [note, stale]})
    assert [i.kind for i in issues] == ["rule_differs"]  # the rule now says changed
    assert lint({"x": spot}, {"x": 330.0}, {}, {}) == []  # 15 deg from straight offshore


def _tide_ev(key: str, tide: str, agrees: bool) -> dict[str, Any]:
    return {
        "source_key": key,
        "publisher": f"Synthetic {key}",
        "url": f"https://{key}.example.org/",
        "source_type": "magazine",
        "retrieval": "fetched",
        "value": {"tide": tide},
        "used_agrees": agrees,
    }


# --------------------------------------------------------------------------- real files
def _committed() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    spots = json.loads((DEFAULT_DATA_DIR / "spots.json").read_text(encoding="utf-8"))
    raw = json.loads((DEFAULT_DATA_DIR / "profile_decisions.json").read_text(encoding="utf-8"))
    evidence = json.loads((DEFAULT_DATA_DIR / "research_evidence.json").read_text(encoding="utf-8"))
    parsed = parse_evidence(
        evidence, {s["slug"] for s in spots}, now=datetime(2026, 10, 13, tzinfo=UTC)
    )
    assessments: dict[str, dict[str, Any]] = {}
    for s in spots:
        spot = SurfSpot(slug=s["slug"])
        for f in SPOT_FIELDS:
            setattr(spot, f, s[f])
        sources = parsed.spots[s["slug"]].sources if s["slug"] in parsed.spots else ()
        assessments[s["slug"]] = compute_assessment(sources, profile_values(spot)).per_variable
    return spots, raw, {"assessments": assessments, "evidence": evidence}  # type: ignore[return-value]


def test_committed_decisions_are_valid_and_are_the_rules_decisions() -> None:
    spots, raw, extra = _committed()
    by_slug = {s["slug"]: s for s in spots}
    keys = {
        slug: {src["key"] for src in e["sources"]} for slug, e in extra["evidence"]["spots"].items()
    }
    assert validate_decisions(raw, by_slug, keys) == []
    records = raw["decisions"]
    decisions: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        decisions.setdefault(rec["slug"], []).append(rec)
    assessments = extra["assessments"]
    # Every contradicted field of the current assessment has a record, and every kept or
    # disputed record is what the rule gives today.
    normals = {}  # geometry is checked by the CLI against the database's shore normals
    issues = lint(by_slug, normals, assessments, decisions)
    assert issues == [], issues
    # A changed record is what the rule gave at the old value.
    changed = [rec for rec in records if rec["decision"] == "changed"]
    assert {r["field"] for r in changed} <= set(FIELD_OF_VARIABLE.values())
    for rec in changed:
        old_spot = dict(by_slug[rec["slug"]])
        if rec["field"] == "tide_preference":
            old_spot["tide_preference"] = rec["old_value"]
        elif rec["field"] == "wave_height_ft":
            old_spot["wave_height_min_ft"] = rec["old_value"]["min"]
            old_spot["wave_height_max_ft"] = rec["old_value"]["max"]
        spot = SurfSpot(slug=rec["slug"])
        for f in SPOT_FIELDS:
            setattr(spot, f, old_spot[f])
        sources = _sources_for(rec["slug"], spots, extra["evidence"])
        per_variable = compute_assessment(sources, profile_values(spot)).per_variable
        proposals = {
            p.field_name: p for p in propose_all(per_variable, profile_from_spot(old_spot))
        }
        assert proposals[rec["field"]].decision == "changed", rec["slug"]
        assert proposals[rec["field"]].new_value == rec["new_value"]


def _sources_for(slug: str, spots: list[dict[str, Any]], evidence: dict[str, Any]) -> Any:
    parsed = parse_evidence(
        evidence, {s["slug"] for s in spots}, now=datetime(2026, 10, 13, tzinfo=UTC)
    )
    return parsed.spots[slug].sources


def test_committed_decisions_cover_the_wind_conflicts_and_record_the_model_wind() -> None:
    _spots, raw, extra = _committed()
    wind_records = {
        r["slug"]: r
        for r in raw["decisions"]
        if r["field"] == "offshore_wind_direction" and r.get("reason") is None
    }
    for slug, per_variable in extra["assessments"].items():
        if per_variable["wind"]["independent_contradicting"]:
            assert slug in wind_records, slug
    for rec in wind_records.values():
        assert rec["decision"] in ("kept", "disputed")  # the evidence changed no wind
        assert "model_wind" in rec and rec["model_wind"]["hours"] > 0
        assert rec["model_wind"]["supports_change"] in (True, False, None)
        assert (
            "never changed on model wind" in rec["rationale"]
            or rec["model_wind"]["supports_change"] is not True
        )
        assert "Before -> after" in rec["rationale"]
