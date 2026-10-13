"""Research confidence, methodology "research-v1.2" (docs/research-confidence.md).

Research confidence says how well a break's ideal conditions are understood: how strong
and how independent the published evidence for each researched variable is, and whether
it agrees with the spot profile. It never looks at a forecast, it is not a probability and
it is never merged with forecast confidence.

Per variable (``RESEARCH_VARIABLES``):

* Sources are grouped into independent sources: two sources with the same publisher, or
  pages of the same web host, count once. A group's tier is the best tier among its
  sources (A: survey_data, peer_reviewed, official; B: guidebook, encyclopedia,
  surf_forecast_site, magazine; C: community). "official" means a government agency with
  a coastal or marine mandate, a national surfing reserve, or a government geological or
  hydrographic survey; tourism boards and World Surf League editorial pages are
  "magazine" (tier B).
* Agreement with the profile is recomputed from structured values
  (:mod:`app.services.research.values`); without one the researcher's flag is used.
  Supporting = agrees; for the descriptive variables (bathymetry, local_behavior) a
  claim without a judgement also supports. Contradicting = disagrees.
* High: >= 2 independent supporting sources including a tier-A one, or >= 3 of tier B or
  better. Moderate: >= 2 independent supporting sources, or one tier-A source, or one
  tier-B source whose value matches the profile exactly. Otherwise Low.
* A contradicting source of the same tier as the best supporting one is an unresolved
  conflict and lowers the class by one; one of a higher tier (or with no support at all)
  makes it Low; a lower-tier one is outweighed and only noted in the summary.
* Bathymetry is capped at Moderate without survey or peer-reviewed support. At
  sand-bottom breaks bathymetry and local behaviour drop one class unless a supporting
  source with a known year is at most 15 years older than the research (sandbars move).
* research-v1.1: a variable whose supporting sources were ALL read only as search
  summaries (``retrieval`` "web_search": the excerpt is a search tool's text about the
  page, not the page itself) is capped at Moderate (rule ``cap:search_only``), applied
  after the rules above; its summary then ends "(based on search summaries; source pages
  not read in full)". So overall High needs some directly read evidence.
* research-v1.2: swell-size values are compared bound by bound (values.compare_value):
  a stated maximum more than 25 % above the profile's maximum, or a stated minimum more
  than 25 % below its minimum, contradicts. Everything else is unchanged from v1.1.

Overall: Low if swell direction or wind is Low or 4+ variables are Low; High only if
swell direction, size and wind are High and none of tide, bathymetry and local behaviour
is Low; otherwise Moderate. The number of filled-in profile fields plays no part.

Assessments are history: a new row is inserted (and the previous one marked not
current) only when the evidence fingerprint or the methodology version changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SpotResearchAssessment, SpotResearchClaim, SpotResearchSource, SurfSpot
from app.models.research import RESEARCH_VARIABLES, SOURCE_TYPES
from app.services.research.evidence import (
    EVIDENCE_FILENAME,
    EvidenceClaim,
    EvidenceSource,
    load_evidence,
    sync_evidence,
)
from app.services.research.values import (
    DESCRIPTIVE_VARIABLES,
    compare_value,
    profile_values,
)

logger = logging.getLogger(__name__)

METHODOLOGY_VERSION = "research-v1.2"

HIGH, MODERATE, LOW = "high", "moderate", "low"
_DOWNGRADE = {HIGH: MODERATE, MODERATE: LOW, LOW: LOW}

TIER_A, TIER_B, TIER_C = 0, 1, 2  # lower is stronger
TIER_NAMES = {TIER_A: "A", TIER_B: "B", TIER_C: "C"}
SOURCE_TIERS: dict[str, int] = {
    "survey_data": TIER_A,
    "peer_reviewed": TIER_A,
    "official": TIER_A,
    "guidebook": TIER_B,
    "encyclopedia": TIER_B,
    "surf_forecast_site": TIER_B,
    "magazine": TIER_B,
    "community": TIER_C,
}
MEASURED_BATHYMETRY_TYPES = frozenset({"survey_data", "peer_reviewed"})
RECENCY_VARIABLES = frozenset({"bathymetry", "local_behavior"})
RECENCY_MAX_AGE_YEARS = 15
SEARCH_ONLY_RETRIEVAL = "web_search"
SEARCH_ONLY_NOTE = "(based on search summaries; source pages not read in full)"
MAX_OVERALL_GAPS = 4

VARIABLE_LABELS: dict[str, str] = {
    "swell_direction": "Swell direction",
    "swell_size": "Swell size",
    "swell_period": "Swell period",
    "wind": "Wind",
    "tide": "Tide",
    "bathymetry": "Bottom / bathymetry",
    "seasonality": "Season",
    "local_behavior": "How the wave breaks",
}
# "Sources disagree about <topic>."
_TOPIC: dict[str, str] = {
    "swell_direction": "the best swell direction",
    "swell_size": "the wave size this break works in",
    "swell_period": "the swell period this break needs",
    "wind": "the best wind direction",
    "tide": "the best tide",
    "bathymetry": "the shape of the bottom",
    "seasonality": "the best season",
    "local_behavior": "how the wave breaks",
}
# "... do not support this break's listed <noun>."
_LISTED: dict[str, str] = {
    "swell_direction": "swell direction",
    "swell_size": "size range",
    "swell_period": "swell period",
    "wind": "offshore wind direction",
    "tide": "tide preference",
    "bathymetry": "description of the bottom",
    "seasonality": "best months",
    "local_behavior": "description of the wave",
}
NO_EVIDENCE_GAPS: dict[str, str] = {
    "swell_direction": (
        "Limited information is available about which swell directions work best at this break."
    ),
    "swell_size": "Limited information is available about the wave sizes this break works in.",
    "swell_period": "Limited information is available about the swell period this break needs.",
    "wind": "Limited information is available about which winds are offshore at this break.",
    "tide": "Limited information is available about this break's preferred tide conditions.",
    "bathymetry": "Limited information is available about the shape of the bottom at this break.",
    "seasonality": "Limited information is available about this break's best season.",
    "local_behavior": "Limited information is available about how this wave breaks.",
}
TYPE_PHRASES: dict[str, str] = {
    "survey_data": "survey data",
    "peer_reviewed": "a peer-reviewed study",
    "official": "an official publication",
    "guidebook": "a published surf guide",
    "encyclopedia": "an encyclopedia entry",
    "surf_forecast_site": "a forecast site's spot guide",
    "magazine": "a surf magazine",
    "community": "a community source",
}
# Order in which per-variable gaps are promoted to the overall gaps (most important first).
OVERALL_GAP_ORDER = (
    "swell_direction",
    "wind",
    "swell_size",
    "tide",
    "bathymetry",
    "local_behavior",
    "swell_period",
    "seasonality",
)
_CONFLICT_RULES = frozenset({"conflict:same_tier", "conflict:stronger", "conflict:unsupported"})


@dataclass(frozen=True)
class Judgement:
    """Agreement of one claim with the profile.

    ``computed`` comes from the structured value (None: no value, or nothing in the
    profile to compare with); ``researcher`` is the flag in the evidence file; ``used`` is
    what the methodology applies: the computed result when the claim has a value,
    otherwise the researcher's flag.
    """

    computed: bool | None
    researcher: bool | None
    used: bool | None
    basis: str  # "computed" | "researcher"
    exact: bool


def judge_claim(claim: EvidenceClaim, profile: dict[str, Any]) -> Judgement:
    if claim.value is not None:
        result = compare_value(claim.variable, claim.value, profile)
        return Judgement(result.agrees, claim.agrees, result.agrees, "computed", result.exact)
    return Judgement(None, claim.agrees, claim.agrees, "researcher", False)


def _host(url: str | None) -> str:
    host = (urlsplit(url).hostname or "") if url else ""
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def publisher_groups(items: Sequence[tuple[str, str, str | None]]) -> dict[str, int]:
    """(key, publisher, url) -> key: group id; same publisher (case/space-insensitive) or
    the same web host (ignoring a leading www. / m.) = one group."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple[str, str], int] = {}
    for i, (_key, publisher, url) in enumerate(items):
        for kind, value in (
            ("publisher", " ".join(publisher.casefold().split())),
            ("host", _host(url)),
        ):
            if not value:
                continue
            j = seen.setdefault((kind, value), i)
            parent[find(i)] = find(j)
    return {key: find(i) for i, (key, _publisher, _url) in enumerate(items)}


