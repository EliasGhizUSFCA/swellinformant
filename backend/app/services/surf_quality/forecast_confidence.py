"""Forecast confidence and local confidence per forecast hour, methodology "forecast-v1.2".

Two classes (High / Moderate / Low), each with short plain-language reasons
(docs/forecast-confidence.md has every threshold with the evidence behind it):

* **Forecast confidence**: how trustworthy the OFFSHORE forecast (swell and wind) is at
  this hour. It starts from the class of the 0-100 confidence index
  (``uncertainty.confidence_index``: High >= 70, Moderate >= 45) and applies documented
  caps and downgrades::

      class = max(Low, min(index class, every cap that fired) - number of downgrades)

  so a rule can only lower the class. The one conditional is the lead-time rule for
  3-7 days, which allows High only when an ensemble spread exists and is small; it is a
  cap of Moderate otherwise and never lifts the index class.
* **Local confidence**: how trustworthy our BREAKING-wave prediction at this break is.
  It can be no better than the forecast and no better than how well the break is
  understood: ``min(forecast class, research basis)`` with research basis
  ``min(swell_direction, swell_size, max(bathymetry, local_behavior))`` from the spot's
  current research assessment (app.services.research); no assessment gives Low.
* **Disputed wind** (forecast-v1.1, a note that changes no class): when the spot's
  current research assessment fires a conflict rule on wind, or its profile decision
  record says the offshore wind direction is "disputed"
  (app.services.research.decisions.wind_dispute), the hour carries the reason
  WIND_DISPUTE_REASON and a ``wind_dispute`` entry in its stored inputs (the profile and
  the main contradicting offshore direction and the wind relation under each). Forecast
  and local classes are computed exactly as in forecast-v1.
* **Wind direction disagreement** (forecast-v1.2): when the blend flags that two
  independent families blow from directions more than 60 degrees apart, both at 10 km/h or
  more (``blend.WIND_DIRECTION_FLAG_*``), forecast confidence is at most Moderate, like the
  other disagreement caps. It fires only with two independent families, so single-source
  classes are those of forecast-v1.

Research confidence itself is a separate assessment (docs/research-confidence.md) that is
stored and shown on its own; only the documented ``min`` above connects the two. None of
these classes is a calibrated probability, and agreement between models that share NCEP
forcing (GFS-Wave, GEFS-Wave) never counts as independent confirmation: only families
that ``families.are_independent`` separates count, and agreement never raises a class.

Everything here is pure (no database); ``confidence_context`` gathers the inputs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.forecasts.families import are_independent

# forecast-v1.1 = forecast-v1 plus the disputed-wind note (no class changes); forecast-v1.2
# adds the blend's wind-direction disagreement cap (two independent families only).
METHOD = "forecast-v1.2"

HIGH, MODERATE, LOW = "high", "moderate", "low"
_RANK = {LOW: 0, MODERATE: 1, HIGH: 2}
_BY_RANK = {v: k for k, v in _RANK.items()}

# Index class boundaries (= confidence.confidence_label).
INDEX_HIGH = 70
INDEX_MODERATE = 45

# Lead time (valid time - issue time of the forecast run), hours.
LEAD_SHORT_H = 72.0  # up to 3 days: no lead-time cap
LEAD_MEDIUM_H = 168.0  # beyond 7 days: at most Moderate
LEAD_LONG_H = 240.0  # beyond 10 days: Low (no skill against climatology at 240 h)

# GEFS-Wave relative spread (Hs spread / Hs mean). Typical unfiltered values in the
# 51-cycle analysis: 0.098 / 0.181 / 0.245 / 0.313 / 0.392 at days 1 / 3 / 5 / 7 / 10.
SPREAD_SMALL = 0.15  # tighter than a typical 3-day ensemble
SPREAD_LARGE = 0.30  # as wide as a typical 7-day ensemble: downgrade one class
SPREAD_VERY_LARGE = 0.40  # as wide as a typical 10-day ensemble: Low
ENSEMBLE_MIN_MEAN_M = 0.05  # below this spread / mean is meaningless (flat sea)

# Disagreement between independent model families (blend comparisons).
HS_DISAGREE_LOG_RATIO = math.log(1.4)  # = blend.HS_LOG_RATIO_FLAG: at most Moderate
HS_STRONG_DISAGREE_LOG_RATIO = math.log(2.0)  # a factor of two apart: Low
HS_AGREE_LOG_RATIO = math.log(1.15)  # within 15 %: "agree" (a reason only)

# Run-to-run change of offshore Hs at this valid time (heuristic, a weak error predictor).
RUN_CHANGE_RATIO = 1.4
RUN_CHANGE_FLOOR_M = 0.5  # small seas: ratios below this height are not meaningful

# Verification against buoys (forecast_accuracy_metrics, Hs).
VERIFICATION_POOR_SI = 0.5
VERIFICATION_MIN_SKILL_VS_PERSISTENCE = 0.0

# Display names of model families in reasons.
FAMILY_NAMES = {
    "ncep_gfs": "GFS",
    "ncep_gefs": "GEFS",
    "ecmwf_ifs": "ECMWF",
    "ecmwf_aifs": "ECMWF AIFS",
    "mfwam": "MFWAM",
    "dwd_icon": "DWD",
}

# Research variables behind local confidence (app.models.research.RESEARCH_VARIABLES).
RESEARCH_BASIS_VARIABLES = ("swell_direction", "swell_size", "bathymetry", "local_behavior")
NO_RESEARCH_REASON = "How this break responds to swell is not well documented yet."
RESEARCH_REASONS = {
    LOW: NO_RESEARCH_REASON,
    MODERATE: "How this break responds to swell is only partly documented.",
    HIGH: "How this break responds to swell is well documented.",
}
STALE_REASON = "Forecast data is out of date; a newer model run is overdue."
WIND_DISPUTE_REASON = "Sources disagree about which wind is offshore here."
# A note (no class change) that fires on every hour while no buoy near the spot has been
# checked. It is kept in the stored reasons and the deprecated mixed list, but not repeated
# in ``forecast_confidence_reasons``: the method note says it once (docs/forecast-confidence.md).
NOT_VERIFIED_REASON = "Not yet checked against buoys near this spot."
MAX_REASON_CHARS = 90
# Reasons that never belong among forecast-confidence reasons: the research sentences of
# local confidence and the disputed-wind note.
NON_FORECAST_REASONS = frozenset({*RESEARCH_REASONS.values(), WIND_DISPUTE_REASON})


def index_class(index: float) -> str:
    """Class of the 0-100 confidence index (an index, not a probability)."""
    if index >= INDEX_HIGH:
        return HIGH
    if index >= INDEX_MODERATE:
        return MODERATE
    return LOW


def lower(a: str, b: str) -> str:
    """The lower of two classes."""
    return a if _RANK[a] <= _RANK[b] else b


def higher(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def rank(cls: str) -> int:
    return _RANK[cls]


# --------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class EnsembleSignal:
    """GEFS-Wave statistics at this hour (relative spread = Hs spread / Hs mean)."""

    hs_mean_m: float | None
    hs_spread_m: float | None
    members: int | None = None

    @property
    def rel_spread(self) -> float | None:
        if self.hs_mean_m is None or self.hs_spread_m is None:
            return None
        if self.hs_mean_m <= ENSEMBLE_MIN_MEAN_M:
            return None
        return self.hs_spread_m / self.hs_mean_m


@dataclass(frozen=True)
class Disagreement:
    """Largest differences between INDEPENDENT families at this hour (blend comparison)."""

    hs_log_ratio: float | None = None
    tp_diff_s: float | None = None
    direction_diff_deg: float | None = None
    wind_diff_kmh: float | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verification:
    """The verification row used for this source and lead bucket (Hs against buoys)."""

    scope: str  # "station" (a buoy paired with the spot), "region" or "global"
    scope_key: str
    source_code: str
    lead_bucket_h: int
    n: int
    scatter_index: float | None
    skill_vs_persistence: float | None
    near_spot: bool

    @property
    def poor(self) -> bool:
        si, ss = self.scatter_index, self.skill_vs_persistence
        return (si is not None and si > VERIFICATION_POOR_SI) or (
            ss is not None and ss <= VERIFICATION_MIN_SKILL_VS_PERSISTENCE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.scope_key,
            "source": self.source_code,
            "bucket_h": self.lead_bucket_h,
            "n": self.n,
            "si": _r(self.scatter_index),
            "ss_persistence": _r(self.skill_vs_persistence),
            "near_spot": self.near_spot,
            "poor": self.poor,
        }


@dataclass(frozen=True)
class ForecastInputs:
    """Everything forecast confidence looks at for one spot and hour."""

    index: int  # the 0-100 confidence index of this hour
    sigma_source: str  # what set the index's spread: lead_table / ensemble_spread / ...
    lead_hours: float
    source_code: str
    is_demo: bool = False
    # Deterministic model families behind this hour (one for a single source, the blend's
    # members otherwise). Ensembles are not listed: they are a spread, not a second model.
    families: tuple[str, ...] = ()
    ensemble: EnsembleSignal | None = None
    disagreement: Disagreement | None = None
    # max(Hs_now, Hs_prev) / max(min(Hs_now, Hs_prev), RUN_CHANGE_FLOOR_M) for this valid
    # time against the previous run of the same source; None without a previous run.
    run_change_ratio: float | None = None
    # Verification rows of the families behind this hour (empty = none exists).
    verification: tuple[Verification, ...] = ()
    missing_wind: bool = False
    missing_swell: bool = False  # swell (or bulk wave) period or direction missing
    stale: bool = False
    stale_after_hours: float | None = None


def independent_families(families: Iterable[str]) -> list[str]:
    """The largest set of mutually independent families, greedily in the given order.

    GFS-Wave and GEFS-Wave share NCEP forcing and physics, so together they count once;
    ``unknown`` (a model mix) never counts as independent evidence.
    """
    chosen: list[str] = []
    for fam in dict.fromkeys(families):
        if fam != "unknown" and all(are_independent(fam, c) for c in chosen):
            chosen.append(fam)
    return chosen


def independent_family_count(families: Iterable[str]) -> int:
    return len(independent_families(families))


def run_change_ratio(current_hs: float | None, previous_hs: float | None) -> float | None:
    if current_hs is None or previous_hs is None:
        return None
    hi, lo = max(current_hs, previous_hs), min(current_hs, previous_hs)
    return hi / max(lo, RUN_CHANGE_FLOOR_M)


# --------------------------------------------------------------------------- result
@dataclass(frozen=True)
class Rule:
    """One rule that fired: its effect on the class and the reason shown to users."""

    rule: str
    effect: str  # "cap:<class>", "downgrade" or "note"
    reason: str
    priority: int  # higher = more important (reasons are ordered by it)

    def as_str(self) -> str:
        return f"{self.effect}:{self.rule}"


@dataclass
class ForecastConfidence:
    cls: str
    index_class: str
    reasons: list[str]
    rules: list[Rule]
    inputs: ForecastInputs

    def inputs_dict(self) -> dict[str, Any]:
        inp = self.inputs
        ens = inp.ensemble
        dis = inp.disagreement
        return {
            "index": inp.index,
            "index_class": self.index_class,
            "sigma_source": inp.sigma_source,
            "lead_h": round(inp.lead_hours, 1),
            "source": inp.source_code,
            "demo": inp.is_demo,
            "families": list(inp.families),
            "independent_families": independent_family_count(inp.families),
            "ensemble": None
            if ens is None
            else {"rel_spread": _r(ens.rel_spread), "members": ens.members},
            "disagreement": None
            if dis is None
            else {
                "hs_log_ratio": _r(dis.hs_log_ratio),
                "tp_diff_s": _r(dis.tp_diff_s, 1),
                "dir_diff_deg": _r(dis.direction_diff_deg, 0),
                "wind_diff_kmh": _r(dis.wind_diff_kmh, 1),
                "flags": list(dis.flags),
            },
            "run_change_ratio": _r(inp.run_change_ratio),
            "verification": [v.as_dict() for v in inp.verification],
            "missing": [
                name
                for name, flag in (("swell", inp.missing_swell), ("wind", inp.missing_wind))
                if flag
            ],
            "stale": inp.stale,
            "stale_after_h": _r(inp.stale_after_hours, 1),
        }


@dataclass(frozen=True)
class ResearchBasisInput:
    """The spot's current research assessment as local confidence uses it."""

    assessment_id: int | None
    overall_class: str
    variable_classes: dict[str, str] = field(default_factory=dict)
    # The assessment's wind entry (rules and evidence), for the disputed-wind note only;
    # it plays no part in the local class.
    wind: dict[str, Any] | None = None


