"""Profile decisions: when research evidence may change a spot profile (ADR-018).

``data/surf_spots/profile_decisions.json`` holds one record per spot and profile field
whose current research assessment has contradicting evidence (and, for the geometry lint,
reasons why an offshore wind far from the shore normal is right). A record says whether
the profile value was ``changed``, ``kept`` or is ``disputed``, which evidence keys the
decision rests on and why. The rule (``propose``) is applied mechanically:

* **changed** only when at least two independent publishers (methodology independence:
  same publisher or web host = one) whose pages were read directly (``retrieval``
  "fetched") state values that contradict the profile and agree with one new value V
  within the methodology's agreement tolerances, and every directly read source that
  supports the current value is of a strictly lower tier than BOTH of those publishers
  (a same-tier supporter makes the contradiction an unresolved conflict, not a
  resolution). V is the value stated by one of those sources (never an average);
* **kept** when no directly read source contradicts (only search summaries do), when the
  directly read contradicting sources are of a lower tier than the directly read
  supporting ones, or when a single publisher contradicts while two or more directly read
  publishers of the same or a higher tier support the profile;
* **disputed** otherwise.

The main contradicting value (``alternative``) is the value stated by a contradicting
source that the most independent contradicting publishers agree with (ties: directly
read first, then the better tier, then the most central value, then the largest change).
Model output never decides anything here; the wind records only report it.

Assumptions: tiers and agreement are those of :mod:`app.services.research.assess` and
:mod:`app.services.research.values`; claims without a structured value can contradict
(researcher flag false) but never propose a value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.services.geo import angular_diff
from app.services.research.assess import SOURCE_TIERS, TIER_C, publisher_groups
from app.services.research.values import TIDE_ORDER, compare_value
from app.services.seed import SeedValidationError, resolve_data_dir

DECISIONS_FILENAME = "profile_decisions.json"
FORMAT_VERSION = 1
DECISIONS = ("changed", "kept", "disputed")
FETCHED = "fetched"
# Profile field of each researched variable that decisions cover, and its spots.json keys.
FIELD_OF_VARIABLE: dict[str, str] = {
    "wind": "offshore_wind_direction",
    "swell_direction": "optimal_swell_direction",
    "swell_size": "wave_height_ft",
    "tide": "tide_preference",
    "seasonality": "best_months",
}
VARIABLE_OF_FIELD = {f: v for v, f in FIELD_OF_VARIABLE.items()}
# Reason codes of geometry notes (records that explain an offshore wind far from the shore
# normal rather than apply the rule):
# * wave_face: at a point or wrapping reef (break type point, or a directly read source
#   describing the wave wrapping around a point, headland or reef) the offshore wind is
#   for the wave face, and a directly read source supports the profile's direction;
# * stated_orientation: a directly read source or a measurement gives the break's
#   orientation, and the profile's offshore wind is straight offshore for it.
WAVE_FACE = "wave_face"
STATED_ORIENTATION = "stated_orientation"
RECORD_REASONS = frozenset({WAVE_FACE, STATED_ORIENTATION})
# research lint: offshore wind more than this from (shore normal + 180) needs a reason.
GEOMETRY_LINT_DEG = 60.0
RECORD_KEYS_REQUIRED = frozenset(
    {"slug", "field", "old_value", "new_value", "decision", "sources", "rationale", "decided_at"}
)
RECORD_KEYS = RECORD_KEYS_REQUIRED | {"alternative", "reason", "sensitivity", "model_wind"}


class DecisionValidationError(SeedValidationError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


# --------------------------------------------------------------------------- values
def field_value(spot: dict[str, Any], field_name: str) -> Any:
    """The current profile value of a decision field, from a spots.json-like mapping."""
    if field_name == "offshore_wind_direction":
        return float(spot["offshore_wind_direction"])
    if field_name == "optimal_swell_direction":
        return {
            "min": float(spot["optimal_swell_direction_min"]),
            "max": float(spot["optimal_swell_direction_max"]),
        }
    if field_name == "wave_height_ft":
        return {"min": float(spot["wave_height_min_ft"]), "max": float(spot["wave_height_max_ft"])}
    if field_name == "tide_preference":
        tide = spot.get("tide_preference")
        return str(tide) if tide else None
    if field_name == "best_months":
        return sorted(int(m) for m in spot.get("best_months") or [])
    raise ValueError(f"unknown decision field {field_name!r}")


def apply_value(profile: dict[str, Any], field_name: str, value: Any) -> dict[str, Any]:
    """``profile`` (values.profile_values shape) with one decision field replaced."""
    out = dict(profile)
    if field_name == "offshore_wind_direction":
        out["offshore_wind_direction"] = float(value)
    elif field_name == "optimal_swell_direction":
        out["optimal_swell_direction_min"] = float(value["min"])
        out["optimal_swell_direction_max"] = float(value["max"])
    elif field_name == "wave_height_ft":
        out["wave_height_min_ft"] = float(value["min"])
        out["wave_height_max_ft"] = float(value["max"])
    elif field_name == "tide_preference":
        out["tide_preference"] = value
    elif field_name == "best_months":
        out["best_months"] = sorted(value)
    else:
        raise ValueError(f"unknown decision field {field_name!r}")
    return out


def profile_field(profile: dict[str, Any], field_name: str) -> Any:
    """A decision field read from a values.profile_values mapping."""
    if field_name == "optimal_swell_direction":
        return {
            "min": profile["optimal_swell_direction_min"],
            "max": profile["optimal_swell_direction_max"],
        }
    if field_name == "wave_height_ft":
        return {"min": profile["wave_height_min_ft"], "max": profile["wave_height_max_ft"]}
    if field_name == "offshore_wind_direction":
        return profile["offshore_wind_direction"]
    if field_name == "tide_preference":
        return profile.get("tide_preference")
    return sorted(profile.get("best_months") or [])


def candidate_value(
    variable: str, value: dict[str, Any], profile: dict[str, Any]
) -> tuple[Any, bool] | None:
    """(the profile value a claim proposes, whether it could be adopted as one).

    Wind and tide propose the stated value; a size claim replaces only the bound(s) it
    contradicts; a direction window needs two different bounds to define a new window
    (a single direction is a candidate for comparison but cannot be adopted); months
    propose the stated set.
    """
    if variable == "wind":
        return float(value["offshore_from_deg"]), True
    if variable == "tide":
        return str(value["tide"]), True
    if variable == "seasonality":
        return sorted(int(m) for m in value["months"]), True
    if variable == "swell_direction":
        lo = value.get("min", value.get("max"))
        hi = value.get("max", value.get("min"))
        if lo is None or hi is None:
            return None
        return {"min": float(lo), "max": float(hi)}, ("min" in value and "max" in value) and (
            angular_diff(float(lo), float(hi)) > 0
        )
    if variable == "swell_size":
        lo, hi = value.get("min_ft"), value.get("max_ft")
        new_lo, new_hi = float(profile["wave_height_min_ft"]), float(profile["wave_height_max_ft"])
        if lo is not None and compare_value(variable, {"min_ft": lo}, profile).agrees is False:
            new_lo = float(lo)
        if hi is not None and compare_value(variable, {"max_ft": hi}, profile).agrees is False:
            new_hi = float(hi)
        if new_lo >= new_hi:
            return None
        return {"min": new_lo, "max": new_hi}, True
    return None


def value_distance(field_name: str, a: Any, b: Any) -> float:
    """How far apart two values of a decision field are (for the "most central" tie-break)."""
    if field_name == "offshore_wind_direction":
        return angular_diff(float(a), float(b))
    if field_name == "optimal_swell_direction":
        return angular_diff(a["min"], b["min"]) + angular_diff(a["max"], b["max"])
    if field_name == "wave_height_ft":
        return abs(a["min"] - b["min"]) + abs(a["max"] - b["max"])
    if field_name == "tide_preference":
        if a == b:
            return 0.0
        if "all" in (a, b) or a is None or b is None:
            return float(len(TIDE_ORDER))
        return float(abs(TIDE_ORDER.index(a) - TIDE_ORDER.index(b)))
    return float(len(set(a) ^ set(b)))


# --------------------------------------------------------------------------- evidence
@dataclass(frozen=True)
class Entry:
    """One claim as the current assessment judged it (assessment ``evidence`` entries)."""

    source_key: str
    publisher: str
    url: str | None
    tier: int
    retrieval: str
    value: dict[str, Any] | None
    agrees: bool | None  # the methodology's used agreement

    @classmethod
    def from_assessment(cls, e: dict[str, Any]) -> Entry:
        return cls(
            source_key=str(e["source_key"]),
            publisher=str(e.get("publisher") or ""),
            url=e.get("url"),
            tier=SOURCE_TIERS.get(str(e.get("source_type")), TIER_C),
            retrieval=str(e.get("retrieval") or FETCHED),
            value=e.get("value"),
            agrees=e.get("used_agrees"),
        )


def entries_of(per_variable: dict[str, Any], variable: str) -> list[Entry]:
    entry = (per_variable or {}).get(variable) or {}
    return [Entry.from_assessment(e) for e in entry.get("evidence") or []]


def _groups(entries: Sequence[Entry]) -> dict[str, int]:
    seen: dict[str, tuple[str, str, str | None]] = {}
    for e in entries:
        seen.setdefault(e.source_key, (e.source_key, e.publisher, e.url))
    return publisher_groups(list(seen.values()))


def _group_tiers(entries: Iterable[Entry], groups: dict[str, int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for e in entries:
        g = groups[e.source_key]
        out[g] = min(out.get(g, TIER_C), e.tier)
    return out


@dataclass
class Candidate:
    value: Any
    adoptable: bool
    source_keys: tuple[str, ...]  # sources stating it
    agreeing_keys: tuple[str, ...]  # contradicting sources whose value agrees with it
    fetched_tiers: tuple[int, ...]  # tier per independent directly read agreeing publisher
    all_groups: int
    centrality: float
    own_tier: int
    change: float


@dataclass
class Proposal:
    variable: str
    field_name: str
    decision: str
    reason: str
    old_value: Any
    new_value: Any | None
    alternative: Any | None
    candidate: Candidate | None
    supporting_keys: list[str] = field(default_factory=list)
    contradicting_keys: list[str] = field(default_factory=list)
    blocking_keys: list[str] = field(default_factory=list)


def _rank_candidates(
    variable: str, entries: Sequence[Entry], profile: dict[str, Any], groups: dict[str, int]
) -> list[Candidate]:
    fname = FIELD_OF_VARIABLE[variable]
    old = profile_field(profile, fname)
    contradicting = [e for e in entries if e.agrees is False]
    with_value = [e for e in contradicting if e.value is not None]
    proposals: dict[str, tuple[Any, bool, list[str], int]] = {}
    for e in with_value:
        assert e.value is not None
        cand = candidate_value(variable, e.value, profile)
        if cand is None:
            continue
        key = json.dumps(cand[0], sort_keys=True)
        prev = proposals.get(key)
        keys = [*prev[2], e.source_key] if prev else [e.source_key]
        tier = min(prev[3], e.tier) if prev else e.tier
        proposals[key] = (cand[0], cand[1] or (prev[1] if prev else False), keys, tier)
    out: list[Candidate] = []
    for value, adoptable, keys, own_tier in proposals.values():
        hyp = apply_value(profile, fname, value)
        agreeing = [
            e
            for e in with_value
            if e.value is not None and compare_value(variable, e.value, hyp).agrees is True
        ]
        fetched = [e for e in agreeing if e.retrieval == FETCHED]
        tiers = sorted(_group_tiers(fetched, groups).values())
        centrality = 0.0
        for e in agreeing:
            assert e.value is not None
            c = candidate_value(variable, e.value, profile)
            if c is not None:
                centrality += value_distance(fname, value, c[0])
        out.append(
            Candidate(
                value=value,
                adoptable=adoptable,
                source_keys=tuple(dict.fromkeys(keys)),
                agreeing_keys=tuple(dict.fromkeys(e.source_key for e in agreeing)),
                fetched_tiers=tuple(tiers),
                all_groups=len(_group_tiers(agreeing, groups)),
                centrality=round(centrality, 6),
                own_tier=own_tier,
                change=value_distance(fname, value, old) if old is not None else math.inf,
            )
        )
    out.sort(
        key=lambda c: (
            -len(c.fetched_tiers),
            -c.all_groups,
            c.fetched_tiers[0] if c.fetched_tiers else TIER_C + 1,
            c.centrality,
            c.own_tier,
            -c.change,
            json.dumps(c.value, sort_keys=True),
        )
    )
    return out


def propose(variable: str, entries: Sequence[Entry], profile: dict[str, Any]) -> Proposal | None:
    """The rule's decision for one variable, or None without contradicting evidence."""
    fname = FIELD_OF_VARIABLE[variable]
    old = profile_field(profile, fname)
    contradicting = [e for e in entries if e.agrees is False]
    if not contradicting:
        return None
    groups = _groups(entries)
    supporting = [e for e in entries if e.agrees is True]
    sup_f = _group_tiers([e for e in supporting if e.retrieval == FETCHED], groups)
    con_f = _group_tiers([e for e in contradicting if e.retrieval == FETCHED], groups)
    ranked = _rank_candidates(variable, entries, profile, groups)
    best = ranked[0] if ranked else None
    proposal = Proposal(
        variable=variable,
        field_name=fname,
        decision="kept",
        reason="",
        old_value=old,
        new_value=None,
        alternative=best.value if best else None,
        candidate=best,
        supporting_keys=sorted({e.source_key for e in supporting}),
        contradicting_keys=sorted({e.source_key for e in contradicting}),
    )
    if best is not None and len(best.fetched_tiers) >= 2:
        second = best.fetched_tiers[1]
        blockers = sorted(
            {
                e.source_key
                for e in supporting
                if e.retrieval == FETCHED and e.tier <= second  # same or higher tier
            }
        )
        proposal.blocking_keys = blockers
        if not blockers and best.adoptable:
            proposal.decision, proposal.reason = "changed", "criterion_met"
            proposal.new_value = best.value
            return proposal
    if not con_f:
        proposal.reason = "search_only_contradiction"
    elif sup_f and min(con_f.values()) > min(sup_f.values()):
        proposal.reason = "outweighed_by_direct_support"
    elif len(con_f) == 1 and sum(t <= min(con_f.values()) for t in sup_f.values()) >= 2:
        proposal.reason = "single_publisher_contradicts"
    else:
        proposal.decision = "disputed"
        if best is None:
            proposal.reason = "no_value_to_adopt"
        elif len(best.fetched_tiers) < 2:
            proposal.reason = "fewer_than_two_direct_publishers_agree"
        elif proposal.blocking_keys:
            proposal.reason = "direct_support_of_the_same_or_higher_tier"
        else:
            proposal.reason = "single_direction_does_not_define_a_window"
    return proposal


