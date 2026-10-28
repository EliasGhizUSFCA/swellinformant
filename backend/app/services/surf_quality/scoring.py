"""Deterministic, documented surf quality score (0–100).

Each component maps measurable inputs to [0, 1]; the score is their weighted mean
(weights renormalised over the components whose inputs exist) multiplied by three gates
that stop flat, weak or blown-out conditions from scoring well. The weights are a reasoned
starting point, NOT statistically calibrated values, and may be overridden with
SCORING_WEIGHTS_JSON (``weights.py``). See docs/SURF_SCORING.md.

Algorithm 2.2.0 (scores equal 2.1.0's with the default weights):

* every hour also stores its main ``favourable`` and ``unfavourable`` conditions, short
  plain phrases from the component fits (``reasons.py``);
* an onshore or cross-onshore wind is at best "workable" in the wind's favourability class
  (a light onshore breeze below about 8.6 km/h was "favourable"); the score is unchanged;
* the component weights come from SCORING_WEIGHTS_JSON when it is set; the stored version
  then carries a tag of them (``weights.tagged_version``).

Algorithm 2.1.0 (this module and ``wind.py``):

* the wind fit is the continuous Q_wind of ``wind.py`` (offshore component V . n against the
  spot's seaward normal, chop from onshore and cross-shore components, strong offshore
  penalised, no floor), with separate strength and favourability classes;
* swell direction and period are scored over every wave partition, weighted by its share of
  the breaking energy flux ``Hb_i^2.5 / sum(Hb_j^2.5)``, so a well-aligned secondary swell
  that makes the surf is no longer scored by a blocked primary swell's direction;
* outside the swell window the direction fit tapers smoothly (``exp(-2 (d / 15)^2)``, the
  nearshore model's taper) instead of dropping to 0;
* consistency is the share of the breaking energy flux carried by swell (all three swell
  partitions), so wind sea the nearshore model sends away from the break no longer counts
  against it;
* the tide is scored only when the tide research class is moderate or high, the tide
  decision is not disputed and the spot has a preference; otherwise the reason is stored;
* every prediction stores the wind breakdown, the weights used and the points each factor
  and gate cost (``QualityResult.details``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.models.enums import QualityLabel
from app.services.geo import angular_diff, compass_label, window_distance
from app.services.surf_quality import wind as windmod
from app.services.surf_quality.breaking import estimate_breaking_height
from app.services.surf_quality.models import (
    BreakingEstimate,
    PartitionFlux,
    QualityResult,
    SeaState,
    SpotParams,
)
from app.services.surf_quality.nearshore import taper_transmission
from app.services.surf_quality.reasons import GOOD_FIT, POOR_FIT, condition_reasons
from app.services.surf_quality.weights import DEFAULT_WEIGHTS, tagged_version

# 2.0.0: breaking heights from the linear_wave_v2 nearshore model (energy-flux
# combination, directional spreading, shore normal); typical-to-sets ranges.
# 2.1.0: continuous offshore-wind fit (wind.py), partition-weighted direction and period,
# swell-window taper, flux-based consistency, gated tide, continuous spreading, stored
# contributions.
# 2.2.0: favourable / unfavourable conditions per hour, light onshore wind at best
# "workable", configurable weights (SCORING_WEIGHTS_JSON); scores unchanged by default.
ALGORITHM_VERSION = "2.2.0"

# The default component weights (weights.DEFAULT_WEIGHTS, the 2.1.0 constants).
WEIGHTS: dict[str, float] = dict(DEFAULT_WEIGHTS)


def model_version(weights: Mapping[str, float] | None = None) -> str:
    """The version stored with a score: ALGORITHM_VERSION, tagged when ``weights`` are not
    the defaults (``2.2.0+w1a2b3c4d``, weights.tagged_version)."""
    return tagged_version(ALGORITHM_VERSION, weights or DEFAULT_WEIGHTS)


# Lower bound (inclusive) of each label. Documented; not calibrated against observations.
LABEL_THRESHOLDS: list[tuple[int, QualityLabel]] = [
    (95, QualityLabel.EXCEPTIONAL),
    (85, QualityLabel.EXCELLENT),
    (72, QualityLabel.VERY_GOOD),
    (57, QualityLabel.GOOD),
    (40, QualityLabel.FAIR),
    (0, QualityLabel.POOR),
]

TIDE_TARGETS = {
    "low": 0.1,
    "low_to_mid": 0.3,
    "mid": 0.5,
    "mid_to_high": 0.7,
    "high": 0.9,
}

# The wind gate is 1 from this wind fit up and falls linearly to 0.5 at a fit of 0
# (heuristic). It starts where the wind stops being favourable (wind.FAVOURABLE_MIN_Q).
WIND_GATE_FULL_Q = windmod.FAVOURABLE_MIN_Q

# Tide research classes under which a tide preference is used for scoring.
TIDE_SCORED_CLASSES = frozenset({"moderate", "high"})
NO_TIDE_PREDICTION = "No tide prediction for this time."
NO_TIDE_PREFERENCE = "Tide preference not well established: no preference recorded."
TIDE_NOT_RESEARCHED = "Tide preference not well established: not assessed for this break."
TIDE_RESEARCH_LOW = "Tide preference not well established: research confidence is low."
TIDE_DISPUTED = "Tide preference not well established: sources disagree."
# Kinds of partition named in explanations (the primary swell is just "swell").
PARTITION_WORDS = {
    "primary_swell": "swell",
    "secondary_swell": "secondary swell",
    "tertiary_swell": "tertiary swell",
    "wind_sea": "wind sea",
    "combined": "swell",  # bulk parameters only, worded as in 2.0.0
}
SWELL_KINDS = frozenset({"primary_swell", "secondary_swell", "tertiary_swell"})


def label_for_score(score: float) -> QualityLabel:
    for threshold, label in LABEL_THRESHOLDS:
        if score >= threshold:
            return label
    return QualityLabel.POOR


def min_score_for_label(label: QualityLabel | str) -> int:
    for threshold, lab in LABEL_THRESHOLDS:
        if lab == label:
            return threshold
    raise ValueError(f"unknown label {label!r}")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _inside_direction_fit(direction: float, spot: SpotParams) -> float:
    d_opt = window_distance(
        direction, spot.optimal_swell_direction_min, spot.optimal_swell_direction_max
    )
    return _clamp(1.0 - d_opt / 45.0)


def score_direction(direction: float | None, spot: SpotParams) -> float | None:
    """1 inside the optimal range, linear to 0 at 45 deg from it; outside the swell window
    the value at the nearest window edge tapers with ``exp(-2 (d / 15)^2)``."""
    if direction is None:
        return None
    lo, hi = spot.swell_window_min, spot.swell_window_max
    outside = window_distance(direction, lo, hi)
    if outside > 0:
        edge = lo if angular_diff(direction, lo) <= angular_diff(direction, hi) else hi
        return _inside_direction_fit(edge, spot) * taper_transmission(outside)
    return _inside_direction_fit(direction, spot)


def score_period(period: float | None, spot: SpotParams) -> float | None:
    if period is None:
        return None
    p_min, p_ideal = spot.min_swell_period_s, spot.ideal_swell_period_s
    if period < p_min - 3:
        return 0.0
    if period < p_min:
        return 0.4 * (period - (p_min - 3)) / 3.0
    if period < p_ideal:
        return 0.4 + 0.6 * (period - p_min) / max(p_ideal - p_min, 0.5)
    return 1.0


def score_size(height_ft: float, spot: SpotParams) -> float:
    lo, hi = spot.wave_height_min_ft, spot.wave_height_max_ft
    sweet = lo + 0.65 * (hi - lo)
    if height_ft <= 0.4 * lo:
        return 0.0
    if height_ft < lo:
        return 0.75 * (height_ft - 0.4 * lo) / (0.6 * lo)
    if height_ft <= sweet:
        return 0.75 + 0.25 * (height_ft - lo) / max(sweet - lo, 0.1)
    if height_ft <= hi:
        return 1.0 - 0.15 * (height_ft - sweet) / max(hi - sweet, 0.1)
    return _clamp(0.85 * (1.0 - (height_ft - hi) / (0.5 * hi)))


def wind_relation(speed: float | None, direction: float | None, spot: SpotParams) -> str:
    """The six relation strings (wind.wind_relation) against the spot's offshore direction."""
    return windmod.wind_relation(speed, direction, spot.offshore_wind_direction)


