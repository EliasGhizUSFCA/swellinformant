"""Open-Meteo GFS-family adapter (forecasts.open_meteo) against recorded responses.

``tests/fixtures/open_meteo`` holds real, unmodified Open-Meteo responses captured on
2026-10-14 (README there): a multi-model marine response for the forecast points of
Pipeline, Jeffreys Bay and Thurso East, a weather response for three breaks and the
models' ``meta.json``. The mock transport serves one model per request from them, the way
the adapter asks; variables a model does not have come back null. Where a test needs
something the captures do not contain (240 hours, a run change, an HTTP error), the
response is SYNTHETIC and built in the test.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import redis

from app.core.config import Settings
from app.core.redis import get_redis
from app.services.forecasts.base import SpotPoint
from app.services.forecasts.open_meteo import (
    ATMOS,
    ModelMeta,
    OpenMeteoProvider,
    is_native_step,
    native_step_hours,
)
from app.services.forecasts.quota import call_weight
from app.services.forecasts.registry import SOURCE_CODES, build_provider
from app.services.http import QuotaExhaustedError, RunNotServedError, TransientProviderError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "open_meteo"
MARINE: list[dict[str, Any]] = json.loads((FIXTURES / "marine.json").read_text())
WEATHER: list[dict[str, Any]] = json.loads((FIXTURES / "weather.json").read_text())
NO_DATA = json.loads((FIXTURES / "marine_400_no_data.json").read_text())


def _meta_lines() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in (FIXTURES / "meta.txt").read_text().splitlines():
        head, sep, rest = line.partition(": ")
        if not sep or not rest.endswith("HTTP=200"):
            continue
        out[head.split()[1]] = json.loads(rest.removesuffix("HTTP=200").strip())
    return out


META = _meta_lines()
INIT = datetime(2026, 10, 13, 18, tzinfo=UTC)  # last_run_initialisation_time in meta.txt
NOW = datetime(2026, 10, 14, 1, 35, tzinfo=UTC)  # just after the captures

# Real seed coordinates (data/surf_spots/spots.json): break, then offshore forecast point.
PIPELINE = SpotPoint(1, "pipeline", 21.6655, -158.053, 21.8, -158.18)
JBAY = SpotPoint(2, "jeffreys-bay", -34.032, 24.932, -34.2, 25.0)
THURSO = SpotPoint(3, "thurso-east", 58.6022, -3.5098, 58.77, -3.69)
CLOUDBREAK = SpotPoint(4, "cloudbreak", -17.88, 177.186, -17.98, 177.08)


@pytest.fixture(autouse=True)
def clean_redis() -> Iterator[None]:
    """Budget counters and cached bodies must not leak between tests (Redis optional)."""
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {"forecast_days": 3, "forecast_batch_size": 50}
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type,call-arg]


def _nearest(locations: list[dict[str, Any]], lat: float) -> dict[str, Any]:
    return min(locations, key=lambda loc: abs(float(loc["latitude"]) - lat))


def single_model(loc: dict[str, Any], suffix: str, variables: list[str]) -> dict[str, Any]:
    """One model's slice of a multi-model capture, named as a one-model response is."""
    hourly: dict[str, Any] = {"time": loc["hourly"]["time"]}
    for v in variables:
        hourly[v] = loc["hourly"].get(f"{v}_{suffix}")
    keep = ("latitude", "longitude", "elevation", "utc_offset_seconds", "timezone")
    return {**{k: loc[k] for k in keep if k in loc}, "hourly": hourly}


