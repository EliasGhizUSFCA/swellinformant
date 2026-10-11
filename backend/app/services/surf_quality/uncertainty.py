"""Forecast uncertainty of the breaking height and the confidence index.

The breaking-height forecast is treated as log-normal around the central estimate Hb:

    σ_ln(Hb)² = (0.8 · σ_ln(Hs))² + (0.4 · σ_T / T)² + σ_m²

The exponents 0.8 and 0.4 come from Komar–Gaughan scaling (Hb ∝ H0^0.8 · T^0.4).

* **Offshore part** σ_ln(Hs)² = σ_f² + σ0², where σ_f is the forecast-dependent spread:
  - k · GEFS-Wave ensemble spread / mean when an ensemble row exists for that hour
    (``ensemble_heuristic``), with k = 1.05: in the 51-cycle GEFS analysis-verification
    (docs/surf-forecast-model-research.md, section 12) the unfiltered log error exceeds
    the relative spread by 6–13 % on days 3–10;
  - otherwise the lead-time table σ_L (``heuristic``) from the same analysis, unfiltered
    (flat seas included): 0.080/0.192/0.276/0.352/0.430 at days 1/3/5/7/10. Both are
    measured against the model's own analysis, so they are a lower bound on real error;
  - and never less than the spread between independent model families when several
    were blended.
  σ0 = 0.15 is a prior floor for model-versus-reality error, labelled heuristic: the
  tables above verify against the model's own analysis, a lower bound on real error.
* **Period** σ_T / T: ensemble period spread / mean when available, else 0.08 (assumed).
* **Translation** σ_m = 0.30 for spots without a validated calibration: the scatter of
  breaker-height formulas and focusing uncertainty (research notes, nearshore section).

The 80 % range is Hb · exp(∓1.2816 σ). It is a stated assumption, not a calibrated
interval, until verification against observations exists (``uncertainty_kind``
"calibrated" is reserved for that case and nothing sets it yet).

The confidence index (0–100) is *not* a probability. It summarises offshore forecast
certainty only: 100 · exp(−σ_f / 0.4) × data completeness, ×0.8 when the run is stale.
Run-to-run change is kept as a diagnostic, not a confidence factor: in the research
sample it predicted error much more weakly than ensemble spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# (lead days, σ_ln of Hs) — GEFS-Wave ensemble-mean log error vs its own analysis,
# unfiltered (a lower bound on the error against observations).
LEAD_TABLE: tuple[tuple[float, float], ...] = (
    (1.0, 0.080),
    (3.0, 0.192),
    (5.0, 0.276),
    (7.0, 0.352),
    (10.0, 0.430),
)
# Unfiltered log error / relative ensemble spread (1.0–1.1 in the research analysis).
SPREAD_SCALE = 1.05
SIGMA_FLOOR = 0.15
SIGMA_PERIOD_REL = 0.08
SIGMA_TRANSLATION_UNCALIBRATED = 0.30
SIGMA_MAX = 0.8
Z80 = 1.2816  # standard-normal 90th percentile
CONFIDENCE_SCALE = 0.4


def lead_sigma(lead_hours: float) -> float:
    """σ_ln(Hs) from the lead table; flat before day 1, linear beyond day 10 (capped)."""
    days = max(0.0, lead_hours) / 24.0
    if days <= LEAD_TABLE[0][0]:
        return LEAD_TABLE[0][1]
    for (d0, s0), (d1, s1) in zip(LEAD_TABLE, LEAD_TABLE[1:], strict=False):
        if days <= d1:
            return s0 + (s1 - s0) * (days - d0) / (d1 - d0)
    (d0, s0), (d1, s1) = LEAD_TABLE[-2], LEAD_TABLE[-1]
    return min(SIGMA_MAX, s1 + (s1 - s0) / (d1 - d0) * (days - d1))


@dataclass(frozen=True)
class EnsembleInfo:
    hs_mean_m: float | None = None
    hs_spread_m: float | None = None
    tp_mean_s: float | None = None
    tp_spread_s: float | None = None
    members: int | None = None


@dataclass
class Uncertainty:
    sigma_forecast: float  # forecast-dependent part of σ_ln(Hs)
    sigma_offshore: float  # σ_ln(Hs) including the floor
    sigma_period_rel: float
    sigma_translation: float
    sigma_total: float  # σ_ln(Hb)
    p10_ft: float
    p90_ft: float
    kind: str
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sigma_forecast": round(self.sigma_forecast, 3),
            "sigma_offshore": round(self.sigma_offshore, 3),
            "sigma_period_rel": round(self.sigma_period_rel, 3),
            "sigma_translation": round(self.sigma_translation, 3),
            "sigma_total": round(self.sigma_total, 3),
            "kind": self.kind,
            "source": self.source,
        }


def translation_sigma(calibration: dict[str, Any] | None) -> tuple[float, bool]:
    """σ_m and whether it comes from a validated calibration."""
    cal = calibration or {}
    if cal.get("validated") and isinstance(cal.get("sigma_ln"), int | float):
        return float(cal["sigma_ln"]), True
    return SIGMA_TRANSLATION_UNCALIBRATED, False


def breaking_uncertainty(
    breaking_mid_ft: float,
    lead_hours: float,
    *,
    ensemble: EnsembleInfo | None = None,
    between_model_sigma: float | None = None,
    calibration: dict[str, Any] | None = None,
) -> Uncertainty:
    source = "lead_table"
    sigma_f = lead_sigma(lead_hours)
    # Tiny means make spread/mean meaningless (flat seas): keep the lead table there.
    mean = ensemble.hs_mean_m if ensemble else None
    spread = ensemble.hs_spread_m if ensemble else None
    if mean is not None and spread is not None and mean > 0.05:
        sigma_f = SPREAD_SCALE * spread / mean
        source = "ensemble_spread"
    if between_model_sigma is not None and between_model_sigma > sigma_f:
        sigma_f = between_model_sigma
        source = "model_disagreement"
    sigma_f = min(SIGMA_MAX, sigma_f)
    sigma_off = math.sqrt(sigma_f**2 + SIGMA_FLOOR**2)

    sigma_t = SIGMA_PERIOD_REL
    if ensemble and ensemble.tp_mean_s and ensemble.tp_spread_s is not None:
        sigma_t = ensemble.tp_spread_s / ensemble.tp_mean_s

    sigma_m, calibrated = translation_sigma(calibration)
    total = math.sqrt((0.8 * sigma_off) ** 2 + (0.4 * sigma_t) ** 2 + sigma_m**2)
    if calibrated:
        kind = "calibrated"
    elif source == "ensemble_spread":
        kind = "ensemble_heuristic"
    else:
        kind = "heuristic"
    return Uncertainty(
        sigma_forecast=sigma_f,
        sigma_offshore=sigma_off,
        sigma_period_rel=sigma_t,
        sigma_translation=sigma_m,
        sigma_total=total,
        p10_ft=round(breaking_mid_ft * math.exp(-Z80 * total), 1),
        p90_ft=round(breaking_mid_ft * math.exp(Z80 * total), 1),
        kind=kind,
        source=source,
    )


def confidence_index(sigma_forecast: float, data_completeness: float, stale: bool) -> int:
    """0–100 index of offshore forecast certainty (not a probability)."""
    value = 100.0 * math.exp(-max(0.0, sigma_forecast) / CONFIDENCE_SCALE)
    value *= 0.6 + 0.4 * max(0.0, min(1.0, data_completeness))
    if stale:
        value *= 0.8
    return int(round(max(0.0, min(100.0, value))))
