"""Daily summaries count good surf in hours of forecast steps, not in steps (CT-9, CH-5).

Every point is a SYNTHETIC test value.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.schemas.spots import ForecastPoint
from app.services.spot_views import daily_summaries

GOOD, POOR = 60, 20  # Good starts at 57 (scoring.LABEL_THRESHOLDS)


def point(t: datetime, score: int, *, daylight: bool = True) -> ForecastPoint:
    return ForecastPoint(
        valid_time=t,
        is_daylight=daylight,
        sig_wave_height_m=1.5,
        primary_swell_height_m=1.4,
        primary_swell_period_s=13.0,
        primary_swell_direction_deg=220.0,
        secondary_swell_height_m=None,
        secondary_swell_period_s=None,
        secondary_swell_direction_deg=None,
        wind_wave_height_m=None,
        wind_wave_period_s=None,
        wind_speed_kmh=10.0,
        wind_direction_deg=45.0,
        wind_gust_kmh=None,
        pressure_msl_hpa=None,
        sea_level_m=None,
        wave_power_kw_m=None,
        breaking_height_min_ft=4.0,
        breaking_height_max_ft=5.0,
        score=score,
        label="good" if score >= 57 else "poor",
        confidence=60,
        confidence_label="moderate",
        wind_relation="offshore",
        explanation="synthetic",
        components={},
    )


def series(step_h: int, scores: list[int], start: datetime) -> list[ForecastPoint]:
    return [point(start + timedelta(hours=i * step_h), s) for i, s in enumerate(scores)]


DAY = datetime(2026, 10, 12, 6, tzinfo=UTC)


def test_three_hourly_steps_count_three_hours_each() -> None:
    pts = series(3, [POOR, GOOD, GOOD, POOR, POOR], DAY)  # 06, 09, 12, 15, 18 UTC
    (day,) = daily_summaries(pts, "UTC")
    assert day.date == date(2026, 10, 12)
    assert day.good_hours == 6.0


def test_hourly_steps_count_one_hour_each() -> None:
    pts = series(1, [POOR, GOOD, GOOD, POOR, POOR], DAY)
    (day,) = daily_summaries(pts, "UTC")
    assert day.good_hours == 2.0


def test_mixed_spacing_uses_each_steps_own_duration() -> None:
    # 3-hourly to 12:00, then 6-hourly (as ECMWF beyond 144 h): the 12:00 step lasts 6 h.
    times = [DAY, DAY + timedelta(hours=3), DAY + timedelta(hours=6), DAY + timedelta(hours=12)]
    pts = [point(t, s) for t, s in zip(times, [POOR, GOOD, GOOD, POOR], strict=True)]
    (day,) = daily_summaries(pts, "UTC")
    assert day.good_hours == 9.0


def test_night_steps_and_poor_steps_do_not_count() -> None:
    pts = series(3, [GOOD, GOOD, POOR], DAY)
    pts[0] = point(pts[0].valid_time, GOOD, daylight=False)
    (day,) = daily_summaries(pts, "UTC")
    assert day.good_hours == 3.0


@pytest.mark.parametrize("tz", ["Africa/Johannesburg", "Pacific/Honolulu", "Asia/Kathmandu"])
def test_good_hours_never_exceed_the_local_day(tz: str) -> None:
    pts = series(3, [GOOD] * 16, datetime(2026, 10, 11, 0, tzinfo=UTC))
    for day in daily_summaries(pts, tz):
        assert 0.0 <= day.good_hours <= 24.0
