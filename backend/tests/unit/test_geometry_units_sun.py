from datetime import UTC, date, datetime

import pytest

from app.services.geo import (
    angular_diff,
    compass_label,
    haversine_km,
    in_window,
    wind_from_uv,
    window_center,
    window_contains_window,
    window_distance,
)
from app.services.surf_quality.sun import is_daylight, sun_times
from app.services.units import ft_to_m, height_to_ft, m_to_ft, round_half


class TestAngles:
    def test_angular_diff_wraps(self) -> None:
        assert angular_diff(350, 10) == 20
        assert angular_diff(10, 350) == 20
        assert angular_diff(0, 180) == 180

    def test_window_without_wrap(self) -> None:
        assert in_window(215, 200, 230)
        assert not in_window(240, 200, 230)
        assert window_distance(240, 200, 230) == 10

    def test_window_wrapping_through_north(self) -> None:
        assert in_window(355, 300, 20)
        assert in_window(10, 300, 20)
        assert not in_window(100, 300, 20)
        assert window_center(300, 20) == 340

    def test_window_contains_window(self) -> None:
        assert window_contains_window(280, 20, 295, 320)
        assert window_contains_window(300, 40, 350, 10)
        assert not window_contains_window(200, 250, 240, 260)

    def test_compass_and_wind_components(self) -> None:
        assert compass_label(0) == "N"
        assert compass_label(225) == "SW"
        speed, direction = wind_from_uv(0.0, -5.0)  # air moving south = wind FROM north
        assert speed == pytest.approx(5.0)
        assert direction == pytest.approx(0.0, abs=1e-6)
        _, from_west = wind_from_uv(5.0, 0.0)
        assert from_west == pytest.approx(270.0)

    def test_haversine(self) -> None:
        # SFO → HNL is ~3,860 km
        assert haversine_km(37.6213, -122.379, 21.3187, -157.9225) == pytest.approx(3860, rel=0.01)


class TestUnits:
    def test_wave_height_conversions(self) -> None:
        assert m_to_ft(1.0) == pytest.approx(3.28084, rel=1e-5)
        assert ft_to_m(10) == pytest.approx(3.048)
        assert height_to_ft(2.0, "m") == pytest.approx(6.5617, rel=1e-4)
        assert height_to_ft(6.0, "ft") == 6.0
        with pytest.raises(ValueError):
            height_to_ft(1.0, "yards")

    def test_round_half(self) -> None:
        assert round_half(6.2) == 6.0
        assert round_half(6.3) == 6.5
        assert round_half(6.8) == 7.0


class TestSun:
    def test_honolulu_sunrise_sunset(self) -> None:
        sunrise, sunset, state = sun_times(21.3, -157.86, date(2026, 10, 9))
        assert state == "normal"
        assert sunrise and sunset
        # ~06:25 and ~18:13 HST (UTC-10)
        assert sunrise.hour == 16 and 15 <= sunrise.minute <= 35
        assert sunset.hour == 4 and 3 <= sunset.minute <= 23

    def test_high_latitude_summer_and_winter(self) -> None:
        summer = sun_times(58.6, -3.5, date(2026, 6, 21))
        winter = sun_times(58.6, -3.5, date(2026, 12, 21))
        summer_len = (summer[1] - summer[0]).total_seconds() / 3600  # type: ignore[operator]
        winter_len = (winter[1] - winter[0]).total_seconds() / 3600  # type: ignore[operator]
        assert summer_len > 18 and winter_len < 7

    def test_polar_day_and_night(self) -> None:
        assert sun_times(78.0, 15.0, date(2026, 6, 21))[2] == "polar_day"
        assert sun_times(78.0, 15.0, date(2026, 12, 21))[2] == "polar_night"
        assert is_daylight(78.0, 15.0, datetime(2026, 6, 21, 0, tzinfo=UTC))
        assert not is_daylight(78.0, 15.0, datetime(2026, 12, 21, 12, tzinfo=UTC))

    def test_daylight_uses_local_solar_date(self) -> None:
        # 18:00 UTC is 08:00 in Hawaii (day), 08:00 UTC is 22:00 (night)
        assert is_daylight(21.66, -158.05, datetime(2026, 10, 9, 18, tzinfo=UTC))
        assert not is_daylight(21.66, -158.05, datetime(2026, 10, 9, 8, tzinfo=UTC))
