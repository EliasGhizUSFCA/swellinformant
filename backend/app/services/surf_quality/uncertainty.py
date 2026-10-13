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
"calibrated" is reserved for a validated translation and nothing sets it yet).

**Calibrated offshore component** (``ndbc-offshore-v1``, docs/ml-evaluation.md section
12.2). Against NDBC spectral Hm0 the lead-table range above covered 53-89 % instead of
80 % (the raw GFS-Wave forecast is biased low, so most misses were above p90). Scales
fitted on the older year only (2024-10 to 2025-09) give each offshore bound its own factor
on σ_off::

    offshore p10 = Hs · exp(−1.2816 · k_lo(lead) · σ_off)
    offshore p90 = Hs · exp(+1.2816 · k_hi(lead) · σ_off)

with k_lo, k_hi linear in lead between the bucket centres of OFFSHORE_CALIBRATION and
constant outside them. On the held-out newer year (2025-10 to 2026-10) the result covered
75.1-80.2 % at every lead bucket pooled over five buoys, which passes the pre-registered
75-85 % criterion. It is used ONLY when the caller passes an ``OffshoreContext`` and every
condition holds (``OffshoreCalibration.applies``): the spread comes from the lead table
(no ensemble row, no model disagreement), the central value is a raw forecast of the one
deterministic family it was fitted on (GFS-Wave, ``ncep_gfs``; not a blend and not
ML-corrected, since the factors absorb the raw model's low bias), the lead lies within the
validated 3-240 h, and the spot is one whose paired offshore buoy confirmed the range in
its own held-out data over all leads and at every lead bucket (``spot_slugs``: jaws-peahi
and trestles; the range was validated on station bulletins at the buoys, not on the
gridded values the spots receive). Then the breaking range uses k_lo · σ_off for its
lower and k_hi · σ_off for its upper bound, the central value is unchanged, and
``offshore_kind`` is "calibrated" while ``kind`` keeps the heuristic label: the
translation share σ_m (the larger part of the breaking range) is still an assumption.
Without a context (or outside those conditions) everything is as above.

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


# Offshore range calibration (docs/ml-evaluation.md section 12.2). k_lo / k_hi per lead
# bucket centre, fitted on TRAIN (valid times up to 2025-09-29, five NDBC buoys) as
# Q0.90(∓e / σ_off) / Z80 with e = ln(obs Hm0) − ln(raw GFS-Wave bulletin Hs).
OFFSHORE_CALIBRATION_VERSION = "ndbc-offshore-v1"


@dataclass(frozen=True)
class OffshoreContext:
    """What ``breaking_uncertainty`` must know to use a calibrated offshore range.

    ``families``: the deterministic model families behind the central value (one for a
    single source; ensembles are not listed). ``ml_corrected``: an ML correction changed
    the central offshore Hs of this hour.
    """

    spot_slug: str
    families: tuple[str, ...]
    ml_corrected: bool


@dataclass(frozen=True)
class OffshoreCalibration:
    """Lead-dependent factors on σ_off for the lower and upper offshore bound."""

    version: str
    sigma_source: str  # the spread source it was fitted for
    family: str  # the one deterministic family of the central value
    lead_h: tuple[float, ...]  # bucket centres
    k_lo: tuple[float, ...]
    k_hi: tuple[float, ...]
    lead_min_h: float
    lead_max_h: float
    spot_slugs: frozenset[str]

    def factors(self, lead_hours: float) -> tuple[float, float]:
        """(k_lo, k_hi) at this lead: linear between centres, constant outside."""
        return _interp(lead_hours, self.lead_h, self.k_lo), _interp(
            lead_hours, self.lead_h, self.k_hi
        )

    def applies(self, ctx: OffshoreContext | None, sigma_source: str, lead_hours: float) -> bool:
        return (
            ctx is not None
            and sigma_source == self.sigma_source
            and tuple(ctx.families) == (self.family,)
            and not ctx.ml_corrected
            and self.lead_min_h <= lead_hours <= self.lead_max_h
            and ctx.spot_slug in self.spot_slugs
        )


def _interp(x: float, xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


# Spots whose paired offshore buoy (data/observation_stations.json) had held-out coverage
# consistent with 80 % over all leads AND at every lead bucket (no 90 % CI entirely
# outside 75-85 %, ranges.station_consistency): 51205 (85.5 % all leads) -> jaws-peahi;
# 46224 (76.7 %) -> trestles. Not listed, although part of the pooled fit: the 51201 spots
# (90.8 %, too wide), rincon (46053, 69.3 %, too narrow), and mavericks / ocean-beach-sf
# (46026: 79.4 % over all leads but 69.1 % [64.2, 73.9] at 240 h, removed in review).
OFFSHORE_CALIBRATION = OffshoreCalibration(
    version=OFFSHORE_CALIBRATION_VERSION,
    sigma_source="lead_table",
    family="ncep_gfs",
    lead_h=(24.0, 48.0, 72.0, 120.0, 168.0, 240.0),
    k_lo=(0.0858, 0.1479, 0.1642, 0.2140, 0.3210, 0.4126),
    k_hi=(1.9384, 1.6650, 1.3346, 1.0678, 0.9726, 0.9488),
    lead_min_h=3.0,
    lead_max_h=240.0,
    spot_slugs=frozenset({"jaws-peahi", "trestles"}),
)


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
    kind: str  # label of the breaking range as a whole
    source: str
    # Label of the offshore component: "calibrated" (OFFSHORE_CALIBRATION applied) or the
    # heuristic kind; and of the translation share ("calibrated" only with a validated
    # spot calibration).
    offshore_kind: str = "heuristic"
    nearshore_kind: str = "heuristic"
    offshore_calibration: str | None = None
    offshore_k_lo: float = 1.0
    offshore_k_hi: float = 1.0
    sigma_total_lo: float | None = None  # σ_ln(Hb) behind p10 when it differs from p90's
    sigma_total_hi: float | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sigma_forecast": round(self.sigma_forecast, 3),
            "sigma_offshore": round(self.sigma_offshore, 3),
            "sigma_period_rel": round(self.sigma_period_rel, 3),
            "sigma_translation": round(self.sigma_translation, 3),
            "sigma_total": round(self.sigma_total, 3),
            "kind": self.kind,
            "source": self.source,
            "offshore_kind": self.offshore_kind,
            "nearshore_kind": self.nearshore_kind,
        }
        if self.offshore_calibration is not None:
            out["offshore_calibration"] = self.offshore_calibration
            out["offshore_k"] = [round(self.offshore_k_lo, 4), round(self.offshore_k_hi, 4)]
            out["sigma_total_lo"] = round(self.sigma_total_lo or self.sigma_total, 3)
            out["sigma_total_hi"] = round(self.sigma_total_hi or self.sigma_total, 3)
        return out


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
    offshore: OffshoreContext | None = None,
) -> Uncertainty:
    """The breaking-height range (module docstring). ``offshore``: optional context that
    lets the calibrated offshore component apply (OFFSHORE_CALIBRATION.applies)."""
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

    def total_with(k: float) -> float:
        return math.sqrt((0.8 * k * sigma_off) ** 2 + (0.4 * sigma_t) ** 2 + sigma_m**2)

    total = total_with(1.0)
    heuristic_kind = "ensemble_heuristic" if source == "ensemble_spread" else "heuristic"
    kind = "calibrated" if calibrated else heuristic_kind
    cal = OFFSHORE_CALIBRATION
    if cal.applies(offshore, source, lead_hours):
        k_lo, k_hi = cal.factors(lead_hours)
        total_lo, total_hi = total_with(k_lo), total_with(k_hi)
        return Uncertainty(
            sigma_forecast=sigma_f,
            sigma_offshore=sigma_off,
            sigma_period_rel=sigma_t,
            sigma_translation=sigma_m,
            sigma_total=total,
            p10_ft=round(breaking_mid_ft * math.exp(-Z80 * total_lo), 1),
            p90_ft=round(breaking_mid_ft * math.exp(Z80 * total_hi), 1),
            kind=kind,
            source=source,
            offshore_kind="calibrated",
            nearshore_kind="calibrated" if calibrated else "heuristic",
            offshore_calibration=cal.version,
            offshore_k_lo=k_lo,
            offshore_k_hi=k_hi,
            sigma_total_lo=total_lo,
            sigma_total_hi=total_hi,
        )
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
        offshore_kind=heuristic_kind,
        nearshore_kind="calibrated" if calibrated else "heuristic",
    )