def propose_all(per_variable: dict[str, Any], profile: dict[str, Any]) -> list[Proposal]:
    out = []
    for variable in FIELD_OF_VARIABLE:
        p = propose(variable, entries_of(per_variable, variable), profile)
        if p is not None:
            out.append(p)
    return out


# --------------------------------------------------------------------------- disputes
WIND_CONFLICT_RULES = frozenset({"conflict:same_tier", "conflict:stronger", "conflict:unsupported"})


@dataclass(frozen=True)
class WindDispute:
    """Sources disagree about which wind is offshore at a spot."""

    profile_offshore_deg: float | None
    alternative_offshore_deg: float | None
    basis: tuple[str, ...]  # assessment conflict rules and/or "decision:disputed"
    source_keys: tuple[str, ...]  # contradicting sources

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_offshore_deg": self.profile_offshore_deg,
            "alternative_offshore_deg": self.alternative_offshore_deg,
            "basis": list(self.basis),
            "sources": list(self.source_keys),
        }


def wind_dispute(
    per_variable: dict[str, Any] | None,
    profile_offshore_deg: float | None,
    record: dict[str, Any] | None = None,
) -> WindDispute | None:
    """The spot's wind dispute: its current assessment fires a conflict rule on wind, or
    its decision record for the offshore wind direction says "disputed"; None otherwise.

    The alternative direction is the main contradicting value of the assessment (see the
    module docstring), else the decision record's ``alternative``.
    """
    wind = (per_variable or {}).get("wind") or {}
    basis = [r for r in wind.get("rules") or [] if r in WIND_CONFLICT_RULES]
    if record is not None and record.get("decision") == "disputed":
        basis.append("decision:disputed")
    if not basis:
        return None
    entries = entries_of(per_variable or {}, "wind")
    alternative: float | None = None
    keys: tuple[str, ...] = ()
    if profile_offshore_deg is not None and entries:
        profile = {"offshore_wind_direction": float(profile_offshore_deg)}
        ranked = _rank_candidates("wind", entries, profile, _groups(entries))
        if ranked:
            alternative = float(ranked[0].value)
            keys = ranked[0].agreeing_keys
    if alternative is None and record is not None and record.get("alternative") is not None:
        alternative = float(record["alternative"])
        keys = tuple(record.get("sources") or ())
    return WindDispute(profile_offshore_deg, alternative, tuple(basis), keys)


