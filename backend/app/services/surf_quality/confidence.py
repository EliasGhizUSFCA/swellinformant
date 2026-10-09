"""Forecast confidence (0–100), kept strictly separate from predicted surf quality.

Confidence answers "how much should I trust this forecast?" — not "how good will it be?".
Factors (multiplicative, documented in docs/SURF_SCORING.md):

* lead time: 25 + 75·exp(-lead_days / 9). Skill of global wave models decays with lead
  time; ~0.5 at 7 days is a conservative reading of published GFS-Wave verification.
* data completeness: 0.6 + 0.4·(fraction of key variables present)
* run-to-run agreement with the previous model run for the same valid time:
  0.7 + 0.3·agreement, or 0.9 when no previous run exists yet
* staleness: ×0.8 if the model run is older than FORECAST_STALE_HOURS
"""

from __future__ import annotations

import math

from app.models.enums import ConfidenceLabel

KEY_FIELDS = (
    "primary_swell_height_m",
    "primary_swell_period_s",
    "primary_swell_direction_deg",
    "wind_speed_kmh",
    "wind_direction_deg",
)


def completeness(values: dict[str, float | None]) -> float:
    present = sum(1 for k in KEY_FIELDS if values.get(k) is not None)
    return present / len(KEY_FIELDS)


def run_agreement(current_h: float | None, previous_h: float | None) -> float | None:
    if current_h is None or previous_h is None:
        return None
    diff = abs(current_h - previous_h) / max(current_h, previous_h, 0.5)
    return max(0.0, min(1.0, 1.0 - diff))


def confidence_score(
    lead_hours: float,
    data_completeness: float,
    agreement: float | None = None,
    stale: bool = False,
) -> int:
    lead_days = max(0.0, lead_hours) / 24.0
    value = 25.0 + 75.0 * math.exp(-lead_days / 9.0)
    value *= 0.6 + 0.4 * max(0.0, min(1.0, data_completeness))
    value *= 0.9 if agreement is None else 0.7 + 0.3 * agreement
    if stale:
        value *= 0.8
    return int(round(max(0.0, min(100.0, value))))


def confidence_label(value: float) -> ConfidenceLabel:
    if value >= 70:
        return ConfidenceLabel.HIGH
    if value >= 45:
        return ConfidenceLabel.MODERATE
    return ConfidenceLabel.LOW
