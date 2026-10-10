"""Nearshore transformation: offshore swell partitions → significant breaking height.

Model ``linear_wave_v2`` (interpretable, per partition, no bathymetry required). For each
partition i (swells 1–3 and the wind sea) with height H_i, peak period T_i and direction
θ_i (degrees FROM):

1. **Deep-water equivalent.** When the depth h at the wave-model extraction point is
   known and shallower than half the deep-water wavelength, the height is de-shoaled with
   linear theory: H0_i = H_i / Ks, Ks = sqrt(Cg0 / Cg(h)). The wavenumber comes from the
   Fenton & McKee (1990) explicit approximation refined with one Newton step on
   ω² = g·k·tanh(k·h). When h is unknown the extraction point is assumed to be in deep
   water — for long swell over a 50–200 m deep grid point this misstates H0 by up to
   ~9 % (documented limitation).
2. **Directional factor.** R_i = Σ_θ D(θ; θ_i, σ_i) · max(cos(θ − β), 0) · T(θ) · Δθ,
   where D is a wrapped Gaussian spreading distribution (σ ≈ 10° for T ≥ 13 s, 15° for
   9–13 s, 30° for wind sea and short-period swell — assumptions, since GFS-Wave publishes
   no spread), β is the shore-normal azimuth of the break and T(θ) the *energy*
   transmission past islands and headlands (the spot's shadow table, or a smooth taper
   outside its swell window). cos(θ − β) carries the refraction loss: straight-contour
   refraction plus depth-limited breaking gives Hb ∝ (H0²·T·cos α0)^(2/5).
3. **Breaker height.** Hb_i = 0.39 · g^0.2 · (T_i · H0_i² · R_i)^0.4 · Kf_i — Komar &
   Gaughan (1972) with the directional factor; Kf is the spot's focusing prior,
   Kf(T) = Kf_ref · (1 + κ · max(0, T − T*) / T*).
4. **Combination by energy flux**: Hb = (Σ Hb_i^2.5)^0.4 (the flux physics behind the
   formula), replacing the root-sum-square of version 1. The ad-hoc ×0.7 wind-sea factor
   of version 1 is gone: short periods and onshore directions already reduce wind sea
   through T^0.4 and R.
5. **Depth limit**, only when the spot has a documented controlling depth d (reef ledge
   at chart datum): Hb ≤ γ · d.

Hb is the significant breaking height (≈ mean of the highest third of breaking faces,
trough to crest). Sets are reported as 1.27 · Hb (the Rayleigh H1/10 ratio; an upper
bound, since surf-zone height distributions are narrower).

Sources and the verification of each relation: docs/surf-forecast-model-research.md
(nearshore section). This model has NOT been calibrated against surf observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np

from app.services.geo import window_distance

G = 9.81
KOMAR_GAUGHAN = 0.39
SETS_RATIO = 1.27  # Rayleigh H1/10 / H1/3
DEFAULT_BREAKER_INDEX = 0.78  # McCowan solitary-wave limit
DIRECTION_STEP_DEG = 2.0
# Shadow taper outside the swell window when no shadow table exists: the amplitude falls
# off like exp(-(d/15°)²) (as in version 1), so the energy transmission is its square.
SHADOW_TAPER_DEG = 15.0
METHOD = "linear_wave_v2"


# --------------------------------------------------------------------------- linear theory
def wavenumber(period_s: float, depth_m: float) -> float:
    """Wavenumber k (rad/m) solving ω² = g·k·tanh(k·h).

    Starts from the Fenton & McKee (1990) explicit approximation (error < 1.7 %); Newton
    steps then converge quadratically (two or three reach machine precision).
    """
    if period_s <= 0 or depth_m <= 0:
        raise ValueError("period and depth must be positive")
    omega = 2 * math.pi / period_s
    k0 = omega**2 / G
    k0h = k0 * depth_m
    if k0h > 30:  # deep water to machine precision
        return k0
    k = k0 / math.tanh(k0h**0.75) ** (2.0 / 3.0)
    for _ in range(6):  # Newton on f(k) = g·k·tanh(k·h) − ω²
        th = math.tanh(k * depth_m)
        f = G * k * th - omega**2
        df = G * th + G * k * depth_m * (1 - th**2)
        step = f / df
        k -= step
        if abs(step) < 1e-14 * k:
            break
    return k


def group_velocity(period_s: float, depth_m: float) -> float:
    """Group velocity Cg = n·ω/k with n = 1/2 + k·h / sinh(2·k·h)."""
    k = wavenumber(period_s, depth_m)
    kh = k * depth_m
    n = 0.5 + (kh / math.sinh(2 * kh) if kh < 350 else 0.0)
    return n * (2 * math.pi / period_s) / k


def deep_water_group_velocity(period_s: float) -> float:
    return G * period_s / (4 * math.pi)


def deep_water_wavelength(period_s: float) -> float:
    return G * period_s**2 / (2 * math.pi)


def shoaling_coefficient(period_s: float, depth_m: float) -> float:
    """Ks = sqrt(Cg0 / Cg(h)); 1 in deep water."""
    if depth_m >= deep_water_wavelength(period_s) / 2:
        return 1.0
    return math.sqrt(deep_water_group_velocity(period_s) / group_velocity(period_s, depth_m))


def deep_water_height(height_m: float, period_s: float, depth_m: float | None) -> float:
    """De-shoal a height observed or forecast at depth ``depth_m`` (None = deep water)."""
    if depth_m is None:
        return height_m
    return height_m / shoaling_coefficient(period_s, depth_m)


def komar_gaughan_m(h0_m: float, period_s: float) -> float:
    """Komar & Gaughan (1972) breaker height (m) for normally incident waves."""
    if h0_m <= 0 or period_s <= 0:
        return 0.0
    return KOMAR_GAUGHAN * G**0.2 * (period_s * h0_m**2) ** 0.4


# --------------------------------------------------------------------------- directions
def spreading_sigma_deg(period_s: float, wind_sea: bool, override: float | None = None) -> float:
    """Directional spread (1σ) assumed for a partition."""
    if override is not None:
        return override
    if wind_sea or period_s < 9.0:
        return 30.0
    if period_s < 13.0:
        return 15.0
    return 10.0


def taper_transmission(window_distance_deg: float) -> float:
    """Energy transmission at ``window_distance_deg`` outside the swell window."""
    if window_distance_deg <= 0:
        return 1.0
    return math.exp(-2.0 * (window_distance_deg / SHADOW_TAPER_DEG) ** 2)


def table_transmission(table: list[float], direction_deg: float) -> float:
    """Linear interpolation in a 36-bin (10°) shadow table, circular."""
    x = (direction_deg % 360.0) / 10.0
    i0 = int(math.floor(x)) % 36
    frac = x - math.floor(x)
    return table[i0] * (1 - frac) + table[(i0 + 1) % 36] * frac


@dataclass(frozen=True)
class Profile:
    """Physical parameters of one break (see models.forecast_engine.SpotPhysicalProfile)."""

    shore_normal_deg: float
    swell_window_min: float
    swell_window_max: float
    forecast_point_depth_m: float | None = None
    controlling_depth_m: float | None = None
    breaker_index: float | None = None
    focusing_ref: float = 1.0
    focusing_kappa: float = 0.0
    focusing_t_star_s: float = 12.0
    shadow_table: tuple[float, ...] | None = None
    spreading_deg: float | None = None
    data_quality: dict[str, str] = field(default_factory=dict)

    def transmission(self, direction_deg: float) -> float:
        if self.shadow_table is not None:
            return table_transmission(list(self.shadow_table), direction_deg)
        return taper_transmission(
            window_distance(direction_deg, self.swell_window_min, self.swell_window_max)
        )

    def focusing(self, period_s: float) -> float:
        t_star = max(self.focusing_t_star_s, 1.0)
        return self.focusing_ref * (
            1.0 + self.focusing_kappa * max(0.0, period_s - t_star) / t_star
        )


# The directional factor is evaluated for every partition of every forecast hour, so the
# profile's angular response g(θ) = max(cos(θ − β), 0) · T(θ) is tabulated once per
# profile on a fine grid and convolved with a cached Gaussian kernel.
RESPONSE_STEP_DEG = 0.5
_GRID = np.arange(0.0, 360.0, RESPONSE_STEP_DEG)
_RESPONSE_CACHE: dict[tuple[Any, ...], np.ndarray] = {}
_RESPONSE_CACHE_MAX = 4096


def angular_response(profile: Profile) -> np.ndarray:
    key = (
        profile.shore_normal_deg,
        profile.swell_window_min,
        profile.swell_window_max,
        profile.shadow_table,
    )
    g = _RESPONSE_CACHE.get(key)
    if g is None:
        incidence = np.cos(np.radians(_GRID - profile.shore_normal_deg))
        transmission = np.array([profile.transmission(float(t)) for t in _GRID])
        g = np.where(incidence > 0, incidence * transmission, 0.0)
        if len(_RESPONSE_CACHE) >= _RESPONSE_CACHE_MAX:
            _RESPONSE_CACHE.clear()
        _RESPONSE_CACHE[key] = g
    return g


@lru_cache(maxsize=512)
def _spreading_kernel(sigma_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Offsets (degrees) and normalised weights of a Gaussian truncated at ±4σ."""
    half = min(180.0, 4.0 * sigma_deg)
    steps = int(2 * half / DIRECTION_STEP_DEG)
    offsets = -half + np.arange(steps + 1) * DIRECTION_STEP_DEG
    weights = np.exp(-0.5 * (offsets / sigma_deg) ** 2)
    return offsets, weights / weights.sum()