# --------------------------------------------------------------------------- the file
def default_decisions_path(data_dir: str | Path | None = None) -> Path:
    return resolve_data_dir(data_dir) / DECISIONS_FILENAME


def validate_decisions(
    raw: Any,
    spots: dict[str, dict[str, Any]] | None = None,
    evidence_keys: dict[str, set[str]] | None = None,
) -> list[str]:
    """Every problem with a decoded decisions file (empty = valid).

    With ``spots`` (slug -> spots.json entry) the slugs and the current values are checked:
    a "changed" record's new value, and a "kept" or "disputed" record's old value, must be
    the profile's current value. With ``evidence_keys`` every cited source must exist.
    """
    problems: list[str] = []
    if not isinstance(raw, dict) or raw.get("version") != FORMAT_VERSION:
        return [f"profile decisions: top level must be an object with version {FORMAT_VERSION}"]
    records = raw.get("decisions")
    if not isinstance(records, list):
        return ["profile decisions: decisions must be a list"]
    seen: set[tuple[str, str, str | None]] = set()
    for i, rec in enumerate(records):
        where = f"decision {i + 1}"
        if not isinstance(rec, dict):
            problems.append(f"{where}: must be an object")
            continue
        missing = sorted(RECORD_KEYS_REQUIRED - set(rec))
        extra = sorted(set(rec) - RECORD_KEYS)
        if missing:
            problems.append(f"{where}: missing fields {missing}")
            continue
        if extra:
            problems.append(f"{where}: unknown fields {extra}")
        slug, fname = rec["slug"], rec["field"]
        where = f"decision {i + 1} ({slug}/{fname})"
        if fname not in VARIABLE_OF_FIELD:
            problems.append(f"{where}: field must be one of {sorted(VARIABLE_OF_FIELD)}")
            continue
        if rec["decision"] not in DECISIONS:
            problems.append(f"{where}: decision must be one of {list(DECISIONS)}")
        reason = rec.get("reason")
        if reason is not None and reason not in RECORD_REASONS:
            problems.append(f"{where}: reason must be one of {sorted(RECORD_REASONS)}")
        key = (slug, fname, reason)
        if key in seen:
            problems.append(f"{where}: duplicate record")
        seen.add(key)
        if (rec["decision"] == "changed") != (rec["new_value"] is not None):
            problems.append(f"{where}: new_value is set exactly when the decision is changed")
        if not isinstance(rec["rationale"], str) or not rec["rationale"].strip():
            problems.append(f"{where}: rationale must be a non-empty string")
        try:
            date.fromisoformat(str(rec["decided_at"]))
        except ValueError:
            problems.append(f"{where}: decided_at must be a date such as 2026-10-13")
        sources = rec["sources"]
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            problems.append(f"{where}: sources must be a list of evidence keys")
            sources = []
        if spots is not None:
            spot = spots.get(slug)
            if spot is None:
                problems.append(f"{where}: unknown spot slug")
                continue
            current = field_value(spot, fname)
            expected = rec["new_value"] if rec["decision"] == "changed" else rec["old_value"]
            if not _same(current, expected):
                problems.append(
                    f"{where}: the profile's current value {current!r} is not the record's "
                    f"{'new' if rec['decision'] == 'changed' else 'old'} value {expected!r}"
                )
        if evidence_keys is not None:
            unknown = sorted(set(sources) - evidence_keys.get(slug, set()))
            if unknown:
                problems.append(f"{where}: unknown evidence keys {unknown}")
    return problems


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), abs_tol=1e-6)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return sorted(a) == sorted(b)
    return bool(a == b)


