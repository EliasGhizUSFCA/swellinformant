"""Structured claim values and their comparison with a spot profile.

A research claim may carry a normalised value (``spot_research_claims.value``). Each
variable has one accepted shape:

======================  =====================================  ==========================
variable                value shape                            compared with (profile)
======================  =====================================  ==========================
swell_direction         {"min": deg, "max": deg}               optimal swell window
wind                    {"offshore_from_deg": deg}             offshore wind direction
tide                    {"tide": TidePreference value}         tide preference
swell_size              {"min_ft": ft|null, "max_ft": ft|null} working face-height range
swell_period            {"min_s": s|null, "max_s": s|null}     min .. ideal swell period
seasonality             {"months": [1..12, ...]}               best months
bathymetry              null (descriptive)                     not compared
local_behavior          null (descriptive)                     not compared
======================  =====================================  ==========================

Assumptions (documented in docs/research-confidence.md):

* Directions are nautical degrees the swell or wind comes FROM; a window runs clockwise
  from ``min`` to ``max`` and may wrap through north (``{"min": 300, "max": 20}``). A
  window with only one bound (``{"min": 320}``, "320 degrees or more") states one
  direction that works; it is compared as that single direction and is never exact,
  because the open side of a circular range is undefined.
* Sizes are breaking face heights in feet, the unit of the profile's working range, never
  offshore significant wave height and never Hawaiian scale. A one-sided range (one bound
  null or omitted) is open on that side; the same holds for periods.
* The profile's period range runs from ``min_swell_period_s`` (rarely works below) to
  ``ideal_swell_period_s``; a source range agrees when it overlaps it.

"Agrees" uses the tolerances of the research-v1 methodology (direction windows overlap
once the profile window is widened by 30 degrees on each side, offshore wind within 45
degrees, same or adjacent tide class, overlapping ranges or months). "Exact" is stricter:
the value is the profile's value at the precision sources state it (direction endpoints
and wind within 11.25 degrees, i.e. the same 16-point compass name; the same tide class;
both range bounds within 0.5 ft / 0.5 s; the same set of months).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.models import SurfSpot
from app.models.enums import BreakType, TidePreference
from app.services.geo import angular_diff, in_window, norm_deg, window_width

DIRECTION_TOLERANCE_DEG = 30.0
WIND_TOLERANCE_DEG = 45.0
EXACT_DIRECTION_DEG = 11.25  # half of one 16-point compass sector
EXACT_RANGE_FT = 0.5
EXACT_RANGE_S = 0.5
MAX_PERIOD_S = 30.0
MAX_FACE_HEIGHT_FT = 150.0

# Variables whose claims describe rather than state a comparable value.
DESCRIPTIVE_VARIABLES = frozenset({"bathymetry", "local_behavior"})
# Range variables whose value may state only one bound (the other key omitted or null).
OPTIONAL_BOUND_VARIABLES = frozenset({"swell_direction", "swell_size", "swell_period"})

# Accepted keys of a structured value, per variable (None = no structured value).
# For the range variables (OPTIONAL_BOUND_VARIABLES) one of the two keys may be omitted.
VALUE_KEYS: dict[str, frozenset[str] | None] = {
    "swell_direction": frozenset({"min", "max"}),
    "wind": frozenset({"offshore_from_deg"}),
    "tide": frozenset({"tide"}),
    "swell_size": frozenset({"min_ft", "max_ft"}),
    "swell_period": frozenset({"min_s", "max_s"}),
    "seasonality": frozenset({"months"}),
    "bathymetry": None,
    "local_behavior": None,
}

# Tide classes in order from low to high water; "all" stands apart (no preference).
TIDE_ORDER = ("low", "low_to_mid", "mid", "mid_to_high", "high")

# Breaks treated as sand-bottom for the recency rule (sandbars move): beach and rivermouth
# breaks always; other types when their curated description names a sand bottom.
SAND_BREAK_TYPES = frozenset({BreakType.BEACH, BreakType.RIVERMOUTH})
SAND_TERMS = ("sand-bottom", "sand bottom", "sandbar", "sand bar", "sand spit")


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def validate_value(variable: str, value: Any) -> list[str]:
    """Problems with a claim's structured value (empty list = valid). ``value`` may be None."""
    if value is None:
        return []
    keys = VALUE_KEYS.get(variable)
    if keys is None:
        return [f"{variable} takes no structured value (descriptive variable); use null"]
    if not isinstance(value, dict):
        return [f"value must be an object with keys {sorted(keys)} or null"]
    if variable in OPTIONAL_BOUND_VARIABLES:
        if not value or not set(value) <= keys:
            return [
                f"value for {variable} must have the keys {sorted(keys)} "
                "(one of them may be omitted)"
            ]
    elif set(value) != keys:
        return [f"value for {variable} must have exactly the keys {sorted(keys)}"]
    problems: list[str] = []
    if variable == "swell_direction":
        for k in ("min", "max"):
            if k in value and (not _is_number(value[k]) or not 0 <= value[k] <= 360):
                problems.append(f"value.{k} must be a direction in degrees 0-360")
    elif variable == "wind":
        v = value["offshore_from_deg"]
        if not _is_number(v) or not 0 <= v <= 360:
            problems.append("value.offshore_from_deg must be a direction in degrees 0-360")
    elif variable == "tide":
        if value["tide"] not in tuple(t.value for t in TidePreference):  # tuple: any JSON value
            allowed = ", ".join(t.value for t in TidePreference)
            problems.append(f"value.tide must be one of {allowed}")
    elif variable in ("swell_size", "swell_period"):
        lo_key, hi_key, limit, unit = (
            ("min_ft", "max_ft", MAX_FACE_HEIGHT_FT, "ft")
            if variable == "swell_size"
            else ("min_s", "max_s", MAX_PERIOD_S, "s")
        )
        lo: Any = value.get(lo_key)  # an omitted bound is null
        hi: Any = value.get(hi_key)
        for k, v in ((lo_key, lo), (hi_key, hi)):
            if v is not None and (not _is_number(v) or not 0 < v <= limit):
                problems.append(f"value.{k} must be null or a number in (0, {limit:g}] {unit}")
        if lo is None and hi is None:
            problems.append(f"value.{lo_key} and value.{hi_key} cannot both be null")
        elif _is_number(lo) and _is_number(hi) and lo > hi:
            problems.append(f"value.{lo_key} is greater than value.{hi_key}")
    elif variable == "seasonality":
        months = value["months"]
        if (
            not isinstance(months, list)
            or not months
            or not all(isinstance(m, int) and not isinstance(m, bool) for m in months)
            or not all(1 <= m <= 12 for m in months)
            or len(set(months)) != len(months)
        ):
            problems.append("value.months must be a non-empty list of distinct months 1-12")
    return problems


