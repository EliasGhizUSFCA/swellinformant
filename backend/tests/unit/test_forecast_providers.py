"""Forecast provider adapters against recorded-shape API responses (no network)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.config import Settings
from app.services.forecasts.base import SpotPoint, nominal_gfs_cycle
from app.services.forecasts.demo import DemoForecastProvider, DemoSeriesGenerator, SwellOverride
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.windy import WindyProvider
from app.services.http import PermanentProviderError
from app.services.surf_quality.models import SpotParams

NOW = datetime(2026, 10, 9, 18, tzinfo=UTC)
T0 = int(datetime(2026, 10, 9, 0, tzinfo=UTC).timestamp())

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
SPOTS = [
    SpotPoint(1, "jeffreys-bay", -34.03, 24.93, -34.15, 25.05, PARAMS),
    SpotPoint(2, "pipeline", 21.66, -158.05, 21.8, -158.18, PARAMS),
]


def settings(**kw: object) -> Settings:
    return Settings(forecast_days=3, forecast_batch_size=10, **kw)  # type: ignore[arg-type]


def location(
    variables: list[str], hours: int = 3, null_vars: tuple[str, ...] = ()
) -> dict[str, object]:
    hourly: dict[str, object] = {"time": [T0 + 3600 * i for i in range(hours)]}
    for v in variables:
        hourly[v] = [None if v in null_vars else round(1.0 + i * 0.1, 2) for i in range(hours)]
    return {
        "latitude": -34.125,
        "longitude": 25.0,
        "generationtime_ms": 0.5,
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "timezone_abbreviation": "GMT",
        "hourly_units": {},
        "hourly": hourly,
    }


class Recorder:
    def __init__(self, handler):  # type: ignore[no-untyped-def]
        self.calls: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.handler(request)


def standard_handler(request: httpx.Request) -> httpx.Response:
    url = urlparse(str(request.url))
    q = parse_qs(url.query)
    if url.path.endswith("meta.json"):
        return httpx.Response(
            200,
            json={
                "last_run_initialisation_time": int(
                    datetime(2026, 10, 9, 6, tzinfo=UTC).timestamp()
                )
            },
        )
    variables = q["hourly"][0].split(",")
    n = len(q["latitude"][0].split(","))
    return httpx.Response(200, json=[location(variables) for _ in range(n)])


def provider(handler, **kw) -> tuple[OpenMeteoProvider, Recorder]:  # type: ignore[no-untyped-def]
    rec = Recorder(handler)
    p = OpenMeteoProvider(
        settings(**kw), client=httpx.Client(transport=httpx.MockTransport(rec)), backoff_seconds=0
    )
    return p, rec


class TestOpenMeteo:
    def test_run_key_from_model_metadata(self) -> None:
        p, _ = provider(standard_handler)
        run = p.current_run(NOW)
        assert run.run_key == "ncep_gfswave025:2026100906|gfs_seamless:2026100906"
        assert run.metadata_source == "open_meteo_meta"

    def test_metadata_unavailable_falls_back_to_nominal_cycle(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("meta.json"):
                return httpx.Response(404)
            return standard_handler(request)

        p, _ = provider(handler)
        run = p.current_run(NOW)
        assert run.metadata_source == "nominal_cycle"
        assert run.issued_at == nominal_gfs_cycle(NOW) == datetime(2026, 10, 9, 12, tzinfo=UTC)

    def test_batched_fetch_merges_wave_wind_and_sea_level(self) -> None:
        p, rec = provider(standard_handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert not result.errors
        assert set(result.records) == {1, 2}
        rec0 = result.records[1][0]
        assert rec0.valid_time == datetime.fromtimestamp(T0, UTC)
        for field in (
            "sig_wave_height_m",
            "primary_swell_period_s",
            "wind_speed_kmh",
            "sea_level_m",
            "secondary_swell_height_m",
        ):
            assert rec0.values[field] is not None
        data_calls = [c for c in rec.calls if not c.url.path.endswith("meta.json")]
        assert len(data_calls) == 3  # marine + gfs + sea level, both spots in ONE batch
        marine = parse_qs(urlparse(str(data_calls[0].url)).query)
        assert marine["models"] == ["ncep_gfswave025"]
        assert marine["latitude"] == ["-34.1500,21.8000"]  # offshore forecast points
        gfs = parse_qs(urlparse(str(data_calls[1].url)).query)
        assert gfs["latitude"] == ["-34.0300,21.6600"]  # wind at the break itself
        assert gfs["wind_speed_unit"] == ["kmh"]

    def test_rejected_optional_variables_are_dropped_and_retried(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlparse(str(request.url)).query)
            if "hourly" in q and "secondary_swell_wave_height" in q["hourly"][0]:
                return httpx.Response(
                    400,
                    json={
                        "error": True,
                        "reason": "Cannot initialize MarineVariable from invalid String value secondary_swell_wave_height",
                    },
                )
            return standard_handler(request)

        p, rec = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert set(result.records) == {1, 2}
        assert result.records[1][0].values.get("secondary_swell_height_m") is None
        assert result.records[1][0].values["primary_swell_height_m"] is not None

    def test_one_bad_location_does_not_fail_the_batch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlparse(str(request.url)).query)
            if q.get("models") == ["ncep_gfswave025"] and "21.8000" in q["latitude"][0]:
                return httpx.Response(
                    400, json={"error": True, "reason": "No data is available for this location"}
                )
            return standard_handler(request)

        p, _ = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert 1 in result.records
        assert "No data is available" in result.errors[2]

    def test_land_points_with_all_null_waves_are_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlparse(str(request.url)).query)
            if q.get("models") == ["ncep_gfswave025"]:
                variables = q["hourly"][0].split(",")
                n = len(q["latitude"][0].split(","))
                return httpx.Response(
                    200, json=[location(variables, null_vars=tuple(variables)) for _ in range(n)]
                )
            return standard_handler(request)

        p, _ = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert not result.records
        assert "no wave data" in result.errors[1]

    def test_transient_errors_are_retried(self) -> None:
        state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if not request.url.path.endswith("meta.json") and state["n"] < 2:
                state["n"] += 1
                return httpx.Response(503)
            return standard_handler(request)

        p, _ = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert set(result.records) == {1, 2}

    def test_persistent_outage_is_recorded_per_spot(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("meta.json"):
                return standard_handler(request)
            return httpx.Response(503)

        p, _ = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert not result.records and len(result.errors) == 2

    def test_missing_sea_level_is_tolerated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            q = parse_qs(urlparse(str(request.url)).query)
            if q.get("hourly") == ["sea_level_height_msl"]:
                return httpx.Response(400, json={"error": True, "reason": "unsupported"})
            return standard_handler(request)

        p, _ = provider(handler)
        result = p.fetch(SPOTS, p.current_run(NOW))
        assert set(result.records) == {1, 2}
        assert result.records[1][0].values.get("sea_level_m") is None

    def test_api_key_uses_customer_hosts(self) -> None:
        p, rec = provider(standard_handler, open_meteo_api_key="secret-key")
        p.fetch(SPOTS[:1], p.current_run(NOW))
        assert all(c.url.host.startswith("customer-") for c in rec.calls)
        assert all(
            parse_qs(urlparse(str(c.url)).query).get("apikey") == ["secret-key"] for c in rec.calls
        )


class TestWindy:
    def test_requires_key(self) -> None:
        with pytest.raises(PermanentProviderError):
            WindyProvider(settings())

    def test_parses_waves_and_converts_wind_components(self) -> None:
        ts = [T0 * 1000 + 3600_000 * i for i in range(2)]

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["key"] == "k"
            if body["model"] == "gfsWave":
                return httpx.Response(
                    200,
                    json={
                        "ts": ts,
                        "units": {},
                        "waves_height-surface": [2.1, 2.2],
                        "waves_period-surface": [14, 14],
                        "waves_direction-surface": [215, 216],
                        "swell1_height-surface": [1.9, 2.0],
                        "swell1_period-surface": [15, 15],
                        "swell1_direction-surface": [214, 214],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ts": ts,
                    "units": {},
                    "wind_u-surface": [3.0, 0.0],
                    "wind_v-surface": [0.0, -4.0],
                    "gust-surface": [5.0, 6.0],
                    "pressure-surface": [101300, 101200],
                },
            )

        p = WindyProvider(
            settings(windy_api_key="k"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
        )
        result = p.fetch(SPOTS[:1], p.current_run(NOW))
        r0, r1 = result.records[1]
        assert r0.values["primary_swell_height_m"] == 1.9
        assert r0.values["wind_direction_deg"] == pytest.approx(
            270.0
        )  # u>0: air moving east = from west
        assert r0.values["wind_speed_kmh"] == pytest.approx(10.8)
        assert r1.values["wind_direction_deg"] == pytest.approx(0.0)  # v<0: from north
        assert r0.values["pressure_msl_hpa"] == pytest.approx(1013.0)


class TestDemo:
    def test_is_labelled_demo(self) -> None:
        assert DemoForecastProvider().source_info().is_demo

    def test_deterministic_per_run(self) -> None:
        a = DemoSeriesGenerator(PARAMS, "demo:1").generate(NOW, 240)
        b = DemoSeriesGenerator(PARAMS, "demo:1").generate(NOW, 240)
        c = DemoSeriesGenerator(PARAMS, "demo:2").generate(NOW, 240)
        assert [r.values for r in a] == [r.values for r in b]
        assert [r.values for r in a] != [r.values for r in c]  # run-to-run perturbation of swells
        assert len(a) == 240

    def test_override_injects_a_strong_aligned_swell(self) -> None:
        start = NOW + timedelta(days=7)
        o = SwellOverride("jeffreys-bay", start, start + timedelta(hours=48))
        records = DemoSeriesGenerator(PARAMS, "demo:1").generate(NOW, 10 * 24, [o])
        inside = [r for r in records if start <= r.valid_time <= start + timedelta(hours=48)]
        assert all(200 <= r.values["primary_swell_direction_deg"] <= 230 for r in inside)  # type: ignore[operator]
        assert all(r.values["wind_speed_kmh"] <= 12 for r in inside)  # type: ignore[operator]
        assert max(r.values["primary_swell_height_m"] for r in inside) > 1.0  # type: ignore[type-var]
