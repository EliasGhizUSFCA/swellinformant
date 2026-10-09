"""Offshore swell → estimated breaking (face) wave height at a specific break.

IMPORTANT: offshore significant wave height (Hs) is not the breaking wave height. Waves
shoal, refract, focus or lose energy depending on bathymetry and exposure. This module
uses:

1. **Calibrated spot model** (when ``spot.calibration`` exists, i.e. coefficients were
   fitted from enough observations):  Hb_ft = a · H0^b · T^c

2. **Approximate model** otherwise (always labelled as such):
   * Komar & Gaughan (1972) empirical breaker height for each swell partition:
       Hb = 0.39 · g^(1/5) · (T · H0²)^(2/5)
   * × directional exposure: 1 inside the spot's optimal window, cos-shaped falloff
     outside it, and a steep Gaussian falloff once outside the exposure window
     (islands/headlands block the swell)
   * × ``height_factor``, an uncalibrated per-spot heuristic (e.g. Nazaré's canyon
     focuses energy > 1, sheltered points < 1)
   * partitions combine by energy:  Hb = sqrt(Σ Hb_i²)
   * the reported range is 0.8–1.3 × the central estimate (average waves → sets).

The approximate model has NOT been validated against observations for these spots; it
is a transparent first-order estimate. See docs/SURF_SCORING.md.
"""

from __future__ import annotations

import math
from typing import Any

from app.services.geo import window_distance
from app.services.surf_quality.models import BreakingEstimate, SeaState, SpotParams
from app.services.units import m_to_ft, round_half

G = 9.81
RANGE_LOW = 0.8
RANGE_HIGH = 1.3
WIND_SEA_FACTOR = 0.7  # short-period wind sea breaks weaker than its Hs suggests
METHOD_APPROX = "komar_gaughan_v1_uncalibrated"
METHOD_CALIBRATED = "calibrated_power_law"


def komar_gaughan_m(h0_m: float, period_s: float) -> float:
    """Komar & Gaughan (1972) breaker height (m) from deep-water height and period."""
    if h0_m <= 0 or period_s <= 0:
        return 0.0
    return 0.39 * G**0.2 * (period_s * h0_m**2) ** 0.4


def exposure_factor(direction_deg: float | None, spot: SpotParams) -> float:
    """Fraction of a swell's height that reaches the break from ``direction_deg``."""
    if direction_deg is None:
        return 0.6  # unknown direction: conservative
    d_opt = window_distance(
        direction_deg, spot.optimal_swell_direction_min, spot.optimal_swell_direction_max
    )
    factor = math.sqrt(max(0.0, math.cos(math.radians(min(d_opt, 85.0)))))
    d_exp = window_distance(direction_deg, spot.swell_window_min, spot.swell_window_max)
    if d_exp > 0:
        factor *= math.exp(-((d_exp / 15.0) ** 2))
    return factor


def _partitions(sea: SeaState) -> list[tuple[str, float, float, float | None, float]]:
    """(name, height m, period s, direction, multiplier) for each available partition."""
    parts: list[tuple[str, float, float, float | None, float]] = []
    if sea.primary_swell_height_m and sea.primary_swell_period_s:
        parts.append(
            (
                "primary_swell",
                sea.primary_swell_height_m,
                sea.primary_swell_period_s,
                sea.primary_swell_direction_deg,
                1.0,
            )
        )
    if sea.secondary_swell_height_m and sea.secondary_swell_period_s:
        parts.append(
            (
                "secondary_swell",
                sea.secondary_swell_height_m,
                sea.secondary_swell_period_s,
                sea.secondary_swell_direction_deg,
                1.0,
            )
        )
    if sea.wind_wave_height_m and sea.wind_wave_period_s:
        parts.append(
            (
                "wind_sea",
                sea.wind_wave_height_m,
                sea.wind_wave_period_s,
                sea.wind_wave_direction_deg,
                WIND_SEA_FACTOR,
            )
        )
    if not parts and sea.sig_wave_height_m and sea.wave_period_s:
        # Only bulk parameters available: treat the combined sea state as one partition.
        parts.append(
            ("combined", sea.sig_wave_height_m, sea.wave_period_s, sea.wave_direction_deg, 1.0)
        )
    return parts


def estimate_breaking_height(sea: SeaState, spot: SpotParams) -> BreakingEstimate:
    cal = spot.calibration or {}
    if {"a", "b", "c"} <= cal.keys() and sea.primary_swell_height_m and sea.primary_swell_period_s:
        mid = (
            float(cal["a"])
            * sea.primary_swell_height_m ** float(cal["b"])
            * (sea.primary_swell_period_s ** float(cal["c"]))
        )
        mid *= exposure_factor(sea.primary_swell_direction_deg, spot)
        return BreakingEstimate(
            min_ft=round_half(mid * RANGE_LOW),
            max_ft=round_half(mid * RANGE_HIGH),
            mid_ft=round(mid, 2),
            method=METHOD_CALIBRATED,
            calibrated=True,
        )

    energy = 0.0
    details: list[dict[str, Any]] = []
    for name, h, t, d, mult in _partitions(sea):
        hb_m = komar_gaughan_m(h, t) * exposure_factor(d, spot) * mult
        energy += hb_m**2
        details.append({"name": name, "height_m": h, "period_s": t, "breaking_ft": m_to_ft(hb_m)})
    mid_ft = m_to_ft(math.sqrt(energy)) * spot.height_factor
    return BreakingEstimate(
        min_ft=round_half(mid_ft * RANGE_LOW),
        max_ft=round_half(mid_ft * RANGE_HIGH),
        mid_ft=round(mid_ft, 2),
        method=METHOD_APPROX,
        calibrated=False,
        partitions=details,
    )