def disputed_wind_relation(
    speed: float | None,
    direction: float | None,
    spot: SpotParams,
    alternative_offshore_deg: float | None,
) -> str | None:
    """The wind relation if the offshore direction were ``alternative_offshore_deg`` (the
    main direction of sources that contradict the profile), or None without one.

    Used only to describe disputed winds; the score always uses the profile's direction.
    """
    if alternative_offshore_deg is None:
        return None
    return wind_relation(
        speed, direction, replace(spot, offshore_wind_direction=float(alternative_offshore_deg))
    )


def gust_for_scoring(sea: SeaState, gust_scoring: bool) -> float | None:
    """The gust the wind fit may use: only with WIND_GUST_SCORING=scored and a gust whose
    definition the source documents (wind.SCORABLE_GUST_KINDS); otherwise display-only."""
    if not gust_scoring or sea.wind_gust_kind not in windmod.SCORABLE_GUST_KINDS:
        return None
    return sea.wind_gust_kmh


def wind_fit(sea: SeaState, spot: SpotParams, gust_scoring: bool = False) -> windmod.WindFit | None:
    if sea.wind_speed_kmh is None:
        return None
    return windmod.wind_fit(
        sea.wind_speed_kmh,
        sea.wind_direction_deg,
        spot,
        gust_kmh=gust_for_scoring(sea, gust_scoring),
    )