def independence_groups(sources: Sequence[EvidenceSource]) -> dict[str, int]:
    """source key -> group id; same publisher (case/space-insensitive) or host = one group."""
    return publisher_groups([(s.key, s.publisher, s.url) for s in sources])


@dataclass
class _SourceRole:
    source: EvidenceSource
    group: int
    supports: bool = False
    contradicts: bool = False
    exact: bool = False

    @property
    def tier(self) -> int:
        return SOURCE_TIERS[self.source.source_type]


def _group_tiers(roles: Iterable[_SourceRole]) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in roles:
        out[r.group] = min(out.get(r.group, TIER_C), r.tier)
    return out


def _count_phrase(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _iso(t: datetime | None) -> str | None:
    """ISO 8601 in UTC (the database may return another zone for the same instant)."""
    if t is None:
        return None
    return (t.astimezone(UTC) if t.tzinfo is not None else t.replace(tzinfo=UTC)).isoformat()


def assess_variable(
    variable: str,
    entries: Sequence[tuple[EvidenceSource, EvidenceClaim]],
    groups: dict[str, int],
    profile: dict[str, Any],
    reference_year: int | None,
) -> dict[str, Any]:
    """Classify one variable from its claims (see the module docstring for the rules)."""
    descriptive = variable in DESCRIPTIVE_VARIABLES
    roles: dict[str, _SourceRole] = {}
    evidence: list[dict[str, Any]] = []
    flag_differences = 0
    for src, claim in entries:
        j = judge_claim(claim, profile)
        role = roles.setdefault(src.key, _SourceRole(src, groups[src.key]))
        supports = j.used is True or (descriptive and j.used is None)
        role.supports |= supports
        role.contradicts |= j.used is False
        role.exact |= supports and j.exact
        if j.basis == "computed" and j.computed != j.researcher:
            flag_differences += 1
        evidence.append(
            {
                "source_key": src.key,
                "title": src.title,
                "url": src.url,
                "publisher": src.publisher,
                "source_type": src.source_type,
                "tier": TIER_NAMES[SOURCE_TIERS[src.source_type]],
                "published_year": src.published_year,
                "accessed_at": _iso(src.accessed_at),
                "retrieval": src.retrieval,
                "value": claim.value,
                "researcher_agrees": j.researcher,
                "computed_agrees": j.computed,
                "used_agrees": j.used,
                "basis": j.basis,
                "exact": j.exact,
            }
        )
    supporting = [r for r in roles.values() if r.supports]
    contradicting = [r for r in roles.values() if r.contradicts]
    not_comparable = [r for r in roles.values() if not (r.supports or r.contradicts)]
    sup_groups = _group_tiers(supporting)
    con_groups = _group_tiers(contradicting)
    n_sup, n_con = len(sup_groups), len(con_groups)
    best_sup = min(sup_groups.values()) if sup_groups else None
    best_con = min(con_groups.values()) if con_groups else None

    rules: list[str] = []
    if (n_sup >= 2 and best_sup == TIER_A) or sum(t <= TIER_B for t in sup_groups.values()) >= 3:
        base = HIGH
    elif n_sup >= 2 or best_sup == TIER_A or any(r.exact and r.tier == TIER_B for r in supporting):
        base = MODERATE
    else:
        base = LOW
    rules.append(f"base:{base}")
    cls = base
    if best_con is not None:
        if best_sup is None:
            rules.append("conflict:unsupported")
            cls = LOW
        elif best_con < best_sup:
            rules.append("conflict:stronger")
            cls = LOW
        elif best_con == best_sup:
            rules.append("conflict:same_tier")
            cls = _DOWNGRADE[cls]
        else:
            rules.append("conflict:outweighed")
    if (
        variable == "bathymetry"
        and cls == HIGH
        and not any(r.source.source_type in MEASURED_BATHYMETRY_TYPES for r in supporting)
    ):
        rules.append("cap:bathymetry_unmeasured")
        cls = MODERATE
    if variable in RECENCY_VARIABLES and profile.get("sand_bottom") and cls != LOW:
        years = [y for r in supporting if (y := r.source.published_year) is not None]
        # Without a research date the age of a source is unknown, and unknown never helps.
        if reference_year is None or not any(
            reference_year - y <= RECENCY_MAX_AGE_YEARS for y in years
        ):
            rules.append("recency:sand_bottom")
            cls = _DOWNGRADE[cls]
    # research-v1.1: evidence seen only through search summaries is at most Moderate.
    # Judged on the supporting sources when there are any, else on all sources (for the
    # summary note); a supporting source that was read directly lifts the cap.
    judged = supporting or list(roles.values())
    search_only = bool(judged) and all(r.source.retrieval == SEARCH_ONLY_RETRIEVAL for r in judged)
    if search_only and cls == HIGH:
        rules.append("cap:search_only")
        cls = MODERATE

    gaps: list[str] = []
    if not entries:
        gaps.append(NO_EVIDENCE_GAPS[variable])
    else:
        if "conflict:same_tier" in rules or "conflict:stronger" in rules:
            gaps.append(f"Sources disagree about {_TOPIC[variable]}.")
        elif "conflict:unsupported" in rules:
            gaps.append(
                f"The sources found do not support this break's listed {_LISTED[variable]}."
            )
        if "cap:bathymetry_unmeasured" in rules:
            gaps.append("No survey or peer-reviewed study of the bottom was found.")
        if "recency:sand_bottom" in rules:
            dated = [y for r in supporting if (y := r.source.published_year) is not None]
            if dated:
                gaps.append(
                    f"The newest dated source on {_TOPIC[variable]} is more than "
                    f"{RECENCY_MAX_AGE_YEARS} years old, and sandbars move."
                )
            else:
                # Most web sources carry no publication date: say that, not "too old".
                gaps.append(
                    f"No source on {_TOPIC[variable]} has a publication date, and sandbars "
                    "move, so the descriptions may be out of date."
                )
        if "cap:search_only" in rules:
            gaps.append(
                f"Sources on {_TOPIC[variable]} were seen only as search summaries, "
                "not read in full."
            )
        if not gaps and cls == LOW:
            profile_missing = (variable == "tide" and profile.get("tide_preference") is None) or (
                variable == "seasonality" and not profile.get("best_months")
            )
            gaps.append(
                f"This break has no listed {_LISTED[variable]} to compare the sources with."
                if profile_missing and not supporting
                else NO_EVIDENCE_GAPS[variable]
            )
        elif not gaps and cls == MODERATE:
            verb = "describe" if descriptive else "confirm"
            if n_sup == 1:
                gaps.append(f"Only one independent source {verb}s {_TOPIC[variable]}.")
            elif best_sup == TIER_C:
                gaps.append(f"Only community sources {verb} {_TOPIC[variable]}.")
            else:
                gaps.append(f"No survey, scientific or official source {verb}s {_TOPIC[variable]}.")

    strongest = (
        min((r.source.source_type for r in supporting), key=SOURCE_TYPES.index)
        if supporting
        else None
    )
    years_all = [y for r in roles.values() if (y := r.source.published_year) is not None]
    return {
        "class": cls,
        "label": VARIABLE_LABELS[variable],
        "sources": len(roles),
        "independent_sources": len({r.group for r in roles.values()}),
        "independent_supporting": n_sup,
        "independent_contradicting": n_con,
        "strongest_source_type": strongest,
        "agreement": {
            "supporting": len(supporting),
            "contradicting": len(contradicting),
            "not_comparable": len(not_comparable),
        },
        "newest_year": max(years_all) if years_all else None,
        "search_only": search_only,
        "summary": _variable_summary(
            variable, len(roles), n_sup, n_con, strongest, search_only=search_only
        ),
        "gaps": gaps,
        "rules": rules,
        "flag_differences": flag_differences,
        "evidence": evidence,
    }


def _variable_summary(
    variable: str,
    n_sources: int,
    n_sup: int,
    n_con: int,
    strongest: str | None,
    *,
    search_only: bool = False,
) -> str:
    """One plain sentence; with search-only evidence it ends with SEARCH_ONLY_NOTE."""
    if n_sources == 0:
        return "No sources found."
    verb = "Described by" if variable in DESCRIPTIVE_VARIABLES else "Supported by"
    disagree = "disagrees" if n_con == 1 else "disagree"
    phrase = TYPE_PHRASES.get(strongest or "", "a source")
    if n_sup >= 2:
        text = f"{verb} {n_sup} independent sources, including {phrase}"
    elif n_sup == 1:
        text = f"{verb} one source ({phrase})"
    elif n_con:
        text = f"No source supports the listed {_LISTED[variable]}"
    else:
        text = (
            f"Mentioned by {_count_phrase(n_sources, 'source', 'sources')}, without a value "
            "that can be checked"
        )
    if n_con:
        text += f"; {n_con} {disagree}"
    return f"{text} {SEARCH_ONLY_NOTE}" if search_only else text + "."


def overall_class(classes: dict[str, str]) -> str:
    lows = sum(c == LOW for c in classes.values())
    if classes["swell_direction"] == LOW or classes["wind"] == LOW or lows >= 4:
        return LOW
    if all(classes[v] == HIGH for v in ("swell_direction", "swell_size", "wind")) and not any(
        classes[v] == LOW for v in ("tide", "bathymetry", "local_behavior")
    ):
        return HIGH
    return MODERATE


def _overall_summary(cls: str, classes: dict[str, str], has_evidence: bool) -> str:
    if not has_evidence:
        return (
            "No research sources have been recorded for this break yet, so its listed ideal "
            "conditions are not independently confirmed."
        )
    if cls == HIGH:
        return "Independent sources confirm this break's best swell direction, size and wind."
    if cls == MODERATE:
        return (
            "Some of this break's ideal conditions are confirmed by sources; others rest on "
            "limited evidence."
        )
    core = [
        name
        for variable, name in (("swell_direction", "swell direction"), ("wind", "wind"))
        if classes[variable] == LOW
    ]
    if core:
        return f"Sources do not yet confirm this break's best {' or '.join(core)}."
    return "Most of this break's ideal conditions are not yet confirmed by sources."


def _overall_gaps(per_variable: dict[str, dict[str, Any]]) -> list[str]:
    conflicts: list[str] = []
    lows: list[str] = []
    others: list[str] = []
    for v in OVERALL_GAP_ORDER:
        entry = per_variable[v]
        if not entry["gaps"]:
            continue
        first = entry["gaps"][0]
        if _CONFLICT_RULES & set(entry["rules"]):
            conflicts.append(first)
        elif entry["class"] == LOW:
            lows.append(first)
        else:
            others.append(first)
    return list(dict.fromkeys(conflicts + lows + others))[:MAX_OVERALL_GAPS]


def _canonical(obj: Any) -> Any:
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int | float):
        return round(float(obj), 6)
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_canonical(v) for v in obj]
    return str(obj)


