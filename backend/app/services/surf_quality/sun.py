"""Sunrise / sunset from the NOAA "General Solar Position Calculations" approximation.

Accuracy is about ±2 minutes at low/mid latitudes, which is far better than needed to
decide whether an hourly forecast step is surfable daylight. Polar day/night are handled.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

# Surfers paddle out at first light and stay until dusk: extend the window a little.
TWILIGHT_MARGIN = timedelta(minutes=20)


def _solar_terms(day: date) -> tuple[float, float]:
    """Return (equation of time in minutes, solar declination in radians) at solar noon."""
    doy = day.timetuple().tm_yday
    gamma = 2.0 * math.pi / 365.0 * (doy - 1)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return eqtime, decl


def sun_times(lat: float, lon: float, day: date) -> tuple[datetime | None, datetime | None, str]:
    """Sunrise and sunset (UTC) for the *local solar* date ``day`` at (lat, lon).

    Returns (sunrise, sunset, state) where state is "normal", "polar_day" or "polar_night".
    """
    eqtime, decl = _solar_terms(day)
    lat_r = math.radians(lat)
    zenith = math.radians(90.833)  # refraction + solar disc radius
    cos_ha = math.cos(zenith) / (math.cos(lat_r) * math.cos(decl)) - math.tan(lat_r) * math.tan(
        decl
    )
    if cos_ha > 1:
        return None, None, "polar_night"
    if cos_ha < -1:
        return None, None, "polar_day"
    ha = math.degrees(math.acos(cos_ha))
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    sunrise = base + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sunset = base + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return sunrise, sunset, "normal"


def local_solar_date(lon: float, when: datetime) -> date:
    return (when.astimezone(UTC) + timedelta(hours=lon / 15.0)).date()


def is_daylight(lat: float, lon: float, when: datetime) -> bool:
    """Whether ``when`` falls between first light and dusk at the location."""
    when = when.astimezone(UTC)
    sunrise, sunset, state = sun_times(lat, lon, local_solar_date(lon, when))
    if state == "polar_day":
        return True
    if state == "polar_night" or sunrise is None or sunset is None:
        return False
    return sunrise - TWILIGHT_MARGIN <= when <= sunset + TWILIGHT_MARGIN