def load_decisions(path: str | Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """slug -> decision records; an empty mapping when the file does not exist.

    Raises :class:`DecisionValidationError` when the file is malformed (format only; the
    profile and evidence cross-checks run in ``research lint`` and the tests).
    """
    try:
        file = Path(path) if path is not None else default_decisions_path()
    except FileNotFoundError:
        return {}
    if not file.exists():
        return {}
    raw = json.loads(file.read_text(encoding="utf-8"))
    problems = validate_decisions(raw)
    if problems:
        raise DecisionValidationError(problems)
    out: dict[str, list[dict[str, Any]]] = {}
    for rec in raw["decisions"]:
        out.setdefault(rec["slug"], []).append(rec)
    return out


def record_for(
    decisions: dict[str, list[dict[str, Any]]], slug: str, field_name: str
) -> dict[str, Any] | None:
    """The rule-outcome record (not a geometry note) of one spot field, if any."""
    for rec in decisions.get(slug, []):
        if rec["field"] == field_name and rec.get("reason") is None:
            return rec
    return None


# --------------------------------------------------------------------------- lint
@dataclass(frozen=True)
class LintIssue:
    slug: str
    kind: str  # "geometry" | "missing_record" | "rule_differs" | "file"
    message: str


def geometry_gap(offshore_deg: float, shore_normal_deg: float) -> float:
    """Degrees between the offshore wind and straight offshore (shore normal + 180)."""
    return angular_diff(offshore_deg, (shore_normal_deg + 180.0) % 360.0)


def lint(
    spots: dict[str, dict[str, Any]],
    shore_normals: dict[str, float],
    assessments: dict[str, dict[str, Any]],
    decisions: dict[str, list[dict[str, Any]]],
    max_gap_deg: float = GEOMETRY_LINT_DEG,
) -> list[LintIssue]:
    """``research lint`` (pure): every issue, sorted by spot.

    * geometry: the offshore wind is more than ``max_gap_deg`` from the shore normal + 180
      and no decision record (rule outcome or geometry note) covers the offshore wind;
    * missing_record: the current assessment has contradicting evidence for a decision
      field without a rule-outcome record;
    * rule_differs: a kept or disputed record whose decision the rule no longer gives
      (the evidence or the profile changed since it was decided).

    ``spots``: slug -> spots.json-like mapping; ``shore_normals``: slug -> the shore normal
    the model uses; ``assessments``: slug -> current assessment ``per_variable``.
    """
    issues: list[LintIssue] = []
    for slug in sorted(spots):
        spot = spots[slug]
        records = decisions.get(slug, [])
        normal = shore_normals.get(slug)
        offshore = float(spot["offshore_wind_direction"])
        if normal is not None:
            gap = geometry_gap(offshore, normal)
            covered = any(r["field"] == "offshore_wind_direction" for r in records)
            if gap > max_gap_deg and not covered:
                issues.append(
                    LintIssue(
                        slug,
                        "geometry",
                        f"offshore wind {offshore:g} deg is {gap:.0f} deg from straight "
                        f"offshore ({(normal + 180) % 360:.0f} deg, shore normal {normal:g}) "
                        "with no recorded reason",
                    )
                )
        per_variable = assessments.get(slug)
        if per_variable is None:
            continue
        profile = profile_from_spot(spot)
        proposals = {p.field_name: p for p in propose_all(per_variable, profile)}
        for fname, p in proposals.items():
            rec = record_for(decisions, slug, fname)
            if rec is None:
                issues.append(
                    LintIssue(
                        slug,
                        "missing_record",
                        f"{fname}: contradicting evidence without a decision record "
                        f"(the rule says {p.decision})",
                    )
                )
            elif rec["decision"] != "changed" and rec["decision"] != p.decision:
                issues.append(
                    LintIssue(
                        slug,
                        "rule_differs",
                        f"{fname}: the record says {rec['decision']}, the rule now says "
                        f"{p.decision} ({p.reason})",
                    )
                )
    return issues


def profile_from_spot(spot: dict[str, Any]) -> dict[str, Any]:
    """The values.profile_values fields a decision compares, from a spots.json mapping."""
    return {
        "optimal_swell_direction_min": float(spot["optimal_swell_direction_min"]),
        "optimal_swell_direction_max": float(spot["optimal_swell_direction_max"]),
        "offshore_wind_direction": float(spot["offshore_wind_direction"]),
        "tide_preference": str(spot["tide_preference"]) if spot.get("tide_preference") else None,
        "wave_height_min_ft": float(spot["wave_height_min_ft"]),
        "wave_height_max_ft": float(spot["wave_height_max_ft"]),
        "min_swell_period_s": float(spot["min_swell_period_s"]),
        "ideal_swell_period_s": float(spot["ideal_swell_period_s"]),
        "best_months": sorted(int(m) for m in spot.get("best_months") or []),
    }
