"""Deterministic, documented surf quality score (0–100).

Each component maps measurable inputs to [0, 1]; the score is their weighted mean
(weights renormalised over the components whose inputs exist) multiplied by two gates
that stop flat or blown-out conditions from scoring well. The weights are a reasoned
starting point, NOT statistically calibrated values. See docs/SURF_SCORING.md.
"""

from __future__ import annotations

import math
from dataclasses import replace

from app.models.enums import QualityLabel
from app.services.geo import angular_diff, compass_label, window_distance
from app.services.surf_quality.breaking import estimate_breaking_height
from app.services.surf_quality.models import QualityResult, SeaState, SpotParams

# 2.0.0: breaking heights from the linear_wave_v2 nearshore model (energy-flux
# combination, directional spreading, shore normal); typical-to-sets ranges.
ALGORITHM_VERSION = "2.0.0"

WEIGHTS: dict[str, float] = {
    "swell_direction": 0.20,
    "swell_period": 0.20,
    "wave_size": 0.20,
    "wind": 0.30,
    "tide": 0.05,
    "consistency": 0.05,
}

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

LIGHT_WIND_KMH = 5.0  # at or below this the surface is effectively glassy
FULL_EFFECT_WIND_KMH = 20.0  # wind direction fully matters from here up
STRONG_WIND_KMH = 35.0


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


def score_direction(direction: float | None, spot: SpotParams) -> float | None:
    if direction is None:
        return None
    if window_distance(direction, spot.swell_window_min, spot.swell_window_max) > 0:
        return 0.0
    d_opt = window_distance(
        direction, spot.optimal_swell_direction_min, spot.optimal_swell_direction_max
    )
    return _clamp(1.0 - d_opt / 45.0)


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
    if speed is None or direction is None:
        return "unknown"
    if speed <= LIGHT_WIND_KMH:
        return "glassy"
    diff = angular_diff(direction, spot.offshore_wind_direction)
    if diff <= 45:
        return "offshore"
    if diff <= 80:
        return "cross-offshore"
    if diff <= 110:
        return "cross-shore"
    if diff <= 135:
        return "cross-onshore"
    return "onshore"


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


def score_wind(speed: float | None, direction: float | None, spot: SpotParams) -> float | None:
    if speed is None:
        return None
    if direction is None:
        direction_quality = 0.5
    else:
        diff = angular_diff(direction, spot.offshore_wind_direction)
        direction_quality = (1.0 + math.cos(math.radians(diff))) / 2.0
    effect = _clamp((speed - LIGHT_WIND_KMH) / (FULL_EFFECT_WIND_KMH - LIGHT_WIND_KMH))
    score = 1.0 - effect * (1.0 - direction_quality)
    if speed > STRONG_WIND_KMH:
        score *= max(0.4, 1.0 - (speed - STRONG_WIND_KMH) / 40.0)
    return _clamp(score)


def score_tide(stage: float | None, preference: str | None) -> float | None:
    if stage is None or not preference:
        return None
    if preference == "all":
        return 1.0
    target = TIDE_TARGETS.get(preference)
    if target is None:
        return None
    return _clamp(1.0 - abs(stage - target) * 1.6, 0.2, 1.0)


def score_consistency(sea: SeaState) -> float | None:
    """Share of wave energy carried by organised swell rather than local wind sea."""
    swell = sea.primary_swell_height_m
    if swell is None:
        return None
    swell_e = swell**2 + (sea.secondary_swell_height_m or 0.0) ** 2
    wind_e = (sea.wind_wave_height_m or 0.0) ** 2
    total = swell_e + wind_e
    if total <= 0:
        return 0.0
    return _clamp(swell_e / total)