_FACTOR_CACHE: dict[tuple[Any, ...], float] = {}
_FACTOR_CACHE_MAX = 200_000


def directional_factor(direction_deg: float, sigma_deg: float, profile: Profile) -> float:
    """R = Σ D(θ) · max(cos(θ − β), 0) · T(θ) · Δθ over a wrapped-Gaussian spread.

    Memoised at RESPONSE_STEP_DEG resolution of the mean direction (the response is
    smooth on that scale; forecast directions are not more precise than that).
    """
    key = (
        profile.shore_normal_deg,
        profile.swell_window_min,
        profile.swell_window_max,
        profile.shadow_table,
        round((direction_deg % 360.0) / RESPONSE_STEP_DEG),
        float(sigma_deg),
    )
    cached = _FACTOR_CACHE.get(key)
    if cached is not None:
        return cached
    value = _directional_factor(key[4] * RESPONSE_STEP_DEG, float(sigma_deg), profile)
    if len(_FACTOR_CACHE) >= _FACTOR_CACHE_MAX:
        _FACTOR_CACHE.clear()
    _FACTOR_CACHE[key] = value
    return value


def _directional_factor(direction_deg: float, sigma_deg: float, profile: Profile) -> float:
    offsets, weights = _spreading_kernel(sigma_deg)
    g = angular_response(profile)
    # Linear interpolation on the uniform circular grid (np.interp(period=) re-sorts).
    x = ((direction_deg + offsets) % 360.0) / RESPONSE_STEP_DEG
    i0 = np.floor(x).astype(np.intp)
    frac = x - i0
    i0 %= g.size
    values = g[i0] * (1.0 - frac) + g[(i0 + 1) % g.size] * frac
    return float(np.dot(weights, values))