@dataclass(frozen=True)
class LocalConfidence:
    cls: str
    reason: str
    research_basis: str
    research_class: str | None  # overall research class of the assessment (None = none)
    assessment_id: int | None
    variable_classes: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "reason": self.reason,
            "basis": self.research_basis,
            "research_class": self.research_class,
            "assessment_id": self.assessment_id,
            "variables": dict(self.variable_classes),
        }


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _whole_days(hours: float) -> int:
    return int(hours / 24.0 + 0.5)  # conventional rounding (2.5 days -> 3)


def forecast_phrase(lead_hours: float) -> str:
    """'A 7-day forecast' from the lead time (valid time - issue time).

    Stored reasons describe the forecast's range, not the distance from "now": they are
    shown long after scoring, and a blend's lead counts from its oldest member.
    """
    if lead_hours < 18.0:
        return "A forecast less than a day ahead"
    n = _whole_days(lead_hours)
    article = "An" if str(n).startswith("8") or n in (11, 18) else "A"
    return f"{article} {n}-day forecast"


def lead_phrase(hours_ahead: float | None) -> str | None:
    """'about 6 days away' for an email, from the time until the hour (at sending)."""
    if hours_ahead is None:
        return None
    if hours_ahead < 18.0:
        return "less than a day away"
    n = _whole_days(hours_ahead)
    return f"about {n} day{'s' if n != 1 else ''} away"