def score_tide(stage: float | None, preference: str | None) -> float | None:
    if stage is None or not preference:
        return None
    if preference == "all":
        return 1.0
    target = TIDE_TARGETS.get(preference)
    if target is None:
        return None
    return _clamp(1.0 - abs(stage - target) * 1.6, 0.2, 1.0)


def tide_excluded_reason(spot: SpotParams) -> str | None:
    """Why this spot's tide preference is not used for scoring, or None when it is: the tide
    research class must be moderate or high, the decision not disputed and the preference
    set (the same rule as the tide block's "Tide preference not well established.")."""
    if not spot.tide_preference:
        return NO_TIDE_PREFERENCE
    if spot.tide_decision == "disputed":
        return TIDE_DISPUTED
    if spot.tide_research_class is None:
        return TIDE_NOT_RESEARCHED
    if spot.tide_research_class not in TIDE_SCORED_CLASSES:
        return TIDE_RESEARCH_LOW
    return None


def score_consistency(sea: SeaState, flux: list[PartitionFlux] | None = None) -> float | None:
    """Share of the wave energy at the break carried by organised swell rather than wind sea.

    With a breaking breakdown it is the swell partitions' share of the breaking energy flux
    (sum of Hb_i^2.5), so wind sea the nearshore model sends away from the break (directional
    factor R about 0: travelling away from the shore normal beta) costs nothing; otherwise the
    offshore energy share (H^2) of the three swells. Where beta + 180 is far from the offshore
    wind direction (points, wrapping reefs), wind sea raised by an offshore wind can keep a
    large R (docs/SURF_SCORING.md section 3e, limitations).
    """
    if sea.primary_swell_height_m is None:
        return None
    if flux:
        total = sum(p.breaking_m**2.5 for p in flux if p.breaking_m > 0)
        if total > 0:
            swell = sum(
                p.breaking_m**2.5 for p in flux if p.breaking_m > 0 and p.name in SWELL_KINDS
            )
            return _clamp(swell / total)
    swell_e = (
        sea.primary_swell_height_m**2
        + (sea.secondary_swell_height_m or 0.0) ** 2
        + (sea.tertiary_swell_height_m or 0.0) ** 2
    )
    wind_e = (sea.wind_wave_height_m or 0.0) ** 2
    total_e = swell_e + wind_e
    if total_e <= 0:
        return 0.0
    return _clamp(swell_e / total_e)