def offshore_range(
    hs_m: float,
    lead_hours: float,
    *,
    ensemble: EnsembleInfo | None = None,
    between_model_sigma: float | None = None,
    offshore: OffshoreContext | None = None,
) -> tuple[float, float, str]:
    """(p10, p90, kind) of the OFFSHORE Hs itself (m): the component of the breaking
    range that buoys can verify, calibrated under the same conditions."""
    unc = breaking_uncertainty(
        1.0, lead_hours, ensemble=ensemble, between_model_sigma=between_model_sigma
    )
    s = unc.sigma_offshore
    k_lo = k_hi = 1.0
    kind = unc.kind
    if OFFSHORE_CALIBRATION.applies(offshore, unc.source, lead_hours):
        k_lo, k_hi = OFFSHORE_CALIBRATION.factors(lead_hours)
        kind = "calibrated"
    return hs_m * math.exp(-Z80 * k_lo * s), hs_m * math.exp(Z80 * k_hi * s), kind


def confidence_index(sigma_forecast: float, data_completeness: float, stale: bool) -> int:
    """0–100 index of offshore forecast certainty (not a probability)."""
    value = 100.0 * math.exp(-max(0.0, sigma_forecast) / CONFIDENCE_SCALE)
    value *= 0.6 + 0.4 * max(0.0, min(1.0, data_completeness))
    if stale:
        value *= 0.8
    return int(round(max(0.0, min(100.0, value))))
