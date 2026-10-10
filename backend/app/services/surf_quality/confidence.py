"""Forecast confidence labels and data-quality diagnostics.

Confidence answers "how much should I trust this forecast?" — never "how good will the
surf be?". The 0–100 index itself is computed in ``uncertainty.confidence_index`` from
the forecast-dependent spread (ensemble spread, lead time, model disagreement), data
completeness and staleness; it is an index, not a probability.

``run_agreement`` compares a valid time with the previous model run. It is stored as a
diagnostic ("forecast changed since the last run") but no longer feeds the confidence
index: in the research sample run-to-run change predicted forecast error much more
weakly than ensemble spread (correlation about 0.3 against 0.5–0.6) and added nothing
once spread was included.
"""

from __future__ import annotations

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


def confidence_label(value: float) -> ConfidenceLabel:
    if value >= 70:
        return ConfidenceLabel.HIGH
    if value >= 45:
        return ConfidenceLabel.MODERATE
    return ConfidenceLabel.LOW