@dataclass(frozen=True)
class PartitionFit:
    kind: str
    weight: float
    height_m: float
    period_s: float
    direction_deg: float | None
    direction_fit: float | None
    period_fit: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "weight": round(self.weight, 3),
            "direction_fit": None if self.direction_fit is None else round(self.direction_fit, 3),
            "period_fit": None if self.period_fit is None else round(self.period_fit, 3),
        }


def partition_fits(breaking: BreakingEstimate, spot: SpotParams) -> list[PartitionFit]:
    """Direction and period fits per partition, weighted by Hb_i^2.5 / sum(Hb_j^2.5); empty
    when no partition breaks (blocked swell, or the calibrated model, which has no breakdown).
    """
    parts = [p for p in breaking.flux if p.breaking_m > 0]
    total = sum(p.breaking_m**2.5 for p in parts)
    if total <= 0:
        return []
    return [
        PartitionFit(
            kind=p.name,
            weight=p.breaking_m**2.5 / total,
            height_m=p.height_m,
            period_s=p.period_s,
            direction_deg=p.direction_deg,
            direction_fit=score_direction(p.direction_deg, spot),
            period_fit=score_period(p.period_s, spot),
        )
        for p in parts
    ]


def _weighted(fits: list[PartitionFit], attr: str) -> float | None:
    known = [(f.weight, getattr(f, attr)) for f in fits if getattr(f, attr) is not None]
    total = sum(w for w, _ in known)
    if not known or total <= 0:
        return None
    return sum(w * v for w, v in known) / total


def _bulk_direction(sea: SeaState) -> float | None:
    return (
        sea.primary_swell_direction_deg
        if sea.primary_swell_direction_deg is not None
        else sea.wave_direction_deg
    )


def _bulk_period(sea: SeaState) -> float | None:
    return (
        sea.primary_swell_period_s if sea.primary_swell_period_s is not None else sea.wave_period_s
    )


def contributions(
    components: dict[str, float],
    weights: dict[str, float],
    gates: dict[str, float],
) -> dict[str, float]:
    """Points each factor and gate cost the score (out of 100).

    A factor k loses ``100 w_k (1 - c_k)`` of the base; the gates then remove
    ``100 base (1 - G)`` with G their product, shared in proportion to ``-ln(g)`` (a gate of
    0 takes the whole loss). The points plus the unrounded score are exactly 100.
    """
    out = {k: 100.0 * weights[k] * (1.0 - components[k]) for k in weights}
    base = sum(weights[k] * components[k] for k in weights)
    product = math.prod(gates.values())
    loss = 100.0 * base * (1.0 - product)
    zero = [g for g, v in gates.items() if v <= 0]
    logs = {g: -math.log(v) for g, v in gates.items() if v > 0}
    total_log = sum(logs.values())
    for g, v in gates.items():
        if zero:
            out[g] = loss / len(zero) if v <= 0 else 0.0
        else:
            out[g] = loss * logs[g] / total_log if total_log > 0 else 0.0
    return {k: round(v, 2) for k, v in out.items()}