def _pair_name(families: Sequence[str]) -> str:
    """'GFS and ECMWF' for two independent families, 'Independent models' otherwise."""
    order = list(FAMILY_NAMES)
    ordered = sorted(families, key=lambda f: order.index(f) if f in order else len(order))
    names = [FAMILY_NAMES.get(f, f) for f in independent_families(ordered)]
    return " and ".join(names) if len(names) == 2 else "Independent models"


# --------------------------------------------------------------------------- forecast
def classify_forecast(inp: ForecastInputs) -> ForecastConfidence:
    """Forecast-v1 class, reasons (most important first) and the rules that fired."""
    rules: list[Rule] = []

    def cap(rule: str, to: str, reason: str, priority: int) -> None:
        rules.append(Rule(rule, f"cap:{to}", reason, priority))

    def downgrade(rule: str, reason: str, priority: int) -> None:
        rules.append(Rule(rule, "downgrade", reason, priority))

    def note(rule: str, reason: str, priority: int) -> None:
        rules.append(Rule(rule, "note", reason, priority))

    base = index_class(inp.index)
    lead = inp.lead_hours
    span = forecast_phrase(lead)
    rel = inp.ensemble.rel_spread if inp.ensemble is not None else None
    independent = independent_family_count(inp.families)
    dis = inp.disagreement
    pair = _pair_name(inp.families)

    # Source reliability.
    if inp.is_demo:
        cap("demo", LOW, "Demo data, not a real forecast.", 100)

    # Missing variables.
    if inp.missing_swell:
        cap("missing_swell", LOW, "Swell period or direction missing for this hour.", 95)
    if inp.missing_wind:
        cap("missing_wind", MODERATE, "Wind data missing for this hour.", 50)

    # Lead time.
    if lead > LEAD_LONG_H:
        cap("lead_over_10d", LOW, f"{span} — too far ahead to be dependable.", 90)
    elif lead > LEAD_MEDIUM_H:
        cap("lead_7_10d", MODERATE, f"{span} — forecasts this far out often change.", 70)
    elif lead > LEAD_SHORT_H:
        if rel is None:
            cap("lead_3_7d_no_ensemble", MODERATE, f"{span} with no ensemble data to check it.", 65)
        elif rel > SPREAD_SMALL:
            cap(
                "lead_3_7d_spread_not_small",
                MODERATE,
                f"{span}, and ensemble members do not agree closely.",
                65,
            )
        else:
            note("lead_3_7d", f"{span}.", 20)
    else:
        note("lead_short", f"{span}.", 20)
        if rel is None:
            note("no_ensemble", "No ensemble data for this hour.", 12)

    # Single deterministic source beyond 3 days (an ensemble of the same chain is not a
    # second, independent model).
    if lead > LEAD_SHORT_H and independent < 2 and not inp.is_demo:
        cap(
            "single_source_over_3d",
            MODERATE,
            "Only one independent forecast model is available this far out.",
            60,
        )

    # Ensemble spread.
    if rel is not None:
        if rel > SPREAD_VERY_LARGE:
            cap("spread_very_large", LOW, "Ensemble members disagree widely on swell size.", 85)
        elif rel > SPREAD_LARGE:
            downgrade("spread_large", "Ensemble members disagree on swell size.", 80)
        elif rel <= SPREAD_SMALL:
            note("spread_small", "Ensemble members agree closely.", 5)
        elif base != HIGH:
            note("spread_moderate", "Ensemble members differ somewhat on swell size.", 30)

    # Disagreement between independent families only.
    if dis is not None and independent >= 2:
        ratio = dis.hs_log_ratio
        if ratio is not None and ratio > HS_STRONG_DISAGREE_LOG_RATIO:
            cap("hs_strong_disagree", LOW, f"{pair} disagree strongly on swell size.", 88)
        elif ratio is not None and ratio > HS_DISAGREE_LOG_RATIO:
            cap("hs_disagree", MODERATE, f"{pair} disagree on swell size.", 75)
        if "period_disagree" in dis.flags:
            cap("period_disagree", MODERATE, f"{pair} disagree on swell period.", 56)
        if "direction_disagree" in dis.flags:
            cap("direction_disagree", MODERATE, f"{pair} disagree on swell direction.", 55)
        if "wind_direction_disagree" in dis.flags:
            cap(
                "wind_direction_disagree",
                MODERATE,
                f"{pair} disagree on the wind direction.",
                53,
            )
        if "wind_disagree" in dis.flags:
            cap("wind_disagree", MODERATE, f"{pair} disagree on the wind.", 52)
        if (
            ratio is not None
            and ratio <= HS_AGREE_LOG_RATIO
            and not any(f.endswith("_disagree") for f in dis.flags)
        ):
            note("independent_agree", f"{pair} agree on swell size.", 6)
    if dis is not None and any(f.startswith("same_family_mismatch") for f in dis.flags):
        cap(
            "same_family_mismatch",
            MODERATE,
            "Two copies of the same model differ here; one data feed may be faulty.",
            48,
        )

    # Run-to-run consistency (heuristic: a weak predictor of error).
    if inp.run_change_ratio is not None and inp.run_change_ratio > RUN_CHANGE_RATIO:
        downgrade("run_change", "Forecast changed a lot since the previous model run.", 45)

    # Verification against buoys.
    poor = [v for v in inp.verification if v.poor]
    if poor:
        cap("verification_poor", MODERATE, "Recent checks against buoys show large errors.", 40)
    if not any(v.near_spot for v in inp.verification):
        note("not_verified_near_spot", NOT_VERIFIED_REASON, 10)

    # Staleness (per source, queries.stale_after_hours).
    if inp.stale:
        cap("stale", MODERATE, STALE_REASON, 35)

    # Why the index itself is below High, when no rule above says so (beyond 7 days the
    # lead-time rule already does).
    if base != HIGH and inp.sigma_source == "lead_table" and lead <= LEAD_MEDIUM_H:
        size = "moderate" if base == MODERATE else "large"
        note("index_lead_table", f"Typical forecast error at this lead time is {size}.", 30)
    elif (
        base != HIGH
        and inp.sigma_source == "model_disagreement"
        and not any(r.rule.startswith("hs_") for r in rules)
    ):
        note("index_model_spread", "Forecast models differ on swell size.", 30)

    cls = base
    for r in rules:
        if r.effect.startswith("cap:"):
            cls = lower(cls, r.effect.split(":", 1)[1])
    downgrades = sum(1 for r in rules if r.effect == "downgrade")
    cls = _BY_RANK[max(0, _RANK[cls] - downgrades)]

    ordered = sorted(rules, key=lambda r: -r.priority)
    reasons = list(dict.fromkeys(r.reason[:MAX_REASON_CHARS] for r in ordered))
    return ForecastConfidence(cls, base, reasons, rules, inp)