def evaluate(
    sea: SeaState, spot: SpotParams, alternative_offshore_deg: float | None = None
) -> QualityResult:
    """Score one hour. ``alternative_offshore_deg`` (sources disagree about which wind is
    offshore) only changes the explanation, which then names both wind relations when
    they differ; the score and ``wind_relation`` use the profile's offshore direction."""
    breaking = estimate_breaking_height(sea, spot)
    direction = (
        sea.primary_swell_direction_deg
        if sea.primary_swell_direction_deg is not None
        else sea.wave_direction_deg
    )
    period = (
        sea.primary_swell_period_s if sea.primary_swell_period_s is not None else sea.wave_period_s
    )
    raw: dict[str, float | None] = {
        "swell_direction": score_direction(direction, spot),
        "swell_period": score_period(period, spot),
        "wave_size": score_size(breaking.mid_ft, spot),
        "wind": score_wind(sea.wind_speed_kmh, sea.wind_direction_deg, spot),
        "tide": score_tide(sea.tide_stage, spot.tide_preference),
        "consistency": score_consistency(sea),
    }
    components = {k: round(v, 3) for k, v in raw.items() if v is not None}
    weights = {k: WEIGHTS[k] for k in components}
    total_w = sum(weights.values())
    base = sum(components[k] * weights[k] for k in components) / total_w if total_w else 0.0

    # Limiting-factor gates: a weighted mean alone lets clean wind compensate for flat,
    # short-period surf. Gates stop tiny, weak or blown-out conditions from scoring well.
    size_gate = _clamp(components["wave_size"] / 0.6)
    period_c = components.get("swell_period")
    period_gate = 1.0 if period_c is None else 0.5 + 0.5 * _clamp(period_c / 0.5)
    wind_c = components.get("wind")
    wind_gate = 1.0 if wind_c is None else 0.5 + 0.5 * _clamp(wind_c / 0.5)
    score = int(round(100 * base * size_gate * period_gate * wind_gate))
    score = max(0, min(100, score))
    components["size_gate"] = round(size_gate, 3)
    components["period_gate"] = round(period_gate, 3)
    components["wind_gate"] = round(wind_gate, 3)

    relation = wind_relation(sea.wind_speed_kmh, sea.wind_direction_deg, spot)
    alternative = disputed_wind_relation(
        sea.wind_speed_kmh, sea.wind_direction_deg, spot, alternative_offshore_deg
    )
    label = label_for_score(score)
    explanation = build_explanation(
        sea,
        spot,
        breaking.min_ft,
        breaking.max_ft,
        relation,
        raw,
        alternative_relation=alternative,
    )
    return QualityResult(
        score=score,
        label=label.value,
        components=components,
        weights_used={k: round(v / total_w, 3) for k, v in weights.items()} if total_w else {},
        breaking=breaking,
        wind_relation=relation,
        explanation=explanation,
    )


def _describe(score: float | None, good: str, ok: str, bad: str) -> str:
    if score is None:
        return ""
    if score >= 0.75:
        return good
    if score >= 0.4:
        return ok
    return bad


def build_explanation(
    sea: SeaState,
    spot: SpotParams,
    hmin: float,
    hmax: float,
    relation: str,
    raw: dict[str, float | None],
    *,
    alternative_relation: str | None = None,
) -> str:
    """One sentence per factor. With ``alternative_relation`` (the wind relation under the
    direction that contradicting sources call offshore) different from ``relation``, the
    wind part reads "wind 12 km/h from E, offshore or cross-shore: sources disagree"."""
    parts: list[str] = []
    h = sea.primary_swell_height_m or sea.sig_wave_height_m
    t = sea.primary_swell_period_s or sea.wave_period_s
    d = (
        sea.primary_swell_direction_deg
        if sea.primary_swell_direction_deg is not None
        else (sea.wave_direction_deg)
    )
    if h is not None and t is not None:
        dir_note = _describe(
            raw["swell_direction"],
            "well aligned with the break",
            "at the edge of the break's swell window",
            "poorly aligned / partly blocked",
        )
        parts.append(
            f"{h:.1f} m @ {t:.0f} s {compass_label(d)} swell"
            + (f", {dir_note}" if dir_note else "")
        )
    if sea.wind_speed_kmh is not None:
        if relation == "glassy":
            parts.append("glassy, near-calm wind")
        elif alternative_relation is not None and alternative_relation not in (
            relation,
            "unknown",
        ):
            parts.append(
                f"wind {sea.wind_speed_kmh:.0f} km/h from "
                f"{compass_label(sea.wind_direction_deg)}, {relation} or "
                f"{alternative_relation}: sources disagree"
            )
        else:
            parts.append(
                f"{relation} wind {sea.wind_speed_kmh:.0f} km/h from "
                f"{compass_label(sea.wind_direction_deg)}"
            )
    parts.append(
        f"approx. {hmin:g}–{hmax:g} ft faces vs. a working range of "
        f"{spot.wave_height_min_ft:g}–{spot.wave_height_max_ft:g} ft"
    )
    tide = raw.get("tide")
    if tide is not None:
        parts.append(_describe(tide, "favourable tide", "workable tide", "unfavourable tide"))
    return "; ".join(p for p in parts if p) + "."
