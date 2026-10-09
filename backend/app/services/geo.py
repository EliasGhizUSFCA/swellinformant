"""Angle, window and distance helpers.

All directions are compass degrees (0 = north, clockwise) that a swell or wind comes FROM.
Direction windows run clockwise from ``lo`` to ``hi`` and may wrap through north.
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def norm_deg(deg: float) -> float:
    return deg % 360.0


def angular_diff(a: float, b: float) -> float:
    """Smallest absolute angle between two compass directions, in [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def in_window(deg: float, lo: float, hi: float) -> bool:
    deg, lo, hi = norm_deg(deg), norm_deg(lo), norm_deg(hi)
    if lo <= hi:
        return lo <= deg <= hi
    return deg >= lo or deg <= hi


def window_distance(deg: float, lo: float, hi: float) -> float:
    """Degrees outside the clockwise window [lo, hi]; 0 when inside."""
    if in_window(deg, lo, hi):
        return 0.0
    return min(angular_diff(deg, lo), angular_diff(deg, hi))


def window_width(lo: float, hi: float) -> float:
    return (norm_deg(hi) - norm_deg(lo)) % 360.0


def window_center(lo: float, hi: float) -> float:
    return norm_deg(lo + window_width(lo, hi) / 2.0)


def window_contains_window(outer_lo: float, outer_hi: float, lo: float, hi: float) -> bool:
    """True if the clockwise window [lo, hi] lies inside [outer_lo, outer_hi]."""
    if not (in_window(lo, outer_lo, outer_hi) and in_window(hi, outer_lo, outer_hi)):
        return False
    return window_width(outer_lo, lo) <= window_width(outer_lo, hi)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def compass_label(deg: float | None) -> str:
    if deg is None:
        return "—"
    points = [
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    ]
    return points[int((norm_deg(deg) + 11.25) // 22.5) % 16]


def wind_from_uv(u: float, v: float) -> tuple[float, float]:
    """Convert eastward/northward wind components (m/s) to (speed m/s, direction FROM deg)."""
    speed = math.hypot(u, v)
    direction = norm_deg(270.0 - math.degrees(math.atan2(v, u))) if speed > 0 else 0.0
    return speed, direction