# --------------------------------------------------------------------------- local
def research_input(assessment: Any | None) -> ResearchBasisInput | None:
    """From a SpotResearchAssessment row (or None)."""
    if assessment is None:
        return None
    per_variable = assessment.per_variable or {}
    classes = {
        v: str(per_variable[v].get("class", LOW))
        for v in RESEARCH_BASIS_VARIABLES
        if isinstance(per_variable.get(v), dict)
    }
    wind = per_variable.get("wind")
    return ResearchBasisInput(
        assessment.id,
        assessment.overall_class,
        classes,
        wind=wind if isinstance(wind, dict) else None,
    )


def research_basis(classes: dict[str, str]) -> str:
    """min(swell_direction, swell_size, max(bathymetry, local_behavior)); missing = Low."""

    def get(v: str) -> str:
        c = classes.get(v, LOW)
        return c if c in _RANK else LOW

    return lower(
        lower(get("swell_direction"), get("swell_size")),
        higher(get("bathymetry"), get("local_behavior")),
    )


def classify_local(forecast_cls: str, research: ResearchBasisInput | None) -> LocalConfidence:
    """Local confidence = min(forecast class, research basis); no assessment -> Low."""
    if research is None:
        return LocalConfidence(LOW, NO_RESEARCH_REASON, LOW, None, None, {})
    basis = research_basis(research.variable_classes)
    return LocalConfidence(
        lower(forecast_cls, basis),
        RESEARCH_REASONS[basis],
        basis,
        research.overall_class,
        research.assessment_id,
        dict(research.variable_classes),
    )