# --------------------------------------------------------------------------- breaking
@dataclass(frozen=True)
class Partition:
    name: str
    height_m: float
    period_s: float
    direction_deg: float | None
    wind_sea: bool = False


@dataclass
class PartitionBreaking:
    name: str
    height_m: float
    period_s: float
    direction_deg: float | None
    deep_water_height_m: float
    directional_factor: float
    focusing: float
    breaking_m: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "height_m": round(self.height_m, 3),
            "period_s": round(self.period_s, 2),
            "direction_deg": None if self.direction_deg is None else round(self.direction_deg),
            "h0_m": round(self.deep_water_height_m, 3),
            "R": round(self.directional_factor, 3),
            "Kf": round(self.focusing, 3),
            "breaking_m": round(self.breaking_m, 3),
        }


# Unknown direction: a conservative R equal to a swell 60° off the shore normal.
UNKNOWN_DIRECTION_R = math.cos(math.radians(60.0))


def partition_breaking(p: Partition, profile: Profile) -> PartitionBreaking:
    h0 = deep_water_height(p.height_m, p.period_s, profile.forecast_point_depth_m)
    if p.direction_deg is None:
        r = UNKNOWN_DIRECTION_R
    else:
        sigma = spreading_sigma_deg(p.period_s, p.wind_sea, profile.spreading_deg)
        r = directional_factor(p.direction_deg, sigma, profile)
    kf = profile.focusing(p.period_s)
    hb = KOMAR_GAUGHAN * G**0.2 * (p.period_s * h0**2 * r) ** 0.4 * kf if r > 0 else 0.0
    return PartitionBreaking(p.name, p.height_m, p.period_s, p.direction_deg, h0, r, kf, hb)


def combine_flux(heights_m: list[float]) -> float:
    """Hb = (Σ Hb_i^2.5)^0.4 — breaking heights add through their energy flux."""
    total = sum(h**2.5 for h in heights_m if h > 0)
    return total**0.4 if total > 0 else 0.0


@dataclass
class NearshoreEstimate:
    breaking_m: float  # significant breaking height
    sets_m: float
    partitions: list[PartitionBreaking]
    depth_limited: bool
    assumptions: list[str]


def estimate(partitions: list[Partition], profile: Profile) -> NearshoreEstimate:
    parts = [
        partition_breaking(p, profile) for p in partitions if p.height_m > 0 and p.period_s > 0
    ]
    hb = combine_flux([p.breaking_m for p in parts])
    assumptions: list[str] = []
    if profile.forecast_point_depth_m is None:
        assumptions.append("forecast point assumed to be in deep water")
    if profile.shadow_table is None:
        assumptions.append("shadowing from the swell window (no shadow table)")
    if profile.data_quality.get("shore_normal_deg") in (None, "estimated", "unknown"):
        assumptions.append("shore normal estimated")
    if profile.data_quality.get("focusing_ref") in (None, "estimated", "unknown"):
        assumptions.append("focusing factor is an uncalibrated prior")
    depth_limited = False
    if profile.controlling_depth_m is not None:
        cap = (profile.breaker_index or DEFAULT_BREAKER_INDEX) * profile.controlling_depth_m
        if hb > cap:
            hb, depth_limited = cap, True
    return NearshoreEstimate(hb, hb * SETS_RATIO, parts, depth_limited, assumptions)


def deep_water_height_for_breaking(
    target_breaking_m: float, period_s: float, direction_deg: float, profile: Profile
) -> float:
    """Invert ``estimate`` for one swell partition (bisection). Used by the demo provider."""
    lo, hi = 0.01, 30.0
    for _ in range(60):
        mid = (lo + hi) / 2
        got = estimate([Partition("swell", mid, period_s, direction_deg)], profile).breaking_m
        if got < target_breaking_m:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
