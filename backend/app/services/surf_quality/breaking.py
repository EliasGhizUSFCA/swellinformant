"""Offshore swell → estimated breaking (face) wave height at a specific break.

IMPORTANT: offshore significant wave height (Hs) is not the breaking wave height. Waves
shoal, refract, focus or lose energy depending on bathymetry and exposure.

1. **Calibrated spot model** (when ``spot.calibration`` exists, i.e. coefficients were
   fitted from enough nearshore observations):  Hb_ft = a · H0^b · T^c · R^d

2. **Physical model** ``linear_wave_v2`` otherwise (see ``nearshore.py``): de-shoaling to
   deep water when the extraction depth is known, directional spreading against the
   break's shore normal and shadow table, Komar & Gaughan breaking per partition,
   period-dependent focusing prior, energy-flux combination and an optional depth limit.

The reported range runs from the significant breaking height (typical larger waves) to
the sets (1.27 × Hb). Version 1 (``komar_gaughan_v1_uncalibrated``: root-sum-square of
partitions, ×0.7 wind sea, 0.8–1.3 × mid) is kept for comparison in backtests.

Neither model has been validated against observed surf at these spots; no open surf
observation dataset exists for them. See docs/SURF_SCORING.md.
"""

from __future__ import annotations

import math
from typing import Any

from app.services.geo import window_distance
from app.services.surf_quality import nearshore
from app.services.surf_quality.models import BreakingEstimate, SeaState, SpotParams
from app.services.units import m_to_ft, round_half

G = nearshore.G
RANGE_LOW = 0.8  # version 1 only
RANGE_HIGH = 1.3  # version 1 only
WIND_SEA_FACTOR = 0.7  # version 1 only
METHOD_APPROX_V1 = "komar_gaughan_v1_uncalibrated"
METHOD_PHYSICAL = nearshore.METHOD
METHOD_CALIBRATED = "calibrated_power_law"
# Kept for code that still refers to the previous name.
METHOD_APPROX = METHOD_APPROX_V1

komar_gaughan_m = nearshore.komar_gaughan_m


def partitions_from_sea(sea: SeaState) -> list[nearshore.Partition]:
    """Swell partitions 1–3 and the wind sea; bulk parameters only if nothing else exists."""
    parts: list[nearshore.Partition] = []
    for name, h, t, d in (
        (
            "primary_swell",
            sea.primary_swell_height_m,
            sea.primary_swell_period_s,
            sea.primary_swell_direction_deg,
        ),
        (
            "secondary_swell",
            sea.secondary_swell_height_m,
            sea.secondary_swell_period_s,
            sea.secondary_swell_direction_deg,
        ),
        (
            "tertiary_swell",
            sea.tertiary_swell_height_m,
            sea.tertiary_swell_period_s,
            sea.tertiary_swell_direction_deg,
        ),
    ):
        if h and t:
            parts.append(nearshore.Partition(name, h, t, d))
    if sea.wind_wave_height_m and sea.wind_wave_period_s:
        parts.append(
            nearshore.Partition(
                "wind_sea",
                sea.wind_wave_height_m,
                sea.wind_wave_period_s,
                sea.wind_wave_direction_deg,
                wind_sea=True,
            )
        )
    if not parts and sea.sig_wave_height_m and sea.wave_period_s:
        # Only bulk parameters (e.g. ECMWF): treat the combined sea state as one partition.
        parts.append(
            nearshore.Partition(
                "combined", sea.sig_wave_height_m, sea.wave_period_s, sea.wave_direction_deg
            )
        )
    return parts


def _calibrated(sea: SeaState, spot: SpotParams, cal: dict[str, Any]) -> BreakingEstimate | None:
    h, t = sea.primary_swell_height_m, sea.primary_swell_period_s
    if not (h and t):
        return None
    profile = spot.physical_profile()
    r = 1.0
    if sea.primary_swell_direction_deg is not None:
        sigma = nearshore.spreading_sigma_deg(t, False, profile.spreading_deg)
        r = nearshore.directional_factor(sea.primary_swell_direction_deg, sigma, profile)
    if r <= 0:
        mid = 0.0
    else:
        mid = (
            float(cal["a"])
            * h ** float(cal["b"])
            * t ** float(cal["c"])
            * r ** float(cal.get("d", 0.4))
        )
    return BreakingEstimate(
        min_ft=round_half(mid),
        max_ft=round_half(mid * nearshore.SETS_RATIO),
        mid_ft=round(mid, 2),
        method=METHOD_CALIBRATED,
        calibrated=True,
    )


def estimate_breaking_height(sea: SeaState, spot: SpotParams) -> BreakingEstimate:
    cal = spot.calibration or {}
    if {"a", "b", "c"} <= cal.keys():
        calibrated = _calibrated(sea, spot, cal)
        if calibrated is not None:
            return calibrated

    result = nearshore.estimate(partitions_from_sea(sea), spot.physical_profile())
    mid_ft = m_to_ft(result.breaking_m)
    return BreakingEstimate(
        min_ft=round_half(mid_ft),
        max_ft=round_half(m_to_ft(result.sets_m)),
        mid_ft=round(mid_ft, 2),
        method=METHOD_PHYSICAL,
        calibrated=False,
        partitions=[
            {**p.as_dict(), "breaking_ft": round(m_to_ft(p.breaking_m), 2)}
            for p in result.partitions
        ],
        assumptions=result.assumptions,
        depth_limited=result.depth_limited,
    )


# --------------------------------------------------------------------------- version 1
def exposure_factor(direction_deg: float | None, spot: SpotParams) -> float:
    """Version 1: fraction of a swell's height that reaches the break from a direction."""
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


def estimate_breaking_height_v1(sea: SeaState, spot: SpotParams) -> BreakingEstimate:
    """The version 1 heuristic, kept so backtests can compare it with version 2."""
    energy = 0.0
    details: list[dict[str, Any]] = []
    for p in partitions_from_sea(sea):
        mult = WIND_SEA_FACTOR if p.wind_sea else 1.0
        hb_m = komar_gaughan_m(p.height_m, p.period_s) * exposure_factor(p.direction_deg, spot)
        hb_m *= mult
        energy += hb_m**2
        details.append({"name": p.name, "breaking_ft": m_to_ft(hb_m)})
    mid_ft = m_to_ft(math.sqrt(energy)) * spot.height_factor
    return BreakingEstimate(
        min_ft=round_half(mid_ft * RANGE_LOW),
        max_ft=round_half(mid_ft * RANGE_HIGH),
        mid_ft=round(mid_ft, 2),
        method=METHOD_APPROX_V1,
        calibrated=False,
        partitions=details,
    )