# --------------------------------------------------------------------------- storage
def wind_dispute_inputs(
    dispute: Any, relation: str, relation_alternative: str | None
) -> dict[str, Any]:
    """The ``wind_dispute`` entry of the stored inputs for one hour.

    ``dispute`` is a research.decisions.WindDispute; ``relation`` the hour's wind relation
    under the profile's offshore direction, ``relation_alternative`` under the main
    contradicting direction (None when that direction is unknown).
    """
    return {
        **dispute.as_dict(),
        "relation": relation,
        "relation_alternative": relation_alternative,
        "differs": relation_alternative is not None and relation_alternative != relation,
        "reason": WIND_DISPUTE_REASON,
    }


def confidence_inputs(
    fc: ForecastConfidence,
    local: LocalConfidence,
    wind_dispute: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON stored on predictions and archive rows (every input and every rule).

    ``wind_dispute`` (forecast-v1.1, from :func:`wind_dispute_inputs`) is stored only when
    sources disagree about the offshore wind; it changes neither class.
    """
    out = {
        "method": METHOD,
        **fc.inputs_dict(),
        "rules": [r.as_str() for r in fc.rules],
        "class": fc.cls,
        "reasons": fc.reasons,
        "local": local.as_dict(),
    }
    if wind_dispute is not None:
        out["wind_dispute"] = wind_dispute
    return out


def stored_reasons(inputs: dict[str, Any] | None) -> list[str]:
    """Forecast reasons first, then the disputed-wind note (if any), then the local
    (research) reason, from stored inputs. This is the deprecated mixed list
    (``confidence_reasons``); :func:`forecast_reasons` and :func:`wind_dispute_note` give
    the parts separately."""
    if not inputs:
        return []
    out = [str(r) for r in inputs.get("reasons") or []]
    dispute_reason = wind_dispute_note(inputs)
    if dispute_reason and dispute_reason not in out:
        out.append(dispute_reason)
    local_reason = (inputs.get("local") or {}).get("reason")
    if local_reason and local_reason not in out:
        out.append(str(local_reason))
    return out


def forecast_only(reasons: Iterable[str]) -> list[str]:
    """The forecast-confidence reasons of a list: without the research sentences, the
    disputed-wind note and the constant not-verified note (order kept, duplicates dropped).
    Used for stored inputs and for older event snapshots that only kept the mixed list."""
    skip = NON_FORECAST_REASONS | {NOT_VERIFIED_REASON}
    return list(dict.fromkeys(str(r) for r in reasons if r and str(r) not in skip))


def forecast_reasons(inputs: dict[str, Any] | None) -> list[str]:
    """Forecast-confidence reasons of one hour from its stored inputs: the forecast rules
    only (lead time, model agreement, data, freshness), most important first."""
    if not inputs:
        return []
    return forecast_only(inputs.get("reasons") or [])


def wind_dispute_note(inputs: dict[str, Any] | None) -> str | None:
    """The disputed-wind note stored with the hour (forecast-v1.1), else None."""
    reason = ((inputs or {}).get("wind_dispute") or {}).get("reason")
    return str(reason) if reason else None


def local_reason(
    forecast_cls: str | None, local_cls: str | None, inputs: dict[str, Any] | None
) -> str | None:
    """The single reason local confidence is lower than forecast confidence, else None.

    Local confidence is min(forecast class, research basis), so when it is lower the
    research basis is what holds it back and the stored local reason (the research
    sentence) is the reason. None as well when the stored inputs have no local reason.
    """
    if forecast_cls not in _RANK or local_cls not in _RANK:
        return None
    if _RANK[local_cls] >= _RANK[forecast_cls]:
        return None
    reason = ((inputs or {}).get("local") or {}).get("reason")
    return str(reason) if reason else None


def cap_for_staleness(fields: dict[str, Any]) -> dict[str, Any]:
    """The staleness rule applied at read time to API confidence fields (``api_fields``,
    or an event's stored peak-hour fields): a run that has gone stale since it was scored
    is shown at most Moderate, local confidence follows (it is a min), the staleness reason
    comes first and the local reason stays only while local is still the lower class.
    ``forecast_confidence_reasons`` (when present) then starts with STALE_REASON as well.
    Fields without a forecast class (scored before forecast-v1) are returned unchanged."""
    fc_cls = fields.get("forecast_confidence_class")
    if fc_cls not in _RANK:
        return fields
    capped = lower(fc_cls, MODERATE)
    local_cls = fields.get("local_confidence_class")
    if local_cls in _RANK:
        local_cls = lower(local_cls, capped)
    reasons = list(fields.get("confidence_reasons") or [])
    if STALE_REASON not in reasons:
        reasons = [STALE_REASON, *reasons]
    reason = fields.get("local_confidence_reason")
    if local_cls not in _RANK or _RANK[local_cls] >= _RANK[capped]:
        reason = None
    out = {
        **fields,
        "forecast_confidence_class": capped,
        "local_confidence_class": local_cls,
        "confidence_reasons": reasons,
        "local_confidence_reason": reason,
    }
    if "forecast_confidence_reasons" in fields:
        rest = [r for r in fields["forecast_confidence_reasons"] or [] if r != STALE_REASON]
        out["forecast_confidence_reasons"] = [STALE_REASON, *rest]
    return out


def api_fields(prediction: Any, *, stale: bool = False) -> dict[str, Any]:
    """forecast/local class, reasons and lead for the API, from a stored prediction.

    ``forecast_confidence_reasons`` holds the forecast rules' reasons only; the research
    sentence is ``local_confidence_reason`` (when local is the lower class) and the
    disputed-wind note is ``wind_dispute_note``. ``confidence_reasons`` keeps the deprecated
    mixed list. ``stale`` applies the staleness rule at read time (:func:`cap_for_staleness`).
    """
    fc_cls = prediction.forecast_confidence_class
    local_cls = prediction.local_confidence_class
    inputs = prediction.confidence_inputs
    fields = {
        "forecast_confidence_class": fc_cls,
        "local_confidence_class": local_cls,
        "confidence_reasons": stored_reasons(inputs),
        "forecast_confidence_reasons": forecast_reasons(inputs),
        "wind_dispute_note": wind_dispute_note(inputs),
        "lead_hours": None if prediction.lead_hours is None else round(prediction.lead_hours, 1),
        "local_confidence_reason": local_reason(fc_cls, local_cls, inputs),
    }
    return cap_for_staleness(fields) if stale else fields
