"""Per-source staleness (queries.stale_after_hours / is_stale), on in-memory runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings, get_settings
from app.models import ForecastRun, ForecastSource
from app.services.forecasts import queries
from app.services.forecasts.registry import ECMWF_IFS_CODE, GEFS_WAVE_CODE, NOAA_GFS_CODE

NOW = datetime(2026, 10, 11, 8, 30, tzinfo=UTC)


def run(code: str, age_hours: float, **details: object) -> ForecastRun:
    source = ForecastSource(code=code, name=code, provider="test")
    return ForecastRun(
        source=source,
        run_key=f"{code}:t",
        issued_at=NOW - timedelta(hours=age_hours),
        details=details,
    )


def member(code: str, age_hours: float) -> dict[str, object]:
    return {"code": code, "issued_at": (NOW - timedelta(hours=age_hours)).isoformat()}


def test_stale_after_hours_per_source_with_default_settings() -> None:
    s = Settings()
    assert queries.stale_after_hours(NOAA_GFS_CODE, s) == pytest.approx(16.0)  # 6 + 5 + 3 + 2
    assert queries.stale_after_hours(GEFS_WAVE_CODE, s) == pytest.approx(18.5)  # 6 + 7.5 + 3 + 2
    assert queries.stale_after_hours(ECMWF_IFS_CODE, s) == pytest.approx(25.0)  # 12 + 8 + 3 + 2
    for other in ("demo", "open_meteo_noaa", "windy_gfswave", "blend"):
        assert queries.stale_after_hours(other, s) == s.forecast_stale_hours == 18


def test_stale_after_follows_the_lag_and_refresh_settings() -> None:
    s = Settings(ecmwf_availability_lag_hours=9.0, forecast_refresh_minutes=60)
    assert queries.stale_after_hours(ECMWF_IFS_CODE, s) == pytest.approx(12 + 9 + 1 + 2)


def test_cycle_hours_match_the_ingesters() -> None:
    from app.services.forecasts import ecmwf, ensemble, noaa

    assert queries.SOURCE_CYCLE_HOURS[NOAA_GFS_CODE] == noaa.CYCLE_HOURS
    assert queries.SOURCE_CYCLE_HOURS[GEFS_WAVE_CODE] == ensemble.CYCLE_HOURS
    hours = sorted(ecmwf.CYCLE_HOURS)
    assert queries.SOURCE_CYCLE_HOURS[ECMWF_IFS_CODE] == 24 / len(hours) == hours[1] - hours[0]


def test_ecmwf_defect_reproduced_and_fixed() -> None:
    """With one FORECAST_STALE_HOURS (18 h) a 22 h old ECMWF run counted as stale, although
    its next 12-hourly cycle is not published until ~8 h after that cycle time."""
    old_rule = timedelta(hours=get_settings().forecast_stale_hours)
    ecmwf_22h = run(ECMWF_IFS_CODE, 22.0)
    assert NOW - ecmwf_22h.issued_at > old_rule  # the defect: stale under the old rule
    assert not queries.is_stale(ecmwf_22h, NOW)  # fixed
    assert queries.is_stale(run(ECMWF_IFS_CODE, 26.0), NOW)
    # NOAA GFS cycles every 6 h: 17 h means a cycle was missed
    assert queries.is_stale(run(NOAA_GFS_CODE, 17.0), NOW)
    assert not queries.is_stale(run(NOAA_GFS_CODE, 15.0), NOW)
    assert queries.is_stale(run("demo", 19.0), NOW) and not queries.is_stale(run("demo", 17.0), NOW)


def test_blend_is_stale_when_any_member_is_stale_by_its_own_rule() -> None:
    # The blend's issue time is the oldest member's (ECMWF 22 h): fresh, both members are.
    blend = run("blend", 22.0, members=[member(NOAA_GFS_CODE, 8.0), member(ECMWF_IFS_CODE, 22.0)])
    assert not queries.is_stale(blend, NOW)
    gfs_late = run(
        "blend", 22.0, members=[member(NOAA_GFS_CODE, 17.0), member(ECMWF_IFS_CODE, 22.0)]
    )
    assert queries.is_stale(gfs_late, NOW)
    ecmwf_late = run(
        "blend", 26.0, members=[member(NOAA_GFS_CODE, 3.0), member(ECMWF_IFS_CODE, 26.0)]
    )
    assert queries.is_stale(ecmwf_late, NOW)
    # lenient factor (alerts): twice each member's limit
    assert not queries.is_stale(ecmwf_late, NOW, factor=2.0)


def test_blend_without_member_details_uses_the_most_lenient_member_rule() -> None:
    assert not queries.is_stale(run("blend", 24.0), NOW)
    assert queries.is_stale(run("blend", 26.0), NOW)