def is_sand_bottom(spot: SurfSpot) -> bool:
    """True for breaks whose bottom is (mostly) sand, from break type and description."""
    if spot.break_type in SAND_BREAK_TYPES:
        return True
    text = (spot.description or "").lower()
    return any(term in text for term in SAND_TERMS)


def profile_values(spot: SurfSpot) -> dict[str, Any]:
    """The spot-profile values that claims are compared with (also fingerprinted)."""
    return {
        "optimal_swell_direction_min": float(spot.optimal_swell_direction_min),
        "optimal_swell_direction_max": float(spot.optimal_swell_direction_max),
        "offshore_wind_direction": float(spot.offshore_wind_direction),
        # str(): during seeding the attributes may still hold the raw JSON strings.
        "tide_preference": str(spot.tide_preference) if spot.tide_preference else None,
        "wave_height_min_ft": float(spot.wave_height_min_ft),
        "wave_height_max_ft": float(spot.wave_height_max_ft),
        "min_swell_period_s": float(spot.min_swell_period_s),
        "ideal_swell_period_s": float(spot.ideal_swell_period_s),
        "best_months": sorted(int(m) for m in (spot.best_months or [])),
        "break_type": str(spot.break_type),
        "sand_bottom": is_sand_bottom(spot),
    }


@dataclass(frozen=True)
class Comparison:
    """Result of comparing one structured value with the profile.

    ``agrees`` is None when the profile has nothing to compare with (e.g. no tide
    preference or no best months). ``exact`` implies ``agrees``.
    """

    agrees: bool | None
    exact: bool = False