def evaluate(
    sea: SeaState,
    spot: SpotParams,
    alternative_offshore_deg: float | None = None,
    *,
    gust_scoring: bool = False,
    tide_detail: dict[str, Any] | None = None,
    weights: Mapping[str, float] | None = None,
) -> QualityResult:
    """Score one hour. ``alternative_offshore_deg`` (sources disagree about which wind is
    offshore) only changes the explanation, which then names both wind relations when
    they differ; the score and ``wind_relation`` use the profile's offshore direction.

    ``gust_scoring`` is WIND_GUST_SCORING == "scored"; ``tide_detail`` describes the tide
    stage in ``sea.tide_stage`` (stage, rising, range_m, source) and is stored when the tide
    is scored. ``weights``: the component weights (``weights.effective_weights`` of
    SCORING_WEIGHTS_JSON; None = the defaults).
    """
    weight_of = DEFAULT_WEIGHTS if weights is None else weights
    breaking = estimate_breaking_height(sea, spot)
    fits = partition_fits(breaking, spot)
    direction_c = _weighted(fits, "direction_fit") if fits else None
    if direction_c is None:
        direction_c = score_direction(_bulk_direction(sea), spot)
    period_c = _weighted(fits, "period_fit") if fits else None
    if period_c is None:
        period_c = score_period(_bulk_period(sea), spot)
    wfit = wind_fit(sea, spot, gust_scoring)
    excluded = tide_excluded_reason(spot)
    tide_c = None if excluded is not None else score_tide(sea.tide_stage, spot.tide_preference)
    if excluded is None and tide_c is None:
        excluded = NO_TIDE_PREDICTION if sea.tide_stage is None else NO_TIDE_PREFERENCE
    raw: dict[str, float | None] = {
        "swell_direction": direction_c,
        "swell_period": period_c,
        "wave_size": score_size(breaking.mid_ft, spot),
        "wind": None if wfit is None else wfit.q,
        "tide": tide_c,
        "consistency": score_consistency(sea, breaking.flux),
    }
    components = {k: round(v, 3) for k, v in raw.items() if v is not None}
    present = {k: weight_of[k] for k in components}
    total_w = sum(present.values())
    used = {k: v / total_w for k, v in present.items()} if total_w else {}
    base = sum(components[k] * used[k] for k in components)

    # Limiting-factor gates: a weighted mean alone lets clean wind compensate for flat,
    # short-period surf. Gates stop tiny, weak or blown-out conditions from scoring well.
    # A component weighted 0 (SCORING_WEIGHTS_JSON) is out of the score, its gate included;
    # wave_size always keeps a weight above 0 (weights.validate_weights).
    size_gate = _clamp(components["wave_size"] / 0.6)
    period_fit = components.get("swell_period") if used.get("swell_period") else None
    period_gate = 1.0 if period_fit is None else 0.5 + 0.5 * _clamp(period_fit / 0.5)
    wind_c = components.get("wind") if used.get("wind") else None
    wind_gate = 1.0 if wind_c is None else 0.5 + 0.5 * _clamp(wind_c / WIND_GATE_FULL_Q)
    score = int(round(100 * base * size_gate * period_gate * wind_gate))
    score = max(0, min(100, score))
    gates = {"size_gate": size_gate, "period_gate": period_gate, "wind_gate": wind_gate}
    for name, value in gates.items():
        components[name] = round(value, 3)

    details: dict[str, Any] = {
        "weights_used": {k: round(v, 3) for k, v in used.items()},
        "contributions": contributions({k: components[k] for k in used}, used, gates),
    }
    if wfit is not None:
        details["wind_detail"] = wfit.as_dict()
    if fits:
        details["direction_partitions"] = [f.as_dict() for f in fits]
    if tide_c is not None:
        details["tide_detail"] = tide_detail or {
            "stage": sea.tide_stage,
            "rising": None,
            "range_m": None,
            "source": None,
        }
    else:
        details["tide_excluded_reason"] = excluded

    relation = wind_relation(sea.wind_speed_kmh, sea.wind_direction_deg, spot)
    alternative = disputed_wind_relation(
        sea.wind_speed_kmh, sea.wind_direction_deg, spot, alternative_offshore_deg
    )
    label = label_for_score(score)
    driver = max(fits, key=lambda f: f.weight) if fits else None
    explanation = build_explanation(
        sea,
        spot,
        breaking.min_ft,
        breaking.max_ft,
        relation,
        raw,
        alternative_relation=alternative,
        driver=driver,
        strength=None if wfit is None else wfit.strength,
        tide_not_scored=excluded if sea.tide_stage is not None and tide_c is None else None,
    )
    details["favourable"], details["unfavourable"] = hour_reasons(
        sea, spot, breaking, raw, used, details["contributions"], gates, fits, wfit
    )
    return QualityResult(
        score=score,
        label=label.value,
        components=components,
        breaking=breaking,
        wind_relation=relation,
        explanation=explanation,
        details=details,
        model_version=model_version(weights),
    )