class Recorder:
    """MockTransport handler serving the captures; records every request."""

    def __init__(
        self,
        meta: dict[str, dict[str, Any]] | None = None,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self.meta = meta if meta is not None else META
        self.override = override
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.override is not None and (resp := self.override(request)) is not None:
            return resp
        path = request.url.path
        if path.endswith("/static/meta.json"):
            domain = path.split("/")[2]
            body = self.meta.get(domain)
            return httpx.Response(200, json=body) if body else httpx.Response(404)
        q = parse_qs(urlparse(str(request.url)).query)
        model = q["models"][0]
        lats = [float(x) for x in q["latitude"][0].split(",")]
        variables = q["hourly"][0].split(",")
        if path == "/v1/marine":
            if model == "ncep_gfswave016" and any(not -15.0 <= lat <= 52.5 for lat in lats):
                return httpx.Response(400, json=NO_DATA)
            items = [single_model(_nearest(MARINE, lat), model, variables) for lat in lats]
        else:
            # The weather capture used gfs_seamless; at these points (Hawaii, South Africa,
            # California) it is GFS for the hours compared here, so it stands in for
            # gfs_global. A Thurso break request gets the nearest captured break (mock only).
            suffix = "gfs_seamless" if model.startswith("gfs") else model
            items = [single_model(_nearest(WEATHER, lat), suffix, variables) for lat in lats]
        return httpx.Response(200, json=items if len(items) > 1 else items[0])

    def data_calls(self) -> list[dict[str, list[str]]]:
        return [
            {"path": [c.url.path], **parse_qs(urlparse(str(c.url)).query)}
            for c in self.calls
            if not c.url.path.endswith("meta.json")
        ]


def provider(rec: Recorder, **kw: object) -> OpenMeteoProvider:
    return OpenMeteoProvider(
        settings(**kw),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: NOW,
    )


def fetch(rec: Recorder, spots: list[SpotPoint], **kw: object):  # type: ignore[no-untyped-def]
    p = provider(rec, **kw)
    return p, p.fetch(spots, p.current_run(NOW))


# --------------------------------------------------------------------------- registry
def test_registered_as_the_gfs_family() -> None:
    assert SOURCE_CODES["open_meteo"] == "open_meteo_noaa"
    p = build_provider("open_meteo", settings())
    info = p.source_info()
    assert (info.code, info.family, info.provider) == ("open_meteo_noaa", "ncep_gfs", "open_meteo")
    assert info.wave_model == "ncep_gfswave016,ncep_gfswave025"
    assert info.atmosphere_model == "gfs_global"
    assert "Open-Meteo.com" in info.attribution and "https://open-meteo.com/" in info.attribution


# --------------------------------------------------------------------------- routing
def test_wave_model_per_latitude_never_mixed_in_one_request() -> None:
    rec = Recorder()
    p, result = fetch(rec, [PIPELINE, CLOUDBREAK, THURSO])
    marine = [c for c in rec.data_calls() if c["path"] == ["/v1/marine"]]
    assert [(c["models"], c["latitude"]) for c in marine] == [
        (["ncep_gfswave016"], ["21.8000"]),
        (["ncep_gfswave025"], ["-17.9800,58.7700"]),
    ]
    # Sea cells for waves and for wind, in every request.
    assert all(c["cell_selection"] == ["sea"] for c in rec.data_calls())
    gfs = [c for c in rec.data_calls() if c["path"] == ["/v1/gfs"]]
    assert len(gfs) == 1 and gfs[0]["models"] == ["gfs_global"]
    assert gfs[0]["latitude"] == ["21.6655,-17.8800,58.6022"]  # wind at the breaks
    assert gfs[0]["wind_speed_unit"] == ["kmh"]
    assert set(result.records) == {1, 4, 3}
    assert result.records[1][0].extra["wave_model"] == "ncep_gfswave016"
    assert result.records[3][0].extra["wave_model"] == "ncep_gfswave025"


def test_domain_comes_from_the_model_metadata() -> None:
    gfs016 = ModelMeta.parse("ncep_gfswave016", META["ncep_gfswave016"])
    assert gfs016.bbox == (-15.0, -180.0, 52.5, 179.83334)
    assert gfs016.contains(21.8, -158.18) and gfs016.contains(-14.9, 179.95)
    assert not gfs016.contains(-17.98, 177.08) and not gfs016.contains(58.77, -3.69)
    no_bbox = ModelMeta.parse("ncep_gfswave016", {"last_run_initialisation_time": 1})
    assert no_bbox.contains(52.4, 0.0) and not no_bbox.contains(52.6, 0.0)


def test_http_400_no_data_falls_back_to_the_next_model_per_spot() -> None:
    def refuse_pipeline(request: httpx.Request) -> httpx.Response | None:
        q = parse_qs(urlparse(str(request.url)).query)
        if q.get("models") == ["ncep_gfswave016"] and "21.8000" in q["latitude"][0]:
            return httpx.Response(400, json=NO_DATA)
        return None

    nazare = SpotPoint(5, "nazare", 39.6065, -9.088, 39.65, -9.28)
    rec = Recorder(override=refuse_pipeline)
    _, result = fetch(rec, [PIPELINE, nazare])
    marine = [(c["models"][0], c["latitude"][0]) for c in rec.data_calls() if "models" in c]
    assert marine[:4] == [
        ("ncep_gfswave016", "21.8000,39.6500"),  # refused as a whole
        ("ncep_gfswave016", "21.8000"),  # refused alone
        ("ncep_gfswave016", "39.6500"),  # served
        ("ncep_gfswave025", "21.8000"),  # Pipeline from the global grid
    ]
    assert result.records[1][0].extra["wave_model"] == "ncep_gfswave025"
    assert result.records[5][0].extra["wave_model"] == "ncep_gfswave016"
    assert 1 not in result.errors and any("ncep_gfswave016 refused" in w for w in result.warnings)


# --------------------------------------------------------------------------- values
def test_values_cells_partitions_and_gusts_from_the_captures() -> None:
    _, result = fetch(Recorder(), [PIPELINE, JBAY, THURSO])
    assert not result.errors
    first = result.records[1][0]
    assert first.valid_time == datetime(2026, 10, 14, tzinfo=UTC)  # lead 6 h of the 18Z run
    v = first.values
    # ncep_gfswave016 at Pipeline's forecast point, as captured.
    assert v["sig_wave_height_m"] == 1.78 and v["wave_period_s"] == 13.9
    assert first.period_kind == "tp"  # GFS-Wave wave_period is PERPW, the peak period
    assert v["tertiary_swell_height_m"] is not None  # tertiary swell is read
    assert v["wind_speed_kmh"] == 22.3 and v["wind_direction_deg"] == 76.0
    assert v["sea_level_m"] is None  # never a tide (no sea-level request at all)
    assert first.extra["gust_kind"] == "gfs_instantaneous"
    assert first.extra["native_step_seconds"] == 3600
    assert first.extra["wave_cell"] == {"lat": 21.75, "lon": -158.25, "distance_km": 9.1}
    assert first.extra["wind_cell"]["lat"] == 21.6141  # type: ignore[index]
    assert first.extra["wind_cell"]["model"] == "gfs_global"  # type: ignore[index]
    assert "wave_cell" not in result.records[1][1].extra  # once per spot
    # 48 captured hours, all native hourly steps (leads 6..53 h).
    assert len(result.records[1]) == 48


def test_absent_partitions_are_none_never_zero() -> None:
    _, result = fetch(Recorder(), [THURSO, JBAY])
    thurso = result.records[3]
    no_secondary = [r for r in thurso if r.values["secondary_swell_height_m"] is None]
    assert len(no_secondary) == 27  # captured as 0 m / 0 s / 0 deg in 27 of 48 hours
    for r in no_secondary:
        assert r.values["secondary_swell_period_s"] is None
        assert r.values["secondary_swell_direction_deg"] is None
    assert sum(r.values["tertiary_swell_height_m"] is None for r in thurso) == 36
    jbay = result.records[2]
    assert sum(r.values["wind_wave_height_m"] is None for r in jbay) == 36
    for rows in result.records.values():
        for r in rows:
            for name, value in r.values.items():
                if name.endswith(("_height_m", "_period_s")) and value is not None:
                    assert value > 0, (name, r.valid_time)


def masked_cells(masked: set[tuple[float, float]]) -> Callable[[httpx.Request], httpx.Response]:
    """Marine responses whose cells in ``masked`` (and Pipeline's own cell) have no value in
    any hour, the way Open-Meteo answers for a cell the wave model masks (SYNTHETIC: the
    captured series relabelled with the requested coordinates)."""

    def respond(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/v1/marine":
            return None
        q = parse_qs(urlparse(str(request.url)).query)
        variables = q["hourly"][0].split(",")
        coords = zip(
            [float(x) for x in q["latitude"][0].split(",")],
            [float(x) for x in q["longitude"][0].split(",")],
            strict=True,
        )
        items = []
        for lat, lon in coords:
            item = single_model(_nearest(MARINE, lat), q["models"][0], variables)
            if (lat, lon) != (PIPELINE.forecast_latitude, PIPELINE.forecast_longitude):
                item = {**item, "latitude": lat, "longitude": lon}
            if (lat, lon) in masked or lat == PIPELINE.forecast_latitude:
                times = item["hourly"]["time"]
                item = {
                    **item,
                    "hourly": {"time": times} | dict.fromkeys(variables, [None] * len(times)),
                }
            items.append(item)
        return httpx.Response(200, json=items if len(items) > 1 else items[0])

    return respond


def test_a_masked_wave_cell_is_replaced_by_the_nearest_neighbouring_cell_with_data() -> None:
    # Pipeline's cell (21.75, -158.25) has no data; so have the two cells east of it.
    rec = Recorder(override=masked_cells({(21.75, -158.0833), (21.9167, -158.0833)}))
    _, result = fetch(rec, [PIPELINE])
    marine = [(c["models"][0], c["latitude"][0]) for c in rec.data_calls() if "models" in c]
    assert marine[0] == ("ncep_gfswave016", "21.8000")
    assert marine[1][0] == "ncep_gfswave016" and len(marine[1][1].split(",")) == 8
    assert 1 not in result.errors
    first = result.records[1][0]
    assert first.values["sig_wave_height_m"] is not None
    # Nearest cell with data to the forecast point (21.8, -158.18): one step north.
    cell = first.extra["wave_cell"]
    assert (cell["lat"], cell["lon"]) == (21.9167, -158.25)  # type: ignore[index]
    assert cell["distance_km"] == pytest.approx(14.8, abs=0.2)  # type: ignore[index]
    assert any(
        "pipeline: ncep_gfswave016 cell (21.75, -158.25) has no wave data; the neighbouring "
        "cell (21.9167, -158.25)" in w
        for w in result.warnings
    )
    # The neighbour request is charged too: (1 + 8) locations x 15 variables, + the wind.
    assert result.stats is not None and result.stats.weighted_calls == pytest.approx(14.5)


def test_without_a_neighbouring_cell_with_data_the_spot_has_no_wave_data() -> None:
    step = 1 / 6
    masked = {
        (round(21.75 + j * step, 4), round(-158.25 + i * step, 4))
        for j in (-1, 0, 1)
        for i in (-1, 0, 1)
    }
    _, result = fetch(Recorder(override=masked_cells(masked)), [PIPELINE])
    assert 1 not in result.records
    assert "no wave data at the forecast point" in result.errors[1]
    assert any("nor has any neighbouring cell within 60 km" in w for w in result.warnings)


def test_no_sea_level_request_is_made() -> None:
    rec = Recorder()
    fetch(rec, [PIPELINE])
    assert all("sea_level" not in c.get("hourly", [""])[0] for c in rec.data_calls())


# --------------------------------------------------------------------------- native steps
def test_native_step_tables() -> None:
    assert [h for h in range(0, 130) if is_native_step("ncep_gfswave016", h)] == [
        *range(0, 121),
        123,
        126,
        129,
    ]
    assert native_step_hours("gfs_global", 119) == 1 and native_step_hours("gfs_global", 120) == 3
    assert (
        native_step_hours("ecmwf_wam025", 144) == 6 and native_step_hours("ecmwf_wam025", 360) == 6
    )
    assert is_native_step("ecmwf_ifs025", 141) and not is_native_step("ecmwf_ifs025", 147)
    assert not is_native_step("gfs_global", -1) and not is_native_step("gfs_global", 1.5)
    # Unknown model: the metadata's temporal resolution is the native step.
    assert is_native_step("some_model", 6, 10800) and not is_native_step("some_model", 7, 10800)


def synthetic_hourly(init: datetime, hours: int, start: int = 0) -> Callable[..., Any]:
    """SYNTHETIC hourly series from ``init + start`` h: invented, smooth values."""

    def override(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("meta.json"):
            return None
        q = parse_qs(urlparse(str(request.url)).query)
        variables = q["hourly"][0].split(",")
        n = len(q["latitude"][0].split(","))
        t0 = int((init + timedelta(hours=start)).timestamp())
        times = [t0 + 3600 * i for i in range(hours)]
        loc = {
            "latitude": 21.75,
            "longitude": -158.25,
            "hourly": {
                "time": times,
                **{v: [round(1.0 + 0.01 * i, 2) for i in range(hours)] for v in variables},
            },
        }
        return httpx.Response(200, json=[loc] * n if n > 1 else loc)

    return override


def meta_at(init: datetime, available: datetime, end_hours: int = 385) -> dict[str, Any]:
    out = {}
    for domain, body in META.items():
        out[domain] = {
            **body,
            "last_run_initialisation_time": int(init.timestamp()),
            "last_run_availability_time": int(available.timestamp()),
            "data_end_time": int((init + timedelta(hours=end_hours)).timestamp()),
        }
    return out


def test_hours_beyond_f120_are_thinned_to_the_native_3_hourly_steps() -> None:
    init = datetime(2026, 10, 14, tzinfo=UTC)
    meta = meta_at(init, init + timedelta(hours=5))
    rec = Recorder(meta=meta, override=synthetic_hourly(init, 300, start=-6))
    p = OpenMeteoProvider(
        settings(forecast_days=10),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: init + timedelta(hours=7),
    )
    result = p.fetch([PIPELINE], p.current_run(init + timedelta(hours=7)))
    rows = result.records[1]
    leads = [(r.valid_time - init).total_seconds() / 3600 for r in rows]
    assert len(rows) == 121 + 40  # hourly to f120, then 3-hourly to f240
    assert leads[0] == 0 and leads[120] == 120 and leads[121] == 123 and leads[-1] == 240
    assert all(EXTRA not in r.extra for r in rows for EXTRA in ("interpolated",))
    assert {r.extra["native_step_seconds"] for r in rows[121:]} == {10800}


def test_interpolated_hours_are_flagged_when_kept() -> None:
    init = datetime(2026, 10, 14, tzinfo=UTC)
    meta = meta_at(init, init + timedelta(hours=5))
    rec = Recorder(meta=meta, override=synthetic_hourly(init, 300))
    p = OpenMeteoProvider(
        settings(forecast_days=10, open_meteo_native_steps_only=False),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: init + timedelta(hours=7),
    )
    rows = p.fetch([PIPELINE], p.current_run(init + timedelta(hours=7))).records[1]
    flagged = [r for r in rows if r.extra.get("interpolated")]
    assert len(rows) == 241 and len(flagged) == 80
    lead = (flagged[0].valid_time - init).total_seconds() / 3600
    assert lead == 121 and "sig_wave_height_m" in flagged[0].extra["interpolated_fields"]  # type: ignore[operator]


def test_hours_past_the_runs_data_end_are_dropped() -> None:
    """Open-Meteo fills hours after a run's horizon from an older run: never kept."""
    init = datetime(2026, 10, 14, tzinfo=UTC)
    meta = meta_at(init, init + timedelta(hours=5), end_hours=27)  # SYNTHETIC short run
    rec = Recorder(meta=meta, override=synthetic_hourly(init, 48))
    p = OpenMeteoProvider(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: init + timedelta(hours=7),
    )
    rows = p.fetch([PIPELINE], p.current_run(init + timedelta(hours=7))).records[1]
    assert rows[-1].valid_time == init + timedelta(hours=26)


# --------------------------------------------------------------------------- run identity
def test_run_key_and_model_runs_from_the_metadata() -> None:
    p = provider(Recorder())
    run = p.current_run(NOW)
    assert run.run_key == (
        "gfswave016:2026101318|gfswave025:2026101318|gfs013:2026101318|gfs025:2026101318"
    )
    assert run.issued_at == run.wave_model_run_at == run.atmosphere_model_run_at == INIT
    assert run.metadata_source == "open_meteo_meta"
    refs = {r.model: r for r in run.model_runs}
    assert set(refs) == {"ncep_gfswave016", "ncep_gfswave025", "ncep_gfs013", "ncep_gfs025"}
    assert refs["ncep_gfs025"].available_at == datetime.fromtimestamp(1791936436, UTC)


def test_availability_wait_keeps_the_previous_run_until_ten_minutes_pass() -> None:
    init = datetime(2026, 10, 14, 6, tzinfo=UTC)
    available = datetime(2026, 10, 14, 11, 16, tzinfo=UTC)
    rec = Recorder(meta=meta_at(init, available))
    p = provider(rec)
    early = p.current_run(available + timedelta(minutes=5))
    assert early.wave_model_run_at == init - timedelta(hours=6)  # previous run's key
    assert "2026101400" in early.run_key and "2026101406" not in early.run_key
    assert all(r.available_at is None for r in early.model_runs)
    late = p.current_run(available + timedelta(minutes=11))
    assert late.wave_model_run_at == init and "gfswave016:2026101406" in late.run_key
    # Fetching the previous run while the new one is being published is refused, with the
    # time to retry; nothing is requested.
    p.clock = lambda: available + timedelta(minutes=5)
    with pytest.raises(RunNotServedError) as waiting:
        p.fetch([PIPELINE], early)
    assert waiting.value.retry_after_s == pytest.approx(5 * 60 + 30)
    assert rec.data_calls() == []


def test_a_newer_readable_run_ready_before_the_fetch_is_read_on_a_quick_retry() -> None:
    """The run was identified from the 00Z metadata; by the fetch the 06Z run is ready:
    nothing is requested, and the task retries within a minute to read the newer run."""
    old_init = datetime(2026, 10, 14, 0, tzinfo=UTC)
    new_init = old_init + timedelta(hours=6)
    available = datetime(2026, 10, 14, 11, 16, tzinfo=UTC)
    rec = Recorder(meta=meta_at(old_init, available - timedelta(hours=6)))
    p = provider(rec)
    run = p.current_run(available + timedelta(minutes=30))
    assert run.wave_model_run_at == old_init
    rec.meta = meta_at(new_init, available)
    p.clock = lambda: available + timedelta(minutes=30)
    with pytest.raises(RunNotServedError, match="read on the retry") as newer:
        p.fetch([PIPELINE], run)
    assert newer.value.retry_after_s == 60.0
    assert rec.data_calls() == []


def test_a_run_published_during_the_fetch_is_never_stored_under_the_old_key() -> None:
    state = {"meta_reads": 0}
    new = meta_at(INIT + timedelta(hours=6), INIT + timedelta(hours=11))

    def switch(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("meta.json"):
            state["meta_reads"] += 1
            if state["meta_reads"] > 8:  # current_run (4) + pre-check (4), then the new run
                domain = request.url.path.split("/")[2]
                return httpx.Response(200, json=new[domain])
        return None

    rec = Recorder(override=switch)
    p = provider(rec)
    run = p.current_run(NOW)
    with pytest.raises(TransientProviderError, match="published a new run during the fetch"):
        p.fetch([PIPELINE], run)
    assert len(rec.data_calls()) == 2  # the data was read, then refused


def test_metadata_unavailable_means_no_fetch() -> None:
    rec = Recorder(meta={})
    with pytest.raises(TransientProviderError, match="cannot be identified"):
        provider(rec).current_run(NOW)
    assert rec.data_calls() == []


def test_gfs_seamless_records_hrrr_and_gfs_init_times() -> None:
    p = provider(Recorder(), open_meteo_gfs_atmos_model="gfs_seamless")
    run = p.current_run(NOW)
    refs = {r.model: r.init_time for r in run.model_runs}
    assert refs["ncep_hrrr_conus"] == datetime(2026, 10, 13, 23, tzinfo=UTC)
    assert refs["ncep_gfs013"] == refs["ncep_gfs025"] == INIT
    assert "hrrr_conus:2026101323" in run.run_key and len(run.run_key) <= 120
    assert run.atmosphere_model_run_at == INIT  # the older of the two models
    result = p.fetch([PIPELINE], run)
    assert result.records[1][0].extra["gust_kind"] == "unknown"  # HRRR gusts: not GFS's


def test_deprecated_single_model_alias_is_honoured() -> None:
    rec = Recorder()
    fetch(rec, [PIPELINE, THURSO], open_meteo_wave_model="ncep_gfswave025")
    marine = [c for c in rec.data_calls() if c["path"] == ["/v1/marine"]]
    assert [c["models"] for c in marine] == [["ncep_gfswave025"]]


# --------------------------------------------------------------------------- errors
def test_daily_limit_429_is_quota_exhaustion() -> None:
    def limited(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("meta.json"):
            return None
        reason = "Daily API request limit exceeded. Please try again tomorrow."
        return httpx.Response(429, json={"error": True, "reason": reason})

    rec = Recorder(override=limited)
    with pytest.raises(QuotaExhaustedError, match="Daily API request limit"):
        fetch(rec, [PIPELINE])
    assert len(rec.data_calls()) == 1  # not retried


def test_minutely_limit_429_is_transient_with_a_retry_time() -> None:
    def limited(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("meta.json"):
            return None
        reason = "Minutely API request limit exceeded. Please try again in one minute."
        return httpx.Response(429, json={"error": True, "reason": reason})

    with pytest.raises(TransientProviderError) as limited_error:
        fetch(Recorder(override=limited), [PIPELINE])
    assert limited_error.value.retry_after_s == 60.0


def test_server_errors_are_retried_then_transient() -> None:
    state = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("meta.json") or state["n"] >= 2:
            return None
        state["n"] += 1
        return httpx.Response(503)

    _, result = fetch(Recorder(override=flaky), [PIPELINE])
    assert set(result.records) == {1}  # two 503s, then served

    def down(request: httpx.Request) -> httpx.Response | None:
        return None if request.url.path.endswith("meta.json") else httpx.Response(503)

    with pytest.raises(TransientProviderError):
        fetch(Recorder(override=down), [PIPELINE], provider_response_cache_enabled=False)


def test_wind_failure_for_a_spot_makes_it_incomplete() -> None:
    def no_wind(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/v1/gfs":
            return httpx.Response(400, json={"error": True, "reason": "Invalid coordinates"})
        return None

    _, result = fetch(Recorder(override=no_wind), [PIPELINE])
    assert result.records[1][0].values["wind_speed_kmh"] is None
    assert "incomplete: wind and pressure unavailable" in result.errors[1]


def test_api_key_uses_the_customer_hosts() -> None:
    rec = Recorder()
    fetch(rec, [PIPELINE], open_meteo_api_key="secret-key")
    assert rec.calls and all(c.url.host.startswith("customer-") for c in rec.calls)
    assert all(
        parse_qs(urlparse(str(c.url)).query).get("apikey") == ["secret-key"] for c in rec.calls
    )


# --------------------------------------------------------------------------- cost
def test_stats_record_bytes_requests_and_weighted_calls() -> None:
    rec = Recorder()
    _, result = fetch(rec, [PIPELINE, CLOUDBREAK, THURSO], forecast_days=16)
    stats = result.stats
    assert stats is not None
    assert stats.bytes_downloaded > 0
    # The fetch's own responses: metadata before and after (4 + 4) and 3 data requests;
    # current_run's 4 metadata reads came before it.
    assert stats.requests == result.request_count == len(rec.calls) - 4 == 11
    assert stats.http_status_counts == {"200": 11}
    # marine 016 (1 location) + marine 025 (2) with 15 variables, gfs (3) with 4; 16 days.
    expected = (1 + 2) * 1.5 * 16 / 14 + 3 * 1.0 * 16 / 14
    assert stats.weighted_calls == pytest.approx(expected, abs=1e-3)
    assert result.warnings[0].startswith("Open-Meteo: 3 data requests")


def test_a_retried_fetch_of_the_same_run_reads_the_cache() -> None:
    try:
        get_redis().ping()
    except redis.RedisError:
        pytest.skip("Redis not available")
    rec = Recorder()
    p, first = fetch(rec, [PIPELINE, THURSO])
    n = len(rec.data_calls())
    second = p.fetch([PIPELINE, THURSO], p.current_run(NOW))
    assert len(rec.data_calls()) == n  # no data request the second time
    assert second.stats is not None and second.stats.weighted_calls == 0
    assert [r.values for r in second.records[1]] == [r.values for r in first.records[1]]


def test_a_retry_after_a_failed_wind_request_reads_the_waves_from_the_cache() -> None:
    """The waves arrived, then the forecast API refused (a transient minute limit): the
    task's retry reads the waves from the cache and pays only for the wind request."""
    try:
        get_redis().ping()
    except redis.RedisError:
        pytest.skip("Redis not available")
    refuse = {"on": True}

    def minute_limit(request: httpx.Request) -> httpx.Response | None:
        if refuse["on"] and request.url.path == "/v1/gfs":
            reason = "Minutely API request limit exceeded. Please try again in one minute."
            return httpx.Response(429, json={"error": True, "reason": reason})
        return None

    rec = Recorder(override=minute_limit)
    p = provider(rec)
    with pytest.raises(TransientProviderError):
        p.fetch([PIPELINE, THURSO], p.current_run(NOW))
    marine = [c for c in rec.data_calls() if c["path"] == ["/v1/marine"]]
    assert len(marine) == 2  # one per wave model
    refuse["on"] = False
    result = p.fetch([PIPELINE, THURSO], p.current_run(NOW))
    assert [c for c in rec.data_calls() if c["path"] == ["/v1/marine"]] == marine
    assert result.records[PIPELINE.spot_id] and result.errors == {}
    wind_only = call_weight(2, len(ATMOS), 3)
    assert result.stats is not None and result.stats.weighted_calls == pytest.approx(wind_only)
