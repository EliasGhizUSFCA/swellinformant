"""Blended wind: direction of the mean wind vector, model disagreement on the direction and
the gust's own members (blend.blend_hour, blend_wind_direction, compare_hour, gust_members).

Every value here is a SYNTHETIC test input, not a forecast.
"""

from __future__ import annotations

import math

import pytest

from app.services.forecasts import blend
from app.services.forecasts.base import FORECAST_FIELDS
from app.services.surf_quality.wind import wind_components, wind_relation


def _hour(
    code: str, family: str, speed: float | None, direction: float | None, **kw: float
) -> blend.HourValues:
    values: dict[str, float | None] = {k: None for k in FORECAST_FIELDS}
    values.update(sig_wave_height_m=1.5, wind_speed_kmh=speed, wind_direction_deg=direction)
    values.update(kw)
    return blend.HourValues(code, family, 24.0, values, "tp", gust_kind=None)


EQUAL = {"gfs": 0.5, "ecmwf": 0.5}


def test_direction_is_the_mean_wind_vector_not_the_mean_of_unit_vectors() -> None:
    # Rincon-like: a calm member barely turns a strong one (unit vectors turned it 70 deg).
    calm = _hour("gfs", "ncep_gfs", 3.0, 54.0)
    strong = _hour("ecmwf", "ecmwf_ifs", 26.0, 277.0)
    direction = blend.blend_wind_direction([calm, strong], EQUAL)
    assert direction is not None
    assert blend.angular_diff(direction, 277.0) < 8.0
    # The mean vector of (3 from 54) and (26 from 277), computed by hand.
    u = -(3.0 * math.sin(math.radians(54.0)) + 26.0 * math.sin(math.radians(277.0))) / 2
    v = -(3.0 * math.cos(math.radians(54.0)) + 26.0 * math.cos(math.radians(277.0))) / 2
    assert direction == pytest.approx(math.degrees(math.atan2(-u, -v)) % 360.0)


def test_equal_members_average_to_the_bisector_and_weights_count() -> None:
    a = _hour("gfs", "ncep_gfs", 20.0, 0.0)
    b = _hour("ecmwf", "ecmwf_ifs", 20.0, 90.0)
    assert blend.blend_wind_direction([a, b], EQUAL) == pytest.approx(45.0)
    lopsided = blend.blend_wind_direction([a, b], {"gfs": 0.9, "ecmwf": 0.1})
    assert lopsided is not None and lopsided < 10.0
    # Across north: 350 and 10 average to 0, not 180.
    c = _hour("gfs", "ncep_gfs", 15.0, 350.0)
    d = _hour("ecmwf", "ecmwf_ifs", 15.0, 10.0)
    assert blend.angular_diff(blend.blend_wind_direction([c, d], EQUAL) or 0.0, 0.0) < 1e-6


def test_exactly_opposing_and_calm_members_keep_a_member_direction() -> None:
    a = _hour("gfs", "ncep_gfs", 13.0, 80.0)
    b = _hour("ecmwf", "ecmwf_ifs", 13.0, 260.0)
    assert blend.blend_wind_direction([a, b], EQUAL) in (80.0, 260.0)
    calm = [_hour("gfs", "ncep_gfs", 0.0, 80.0), _hour("ecmwf", "ecmwf_ifs", 0.0, 200.0)]
    assert blend.blend_wind_direction(calm, EQUAL) in (80.0, 200.0)
    assert blend.blend_wind_direction([_hour("gfs", "ncep_gfs", None, None)], {"gfs": 1.0}) is None