def named_partition(fits: list[PartitionFit], attr: str, *, good: bool) -> PartitionFit | None:
    """The partition a direction or period phrase names (``attr`` "direction_fit" or
    "period_fit"); None when no partition has that fit (the bulk sea is described then).

    A favourable phrase names the heaviest partition whose own fit is good (at least
    reasons.GOOD_FIT; one exists whenever the weighted fit is), else the one adding most to
    the weighted fit (largest weight x fit). An unfavourable phrase names the heaviest
    partition whose own fit is poor (below reasons.POOR_FIT), else the one costing the
    weighted fit most (largest weight x (1 - fit)), whose fit is below 1: its period is
    shorter than the ideal, its direction outside the optimal range.

    The partition that drives the surf (the largest share) is not always the one: with
    shares 0.37 / 0.34 / 0.29 a 16 s swell can drive the surf while a 6 s swell and a 5 s
    wind sea make the weighted period fit poor, and the phrase must then name a short
    period, not the 16 s one."""
    known = [(f, v) for f in fits if (v := getattr(f, attr)) is not None]
    if not known:
        return None
    if good:
        clear = [(f, v) for f, v in known if v >= GOOD_FIT]
        if clear:
            return max(clear, key=lambda fv: fv[0].weight)[0]
        return max(known, key=lambda fv: fv[0].weight * fv[1])[0]
    clear = [(f, v) for f, v in known if v < POOR_FIT]
    if clear:
        return max(clear, key=lambda fv: fv[0].weight)[0]
    return max(known, key=lambda fv: fv[0].weight * (1.0 - fv[1]))[0]


def hour_reasons(
    sea: SeaState,
    spot: SpotParams,
    breaking: BreakingEstimate,
    raw: dict[str, float | None],
    used: dict[str, float],
    points_lost: dict[str, float],
    gates: dict[str, float],
    fits: list[PartitionFit],
    wfit: windmod.WindFit | None,
) -> tuple[list[str], list[str]]:
    """The hour's main favourable and unfavourable conditions (reasons.condition_reasons).

    Each swell phrase names the partition behind its claim (:func:`named_partition`; the
    primary or bulk sea without partition fits), so "well-aligned secondary swell" or "period
    too short for this reef (8 s)" describe the partition that made the fit good or poor;
    the wind relation is the one against the offshore direction the wind fit used."""

    def kind(part: PartitionFit | None) -> str:
        return "swell" if part is None else PARTITION_WORDS.get(part.kind, "swell")

    def period(part: PartitionFit | None) -> float | None:
        return _bulk_period(sea) if part is None else part.period_s

    aligned = named_partition(fits, "direction_fit", good=True)
    misaligned = named_partition(fits, "direction_fit", good=False)
    off_direction = _bulk_direction(sea) if misaligned is None else misaligned.direction_deg
    blocked = (
        off_direction is not None
        and window_distance(off_direction, spot.swell_window_min, spot.swell_window_max) > 0
    )
    return condition_reasons(
        raw,
        used_weights=used,
        points_lost=points_lost,
        gates=gates,
        swell_kind=kind(aligned),
        misaligned_kind=kind(misaligned),
        swell_period_s=period(named_partition(fits, "period_fit", good=True)),
        short_period_s=period(named_partition(fits, "period_fit", good=False)),
        swell_blocked=blocked,
        break_type=spot.break_type,
        face_min_ft=breaking.min_ft,
        face_max_ft=breaking.max_ft,
        face_mid_ft=breaking.mid_ft,
        size_min_ft=spot.wave_height_min_ft,
        size_max_ft=spot.wave_height_max_ft,
        wind_relation="unknown" if wfit is None else wfit.relation,
        wind_strength=None if wfit is None else wfit.strength,
        wind_favourability=None if wfit is None else wfit.favourability,
        wind_speed_kmh=sea.wind_speed_kmh,
    )