def evidence_fingerprint(
    sources: Sequence[EvidenceSource], profile: dict[str, Any], reference_year: int | None
) -> str:
    """sha256 of the methodology version, the evidence and the profile values compared."""
    payload = {
        "methodology": METHODOLOGY_VERSION,
        "profile": profile,
        "reference_year": reference_year,
        "sources": sorted(
            (
                {
                    "key": s.key,
                    "title": s.title,
                    "url": s.url,
                    "publisher": s.publisher,
                    "source_type": s.source_type,
                    "published_year": s.published_year,
                    "accessed_at": s.accessed_at,
                    "retrieval": s.retrieval,
                    "claims": sorted(
                        (
                            {
                                "variable": c.variable,
                                "claim": c.claim,
                                "value": c.value,
                                "excerpt": c.excerpt,
                                "agrees": c.agrees,
                            }
                            for c in s.claims
                        ),
                        key=lambda c: json.dumps(_canonical(c), sort_keys=True),
                    ),
                }
                for s in sources
            ),
            key=lambda s: str(s["key"]),
        ),
    }
    text = json.dumps(
        _canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ComputedAssessment:
    overall_class: str
    per_variable: dict[str, dict[str, Any]]
    gaps: list[str]
    summary: str
    evidence_fingerprint: str
    research_updated_at: datetime | None
    methodology_version: str


def compute_assessment(
    sources: Sequence[EvidenceSource], profile: dict[str, Any]
) -> ComputedAssessment:
    """Pure research-v1.2 assessment of one spot from its evidence and profile values.

    The age of evidence is measured against the year of the newest ``accessed_at`` (when
    the research was done), not the clock, so an assessment only changes when the
    evidence, the profile or the methodology does.
    """
    accessed = [s.accessed_at for s in sources if s.accessed_at is not None]
    research_updated_at = max(accessed) if accessed else None
    reference_year = research_updated_at.year if research_updated_at else None
    groups = independence_groups(sources)
    per_variable: dict[str, dict[str, Any]] = {}
    for variable in RESEARCH_VARIABLES:
        entries = [(s, c) for s in sources for c in s.claims if c.variable == variable]
        per_variable[variable] = assess_variable(variable, entries, groups, profile, reference_year)
    classes = {v: e["class"] for v, e in per_variable.items()}
    cls = overall_class(classes)
    return ComputedAssessment(
        overall_class=cls,
        per_variable=per_variable,
        gaps=_overall_gaps(per_variable),
        summary=_overall_summary(cls, classes, bool(sources)),
        evidence_fingerprint=evidence_fingerprint(sources, profile, reference_year),
        research_updated_at=research_updated_at,
        methodology_version=METHODOLOGY_VERSION,
    )


def stored_evidence(
    db: Session, spot_ids: Sequence[int] | None = None
) -> dict[int, list[EvidenceSource]]:
    """Evidence in the database, per spot (two queries for any number of spots)."""
    src_q = select(SpotResearchSource).order_by(SpotResearchSource.id)
    claim_q = select(SpotResearchClaim).order_by(SpotResearchClaim.id)
    if spot_ids is not None:
        src_q = src_q.where(SpotResearchSource.spot_id.in_(spot_ids))
        claim_q = claim_q.where(SpotResearchClaim.spot_id.in_(spot_ids))
    claims: dict[int, list[EvidenceClaim]] = {}
    for c in db.scalars(claim_q).all():
        claims.setdefault(c.source_id, []).append(
            EvidenceClaim(
                variable=c.variable,
                claim=c.claim,
                value=c.value,
                excerpt=c.excerpt,
                agrees=c.agrees,
            )
        )
    out: dict[int, list[EvidenceSource]] = {}
    for s in db.scalars(src_q).all():
        out.setdefault(s.spot_id, []).append(
            EvidenceSource(
                key=s.source_key,
                title=s.title,
                url=s.url,
                publisher=s.publisher,
                source_type=s.source_type,
                published_year=s.published_year,
                accessed_at=s.accessed_at,
                notes=s.notes,
                claims=tuple(claims.get(s.id, [])),
                retrieval=s.retrieval,
            )
        )
    return out


def current_assessment(db: Session, spot_id: int) -> SpotResearchAssessment | None:
    return db.scalar(
        select(SpotResearchAssessment).where(
            SpotResearchAssessment.spot_id == spot_id, SpotResearchAssessment.is_current
        )
    )


def assess_spot(
    db: Session,
    spot: SurfSpot,
    force: bool = False,
    sources: Sequence[EvidenceSource] | None = None,
) -> tuple[SpotResearchAssessment, bool]:
    """Assess one spot; returns (current assessment, whether a new row was inserted).

    A new row is inserted only when the fingerprint or methodology changed (or ``force``);
    the previous current row is then marked not current in the same transaction. Rows
    are never updated otherwise or deleted. Flushes; the caller commits.
    """
    # Serialise concurrent assessments of one spot (one current row per spot).
    db.execute(select(SurfSpot.id).where(SurfSpot.id == spot.id).with_for_update())
    if sources is None:
        sources = stored_evidence(db, [spot.id]).get(spot.id, [])
    computed = compute_assessment(sources, profile_values(spot))
    current = current_assessment(db, spot.id)
    if (
        current is not None
        and not force
        and current.methodology_version == computed.methodology_version
        and current.evidence_fingerprint == computed.evidence_fingerprint
    ):
        return current, False
    if current is not None:
        current.is_current = False
        db.flush()  # before the insert, so the one-current-row index never trips
    row = SpotResearchAssessment(
        spot_id=spot.id,
        methodology_version=computed.methodology_version,
        overall_class=computed.overall_class,
        per_variable=computed.per_variable,
        gaps=computed.gaps,
        summary=computed.summary,
        evidence_fingerprint=computed.evidence_fingerprint,
        research_updated_at=computed.research_updated_at,
        is_current=True,
    )
    db.add(row)
    db.flush()
    return row, True


def assess_all(db: Session, slug: str | None = None, force: bool = False) -> dict[str, int]:
    """Assess every spot (or one ``slug``); flushes, the caller commits."""
    query = select(SurfSpot).order_by(SurfSpot.slug)
    if slug is not None:
        query = query.where(SurfSpot.slug == slug)
    spots = db.scalars(query).all()
    evidence = stored_evidence(db, [s.id for s in spots] if slug is not None else None)
    counts = {"assessed": 0, "created": 0, "unchanged": 0, HIGH: 0, MODERATE: 0, LOW: 0}
    for spot in spots:
        row, created = assess_spot(db, spot, force=force, sources=evidence.get(spot.id, []))
        counts["assessed"] += 1
        counts["created" if created else "unchanged"] += 1
        counts[row.overall_class] += 1
    return counts


def seed_research(db: Session, data_dir: str | Path) -> dict[str, int]:
    """Load the evidence file of ``data_dir`` (if any) and assess every spot (no commit)."""
    evidence = load_evidence(db, Path(data_dir) / EVIDENCE_FILENAME)
    synced = sync_evidence(db, evidence)
    assessed = assess_all(db)
    logger.info("Research confidence: %s", assessed)
    return {
        "research_evidence_spots": synced["evidence_spots"],
        "research_sources_created": synced["sources_created"],
        "research_sources_removed": synced["sources_removed"],
        "research_assessments_created": assessed["created"],
    }