def test_opposing_members_keep_the_mean_speed_and_are_flagged() -> None:
    """Trestles-like (offshore from 79 deg at 13 km/h vs onshore from 261 deg at 16 km/h):
    the blend keeps the members' mean speed (the vector mean's 2 km/h would read as glassy,
    which neither model forecasts), takes the stronger member's side and flags the hour, so
    forecast confidence is capped and the wind is shown with a disagreement note."""
    gfs = _hour("gfs", "ncep_gfs", 13.0, 79.0)
    ec = _hour("ecmwf", "ecmwf_ifs", 16.0, 261.0)
    values, weights, _, _ = blend.blend_hour([gfs, ec], blend.SkillTable())
    assert weights == pytest.approx(EQUAL)
    assert values["wind_speed_kmh"] == pytest.approx(14.5)
    direction = values["wind_direction_deg"]
    assert direction is not None and blend.angular_diff(direction, 261.0) < 10.0
    # The old unit-vector mean pointed north (cross-shore here), which neither model said.
    assert blend.angular_diff(direction, 350.0) > 60.0
    flags = blend.compare_hour([gfs, ec], [])["flags"]
    assert "wind_direction_disagree" in flags and "wind_disagree" not in flags


def test_direction_flag_needs_both_winds_above_the_floor_and_the_angle() -> None:
    def flags(sa: float, da: float, sb: float, db: float) -> list[str]:
        a, b = _hour("gfs", "ncep_gfs", sa, da), _hour("ecmwf", "ecmwf_ifs", sb, db)
        return list(blend.compare_hour([a, b], [])["flags"])

    assert "wind_direction_disagree" not in flags(3.0, 54.0, 26.0, 277.0)  # one is calm
    assert "wind_disagree" in flags(3.0, 54.0, 26.0, 277.0)  # the speeds differ instead
    assert "wind_direction_disagree" not in flags(20.0, 200.0, 20.0, 255.0)  # 55 deg apart
    assert "wind_direction_disagree" in flags(20.0, 200.0, 20.0, 265.0)  # 65 deg apart
    floor = blend.WIND_DIRECTION_FLAG_MIN_KMH
    assert "wind_direction_disagree" in flags(floor, 0.0, floor, 180.0)
    assert "wind_direction_disagree" not in flags(floor - 0.1, 0.0, 30.0, 180.0)
    # Two copies of one family are cross-checks, never a disagreement of models.
    a, b = _hour("gfs", "ncep_gfs", 20.0, 0.0), _hour("gfs2", "ncep_gfs", 20.0, 180.0)
    assert "wind_direction_disagree" not in blend.compare_hour([a, b], [])["flags"]


def test_offshore_relation_follows_the_stronger_member_on_a_west_facing_beach() -> None:
    """On a west-facing beach (offshore wind from 90 deg) the blended wind's offshore
    component has the sign of the dominant member, never a cross-shore fiction."""
    offshore_from = 90.0
    gfs = _hour("gfs", "ncep_gfs", 13.0, 79.0)  # offshore
    ec = _hour("ecmwf", "ecmwf_ifs", 16.0, 261.0)  # onshore, stronger
    values, _, _, _ = blend.blend_hour([gfs, ec], blend.SkillTable())
    speed, direction = values["wind_speed_kmh"], values["wind_direction_deg"]
    assert speed is not None and direction is not None
    assert wind_components(speed, direction, offshore_from).v_offshore_kmh < -10.0
    assert wind_relation(speed, direction, offshore_from) == "onshore"
    assert wind_relation(13.0, 79.0, offshore_from) == "offshore"  # the GFS member alone


def test_gust_members_and_their_own_mean() -> None:
    gfs = _hour("gfs", "ncep_gfs", 10.5, 200.0, wind_gust_kmh=10.8)
    ec = _hour("ecmwf", "ecmwf_ifs", 18.5, 200.0)
    gfs.gust_kind = "gfs_instantaneous"
    values, weights, _, kind = blend.blend_hour([gfs, ec], blend.SkillTable())
    assert kind == "gfs_instantaneous" and values["wind_gust_kmh"] == pytest.approx(10.8)
    # The gust is below the blended mean (14.5) but not below its own member's mean (10.5).
    assert values["wind_speed_kmh"] == pytest.approx(14.5)
    codes, mean = blend.gust_members([gfs, ec], weights, kind)
    assert codes == ["gfs"] and mean == pytest.approx(10.5)
    # Every wind member has a gust of the same kind: nothing extra to record.
    ec.values["wind_gust_kmh"] = 25.0
    ec.gust_kind = "gfs_instantaneous"
    assert blend.gust_members([gfs, ec], weights, kind) == ([], None)
    assert blend.gust_members([gfs, ec], weights, None) == ([], None)