def _describe(score: float | None, good: str, ok: str, bad: str) -> str:
    if score is None:
        return ""
    if score >= 0.75:
        return good
    if score >= 0.4:
        return ok
    return bad


def _whole(value: float) -> int:
    """``value`` to a whole number with halves rounded up, as the charts' ``toFixed(0)`` does
    (Python's format rounds 34.5 to 34), so the sentence and the summary never differ by 1."""
    return math.floor(value + 0.5)


def build_explanation(
    sea: SeaState,
    spot: SpotParams,
    hmin: float,
    hmax: float,
    relation: str,
    raw: dict[str, float | None],
    *,
    alternative_relation: str | None = None,
    driver: PartitionFit | None = None,
    strength: str | None = None,
    tide_not_scored: str | None = None,
) -> str:
    """One sentence per factor. The swell part describes the partition that drives the
    surf (``driver``, the largest share of the breaking energy flux), else the primary or
    bulk sea. With ``alternative_relation`` (the wind relation under the direction that
    contradicting sources call offshore) different from ``relation``, the wind part reads
    "moderate wind 12 km/h from E, offshore or cross-shore: sources disagree"."""
    parts: list[str] = []
    if driver is not None:
        h: float | None = driver.height_m
        t: float | None = driver.period_s
        d = driver.direction_deg
        kind = PARTITION_WORDS.get(driver.kind, "swell")
    else:
        h = sea.primary_swell_height_m or sea.sig_wave_height_m
        t = sea.primary_swell_period_s or sea.wave_period_s
        d = _bulk_direction(sea)
        kind = "swell"
    if h is not None and t is not None:
        dir_note = _describe(
            raw["swell_direction"],
            "well aligned with the break",
            "at the edge of the break's swell window",
            "poorly aligned / partly blocked",
        )
        parts.append(
            f"{h:.1f} m @ {_whole(t)} s {compass_label(d)} {kind}"
            + (f", {dir_note}" if dir_note else "")
        )
    if sea.wind_speed_kmh is not None:
        # The Beaufort-based strength class (wind.wind_strength), e.g. "light offshore wind".
        prefix = f"{strength} " if strength and relation != "unknown" else ""
        if relation == "glassy":
            parts.append("glassy, near-calm wind")
        elif alternative_relation is not None and alternative_relation not in (
            relation,
            "unknown",
        ):
            parts.append(
                f"{prefix}wind {_whole(sea.wind_speed_kmh)} km/h from "
                f"{compass_label(sea.wind_direction_deg)}, {relation} or "
                f"{alternative_relation}: sources disagree"
            )
        else:
            parts.append(
                f"{prefix}{relation} wind {_whole(sea.wind_speed_kmh)} km/h from "
                f"{compass_label(sea.wind_direction_deg)}"
            )
    parts.append(
        f"approx. {hmin:g}–{hmax:g} ft faces vs. a working range of "
        f"{spot.wave_height_min_ft:g}–{spot.wave_height_max_ft:g} ft"
    )
    tide = raw.get("tide")
    if tide is not None:
        parts.append(_describe(tide, "favourable tide", "workable tide", "unfavourable tide"))
    elif tide_not_scored:
        parts.append("tide not scored (" + tide_not_scored.rstrip(".").lower() + ")")
    return "; ".join(p for p in parts if p) + "."