NOT_COMPARABLE = Comparison(None)


def _arcs_overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    """Whether two clockwise arcs (each may wrap through north) share a direction."""
    return in_window(a_lo, b_lo, b_hi) or in_window(b_lo, a_lo, a_hi)


def _ranges_overlap(
    lo: float | None, hi: float | None, profile_lo: float, profile_hi: float
) -> bool:
    lo_v = -math.inf if lo is None else lo
    hi_v = math.inf if hi is None else hi
    return lo_v <= profile_hi and profile_lo <= hi_v


def _range_exact(
    lo: float | None, hi: float | None, profile_lo: float, profile_hi: float, tol: float
) -> bool:
    return (
        lo is not None
        and hi is not None
        and abs(lo - profile_lo) <= tol
        and abs(hi - profile_hi) <= tol
    )


def compare_value(variable: str, value: dict[str, Any], profile: dict[str, Any]) -> Comparison:
    """Compare a (validated) structured value with ``profile_values(spot)``."""
    if variable == "swell_direction":
        # One stated bound: that single direction (lo == hi), never an exact match.
        one_bound = "min" not in value or "max" not in value
        lo = norm_deg(value["min"] if "min" in value else value["max"])
        hi = norm_deg(value["max"] if "max" in value else value["min"])
        p_lo = profile["optimal_swell_direction_min"]
        p_hi = profile["optimal_swell_direction_max"]
        if window_width(p_lo, p_hi) + 2 * DIRECTION_TOLERANCE_DEG >= 360:
            agrees = True
        else:
            agrees = _arcs_overlap(
                lo, hi, p_lo - DIRECTION_TOLERANCE_DEG, p_hi + DIRECTION_TOLERANCE_DEG
            )
        exact = (
            not one_bound
            and angular_diff(lo, p_lo) <= EXACT_DIRECTION_DEG
            and angular_diff(hi, p_hi) <= EXACT_DIRECTION_DEG
        )
        return Comparison(agrees, agrees and exact)
    if variable == "wind":
        diff = angular_diff(value["offshore_from_deg"], profile["offshore_wind_direction"])
        return Comparison(diff <= WIND_TOLERANCE_DEG, diff <= EXACT_DIRECTION_DEG)
    if variable == "tide":
        mine, theirs = profile["tide_preference"], value["tide"]
        if mine is None:
            return NOT_COMPARABLE
        if mine == theirs:
            return Comparison(True, True)
        if "all" in (mine, theirs):
            return Comparison(False)
        return Comparison(abs(TIDE_ORDER.index(mine) - TIDE_ORDER.index(theirs)) <= 1)
    if variable == "swell_size":
        args = (value.get("min_ft"), value.get("max_ft"))
        bounds = (profile["wave_height_min_ft"], profile["wave_height_max_ft"])
        agrees = _ranges_overlap(*args, *bounds)
        return Comparison(agrees, agrees and _range_exact(*args, *bounds, EXACT_RANGE_FT))
    if variable == "swell_period":
        args = (value.get("min_s"), value.get("max_s"))
        bounds = (profile["min_swell_period_s"], profile["ideal_swell_period_s"])
        agrees = _ranges_overlap(*args, *bounds)
        return Comparison(agrees, agrees and _range_exact(*args, *bounds, EXACT_RANGE_S))
    if variable == "seasonality":
        best = set(profile["best_months"])
        if not best:
            return NOT_COMPARABLE
        months = set(value["months"])
        return Comparison(bool(months & best), months == best)
    return NOT_COMPARABLE
