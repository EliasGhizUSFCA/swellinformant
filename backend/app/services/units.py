"""Unit conversions. Internally: metres, seconds, km/h, compass degrees."""

from __future__ import annotations

M_PER_FT = 0.3048


def m_to_ft(m: float) -> float:
    return m / M_PER_FT


def ft_to_m(ft: float) -> float:
    return ft * M_PER_FT


def kmh_to_knots(kmh: float) -> float:
    return kmh / 1.852


def kmh_to_mph(kmh: float) -> float:
    return kmh / 1.609344


def ms_to_kmh(ms: float) -> float:
    return ms * 3.6


def round_half(x: float) -> float:
    """Round to the nearest 0.5 (surf heights are never quoted more precisely)."""
    return round(x * 2.0) / 2.0


def height_to_ft(value: float, units: str) -> float:
    if units == "ft":
        return value
    if units == "m":
        return m_to_ft(value)
    raise ValueError(f"unsupported height unit {units!r}")


def ft_to_units(value_ft: float, units: str) -> float:
    if units == "ft":
        return value_ft
    if units == "m":
        return ft_to_m(value_ft)
    raise ValueError(f"unsupported height unit {units!r}")
