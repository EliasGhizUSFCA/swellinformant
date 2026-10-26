"""One wind-vector conversion for every provider (app.services.geo.wind_from_uv).

Convention: u points east, v north, and (u, v) is where the air goes, so a wind of speed V
FROM bearing theta has u = -V sin(theta), v = -V cos(theta). A calm wind has no direction.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.services.forecasts import demo as demo_module
from app.services.forecasts.base import SpotPoint
from app.services.forecasts.demo import DemoSeriesGenerator
from app.services.forecasts.ecmwf import EcmwfIfsProvider, _RunState
from app.services.forecasts.grib import PointSampler
from app.services.forecasts.noaa import GUST, PRMSL, U10, V10, _atmos_values
from app.services.forecasts.windy import WindyProvider
from app.services.geo import angular_diff, wind_from_uv
from app.services.surf_quality.models import SpotParams

PARAMS = SpotParams(
    slug="jeffreys-bay",
    latitude=-34.03,
    longitude=24.93,
    swell_window_min=180,
    swell_window_max=250,
    optimal_swell_direction_min=200,
    optimal_swell_direction_max=230,
    offshore_wind_direction=290,
    min_swell_period_s=11,
    ideal_swell_period_s=15,
    wave_height_min_ft=4,
    wave_height_max_ft=12,
)
SPOT = SpotPoint(1, "jeffreys-bay", -34.03, 24.93, -34.15, 25.05, PARAMS)
CYCLE = datetime(2026, 10, 14, 0, tzinfo=UTC)


@pytest.mark.parametrize("speed", [0.4, 7.5, 31.0])
def test_72_directions_round_trip_through_u_and_v(speed: float) -> None:
    for k in range(72):
        theta = 5.0 * k
        u = -speed * math.sin(math.radians(theta))
        v = -speed * math.cos(math.radians(theta))
        got_speed, got_dir = wind_from_uv(u, v)
        assert got_dir is not None and 0.0 <= got_dir < 360.0
        assert abs(got_speed - speed) < 1e-9
        assert angular_diff(got_dir, theta) < 1e-9, (theta, got_dir)


def test_named_cases() -> None:
    assert wind_from_uv(0.0, -5.0)[1] == pytest.approx(0.0)  # air moving south: northerly
    assert wind_from_uv(-5.0, 0.0)[1] == pytest.approx(90.0)  # moving west: easterly
    assert wind_from_uv(5.0, 0.0)[1] == pytest.approx(270.0)  # moving east: westerly
    speed, direction = wind_from_uv(3.0, 4.0)  # moving towards the NNE-NE: from the SSW-SW
    assert speed == pytest.approx(5.0) and direction == pytest.approx(216.87, abs=0.01)


def test_calm_has_no_direction() -> None:
    assert wind_from_uv(0.0, 0.0) == (0.0, None)
    assert wind_from_uv(-0.0, 0.0) == (0.0, None)


def test_calm_gives_no_direction_in_every_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # NOAA GFS direct: 10 m u/v of a GRIB step.
    noaa = _atmos_values({U10: 0.0, V10: 0.0, GUST: 0.0, PRMSL: 101300.0})
    assert noaa["wind_speed_kmh"] == 0.0 and noaa["wind_direction_deg"] is None

    # ECMWF open data: one record of a step.
    state = _RunState(sampler=PointSampler())
    record = EcmwfIfsProvider._record(
        CYCLE, 6, {"swh": 1.2}, {"10u": 0.0, "10v": 0.0, "msl": 101300.0}, None, SPOT, state
    )
    assert record.values["wind_speed_kmh"] == 0.0
    assert record.values["wind_direction_deg"] is None

    # Windy point forecast: u/v components in the JSON response.
    ts = [int(CYCLE.timestamp()) * 1000]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "gfsWave":
            return httpx.Response(200, json={"ts": ts, "units": {}, "waves_height-surface": [1.0]})
        return httpx.Response(
            200,
            json={"ts": ts, "units": {}, "wind_u-surface": [0.0], "wind_v-surface": [0.0]},
        )

    windy = WindyProvider(
        Settings(forecast_days=3, windy_api_key="k"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
    )
    rows = windy.fetch([SPOT], windy.current_run(CYCLE + timedelta(hours=6))).records[1]
    assert rows[0].values["wind_speed_kmh"] == 0.0
    assert rows[0].values["wind_direction_deg"] is None

    # Demo provider: its synthetic wind goes through the same conversion.
    monkeypatch.setattr(demo_module, "wind_from_uv", lambda u, v: (0.0, None))
    demo = DemoSeriesGenerator(PARAMS, "demo:test", natural_swells=False).generate(CYCLE, 3)
    assert demo and all(r.values["wind_direction_deg"] is None for r in demo)
    assert all(r.values["wind_wave_direction_deg"] is None for r in demo)
