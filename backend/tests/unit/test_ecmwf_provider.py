"""ECMWF IFS open-data provider against a fake S3 bucket (no network).

Every GRIB2 message here is SYNTHETIC: built with the ecCodes ``regular_ll_sfc_grib2``
sample on a coarse 2° global grid whose first longitude is -180, like ECMWF's 0.25° grids.
The ``.index`` files copy the live JSON-lines layout (``param``/``levtype``/``step`` plus
``_offset``/``_length``) and the message order observed in the bucket on 2026-10-10.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import numpy as np
import pytest

from app.core.config import Settings
from app.services.forecasts.base import FORECAST_FIELDS, RunInfo, SpotPoint
from app.services.forecasts.ecmwf import (
    LICENSE_URL,
    EcmwfIfsProvider,
    candidate_cycles,
    choose_cell,
    combined_band_height,
    cycle_steps,
    file_url,
    wind_from_uv,
)
from app.services.forecasts.grib import (
    PointSampler,
    decode_message,
    haversine_km,
    load_eccodes,
)
from app.services.forecasts.registry import ECMWF_IFS_CODE, build_provider
from app.services.http import PermanentProviderError, TransientProviderError

eccodes = load_eccodes()

BASE = "https://ecmwf.test"
CYCLE = datetime(2026, 10, 10, 0, tzinfo=UTC)
D = 2.0  # synthetic grid spacing (degrees)
NJ, NI = 91, 180  # 90N..90S, -180..178E

# Message order inside the live files (2026-10-10).
WAVE_ORDER: tuple[str, ...] = ("h1012", "h1214", "h1417", "h1721", "h2125", "h2530")
WAVE_ORDER += ("wmb", "mp2", "swh", "mwd", "pp1d", "mwp", "cdww")
# The live atmosphere files interleave many other fields; fillers keep ours non-adjacent.
OPER_ORDER = ("msl", "2t", "10u", "tcc", "10v", "skt", "lsm", "sp", "10fg")


def ij(lat: float, lon: float) -> tuple[int, int]:
    return round((90.0 - lat) / D), round(((lon + 180.0) % 360.0) / D) % NI


def grib(values: np.ndarray, step_range: str | None = None) -> bytes:
    """Synthetic GRIB2 message on the 2° grid (NaN = missing via bitmap)."""
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "Ni", NI)
        eccodes.codes_set(h, "Nj", NJ)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", 90.0)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", -180.0)
        eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", -90.0)
        eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", 178.0)
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", D)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", D)
        eccodes.codes_set(h, "jScansPositively", 0)
        if step_range is not None:  # statistically processed (max) field, PDT 4.8
            eccodes.codes_set(h, "productDefinitionTemplateNumber", 8)
            eccodes.codes_set(h, "typeOfStatisticalProcessing", 2)
            eccodes.codes_set(h, "stepRange", step_range)
        eccodes.codes_set(h, "bitmapPresent", 1)
        eccodes.codes_set(h, "missingValue", 9999)
        eccodes.codes_set_values(h, np.where(np.isnan(values), 9999, values).ravel())
        return bytes(eccodes.codes_get_message(h))
    finally:
        eccodes.codes_release(h)


def full(value: float) -> np.ndarray:
    return np.full((NJ, NI), value, dtype="float64")


# Synthetic sea state: 1.0 m Hs, Tp 14 s, from 300°, band heights below.
BANDS = {"h1012": 0.3, "h1214": 0.4, "h1417": 0.5, "h1721": 0.6, "h2125": 0.1, "h2530": 0.05}
WAVE_BASE = {
    **BANDS,
    "wmb": 999.0,
    "mp2": 7.5,
    "swh": 1.0,
    "mwd": 300.0,
    "pp1d": 14.0,
    "mwp": 9.5,
    "cdww": 0.001,
}
OPER_BASE = {"msl": 101325.0, "10u": 3.0, "10v": 4.0, "lsm": 0.0, "10fg": 10.0}
OPER_BASE.update({"2t": 288.0, "tcc": 0.5, "skt": 289.0, "sp": 101000.0})  # fillers


@dataclass
class Scenario:
    """Field overrides: ``paint[param][(j, i)] = value`` and NaN (land) wave cells."""

    wave_land: set[tuple[int, int]] = field(default_factory=set)
    paint: dict[str, dict[tuple[int, int], float]] = field(default_factory=dict)

    def wave_fields(self) -> dict[str, np.ndarray]:
        out = {}
        for name, base in WAVE_BASE.items():
            arr = full(base)
            for cell, value in self.paint.get(name, {}).items():
                arr[cell] = value
            for cell in self.wave_land:
                arr[cell] = np.nan
            out[name] = arr
        return out

    def oper_fields(self) -> dict[str, np.ndarray]:
        out = {}
        for name, base in OPER_BASE.items():
            arr = full(base)
            for cell, value in self.paint.get(name, {}).items():
                arr[cell] = value
            out[name] = arr
        return out


class FakeBucket:
    """Serves synthetic ECMWF files: GET/HEAD by path, ``Range`` honoured (206)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[httpx.Request] = []
        # path -> number of leading GET attempts answered with 503 SlowDown
        self.throttle: dict[str, int] = {}
        self.fail_all: int | None = None

    def add_file(
        self,
        cycle: datetime,
        step: int,
        stream: str,
        fields: dict[str, np.ndarray],
        order: tuple[str, ...],
        skip: tuple[str, ...] = (),
    ) -> None:
        blob, lines = b"", []
        for name in order:
            if name in skip:
                continue
            rng = None
            if name == "10fg":
                rng = f"{step - 1}-{step}" if 0 < step <= 90 else f"{max(step - 6, 0)}-{step}"
                rng = "0" if step == 0 else rng
            msg = grib(np.zeros((NJ, NI)) if name == "10fg" and step == 0 else fields[name], rng)
            lines.append(
                json.dumps(
                    {
                        "domain": "g",
                        "date": f"{cycle:%Y%m%d}",
                        "time": f"{cycle:%H}00",
                        "expver": "0001",
                        "class": "od",
                        "type": "fc",
                        "stream": stream,
                        "step": str(step),
                        "levtype": "sfc",
                        "param": name,
                        "_offset": len(blob),
                        "_length": len(msg),
                    }
                )
            )
            blob += msg
        self.files[urlparse(file_url(BASE, cycle, step, stream)).path] = blob
        index = ("\n".join(lines) + "\n").encode()
        self.files[urlparse(file_url(BASE, cycle, step, stream, "index")).path] = index

    def add_step(
        self,
        cycle: datetime,
        step: int,
        scenario: Scenario,
        *,
        streams: tuple[str, ...] = ("wave", "oper"),
        skip: tuple[str, ...] = (),
    ) -> None:
        if "wave" in streams:
            self.add_file(cycle, step, "wave", scenario.wave_fields(), WAVE_ORDER, skip)
        if "oper" in streams:
            self.add_file(cycle, step, "oper", scenario.oper_fields(), OPER_ORDER, skip)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        slowdown = b"<Error><Code>SlowDown</Code><Message>Please reduce your request rate."
        if self.fail_all is not None:
            return httpx.Response(self.fail_all, content=slowdown if self.fail_all == 503 else b"")
        if request.method == "GET" and self.throttle.get(path, 0) > 0:
            self.throttle[path] -= 1
            return httpx.Response(503, content=slowdown + b"</Message></Error>")
        if path not in self.files:
            return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
        if request.method == "HEAD":
            return httpx.Response(200)
        data = self.files[path]
        rng = request.headers.get("Range")
        if not rng:
            return httpx.Response(200, content=data)
        start_s, end_s = rng.removeprefix("bytes=").split("-")
        end = int(end_s) if end_s else len(data) - 1
        return httpx.Response(206, content=data[int(start_s) : end + 1])

    def gets(self, suffix: str = "") -> list[httpx.Request]:
        return [c for c in self.calls if c.method == "GET" and c.url.path.endswith(suffix)]


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "ecmwf_base_url": BASE,
        "forecast_days": 12,
        "grib_max_cell_distance_km": 400.0,
        "grib_download_workers": 2,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


def provider(bucket: FakeBucket, steps: list[int] | None = None, **kw: object) -> EcmwfIfsProvider:
    return EcmwfIfsProvider(
        settings(**kw),
        client=httpx.Client(transport=httpx.MockTransport(bucket)),
        steps=steps,
        attempts=2,
        backoff_seconds=0,
        request_interval_seconds=0,
    )


RUN = RunInfo(
    run_key="ecmwf-ifs:2026101000",
    issued_at=CYCLE,
    wave_model_run_at=CYCLE,
    atmosphere_model_run_at=CYCLE,
)
# Real seed positions of two spots; the third sits on the dateline (synthetic test point).
NAZARE = SpotPoint(1, "nazare", 39.6065, -9.088, 39.65, -9.28)
PIPELINE = SpotPoint(2, "pipeline", 21.6655, -158.053, 21.8, -158.18)
DATELINE = SpotPoint(3, "dateline-test", -16.0, 179.7, -16.0, 179.7)


# --------------------------------------------------------------------------- helpers
def test_cycle_steps_match_the_bucket_listing() -> None:
    steps = cycle_steps(0, 360)
    assert len(steps) == 85  # listing of 2026-10-10 00Z and 12Z
    assert steps[:3] == [0, 3, 6] and steps[48] == 144 and steps[49] == 150
    assert steps[-1] == 360 and all(
        b - a == 6 for a, b in zip(steps[48:-1], steps[49:], strict=True)
    )
    assert cycle_steps(12, 240)[-3:] == [228, 234, 240] and len(cycle_steps(12, 240)) == 65
    assert cycle_steps(0, 100)[-1] == 99
    with pytest.raises(ValueError):
        cycle_steps(6, 144)


def test_candidate_cycles_respect_availability_lag() -> None:
    late = candidate_cycles(datetime(2026, 10, 10, 23, 30, tzinfo=UTC), 8.0)
    assert late == [
        datetime(2026, 10, 10, 12, tzinfo=UTC),
        datetime(2026, 10, 10, 0, tzinfo=UTC),
        datetime(2026, 10, 9, 12, tzinfo=UTC),
    ]
    early = candidate_cycles(datetime(2026, 10, 10, 19, 0, tzinfo=UTC), 8.0)
    assert early[0] == datetime(2026, 10, 10, 0, tzinfo=UTC)


def test_file_urls_follow_the_bucket_layout() -> None:
    assert file_url(BASE, CYCLE, 24, "wave") == (
        f"{BASE}/20261010/00z/ifs/0p25/wave/20261010000000-24h-wave-fc.grib2"
    )
    assert file_url(BASE, CYCLE, 150, "oper", "index") == (
        f"{BASE}/20261010/00z/ifs/0p25/oper/20261010000000-150h-oper-fc.index"
    )


def test_wind_from_uv_gives_km_h_and_direction_from() -> None:
    speed, direction = wind_from_uv(3.0, 4.0)  # air moving towards the NE
    assert speed == pytest.approx(18.0) and direction == pytest.approx(216.87, abs=0.01)
    assert wind_from_uv(0.0, -5.0)[1] == pytest.approx(0.0)  # moving south = from north
    assert wind_from_uv(-5.0, 0.0)[1] == pytest.approx(90.0)  # moving west = from east
    assert wind_from_uv(0.0, 0.0) == (0.0, None)  # calm: no direction


def test_combined_band_height_adds_energy() -> None:
    assert combined_band_height([3.0, 4.0]) == pytest.approx(5.0)
    assert combined_band_height([1.0, None]) is None
    assert combined_band_height([]) is None


def test_synthetic_minus_180_grid_wraps_longitude() -> None:
    vals = full(1.0)
    vals[ij(-16.0, -180.0)] = 3.3  # column 0 (= 180°)
    vals[ij(-16.0, 178.0)] = 1.1  # last column
    grid = decode_message(grib(vals))
    assert grid.lon0 == pytest.approx(180.0) and grid.is_global_in_longitude  # GRIB2: 180
    sampler = PointSampler(max_distance_km=100)
    # 179.7 lies in the last half column; the grid wraps, so it belongs to column 0
    # (grib.GridField.contains treats a global grid as covering every longitude).
    for n, lon in enumerate((179.7, 179.95, -179.7, 180.0, -180.0)):
        cell = choose_cell(sampler, grid, n, -16.0, lon)
        assert cell is not None and (cell.j, cell.i) == ij(-16.0, -180.0), lon
        assert cell.longitude == pytest.approx(-180.0)
        assert sampler.sample(grid, cell) == pytest.approx(3.3, abs=1e-3)
    seam = choose_cell(sampler, grid, "seam", -16.0, 179.7)
    assert seam is not None and seam.distance_km == pytest.approx(
        haversine_km(-16.0, 179.7, -16.0, -180.0)
    )
    nazare = choose_cell(sampler, grid, "nazare", 39.65, -9.28)
    assert nazare is not None and nazare.longitude == pytest.approx(-10.0)
    # The seam search honours the validity mask too (column 0 masked as land here).
    vals[ij(-16.0, -180.0)] = np.nan
    land = decode_message(grib(vals))
    cell = choose_cell(PointSampler(max_distance_km=300), land, "seam-land", -16.0, 179.7)
    assert cell is not None and (cell.j, cell.i) == ij(-16.0, 178.0)
    assert land.value(cell.j, cell.i) == pytest.approx(1.1, abs=1e-3)


# --------------------------------------------------------------------------- fetch
def standard_bucket(steps: list[int], scenario: Scenario | None = None) -> FakeBucket:
    bucket = FakeBucket()
    for step in steps:
        bucket.add_step(CYCLE, step, scenario or Scenario())
    return bucket


def test_fetch_maps_fields_units_and_period_kind() -> None:
    bucket = standard_bucket([0, 3])
    p = provider(bucket, steps=[0, 3])
    result = p.fetch([NAZARE, PIPELINE], RUN)
    assert result.errors == {}
    recs = result.records[NAZARE.spot_id]
    assert [r.valid_time for r in recs] == [
        datetime(2026, 10, 10, 0, tzinfo=UTC),
        datetime(2026, 10, 10, 3, tzinfo=UTC),
    ]
    rec = recs[1]
    v = rec.values
    assert set(v) == set(FORECAST_FIELDS)
    assert v["sig_wave_height_m"] == pytest.approx(1.0, abs=1e-3)
    assert v["wave_period_s"] == pytest.approx(14.0, abs=0.01) and rec.period_kind == "tp"
    assert v["wave_direction_deg"] == pytest.approx(300.0, abs=0.1)
    assert v["wind_speed_kmh"] == pytest.approx(18.0, abs=0.05)  # 5 m/s
    assert v["wind_direction_deg"] == pytest.approx(216.9, abs=0.1)
    assert v["wind_gust_kmh"] == pytest.approx(36.0, abs=0.05)  # 10 m/s
    assert v["pressure_msl_hpa"] == pytest.approx(1013.25, abs=0.01)
    # ECMWF open data have no partitions: never invented.
    for name in FORECAST_FIELDS:
        if "swell" in name or name.startswith("wind_wave") or name == "sea_level_m":
            assert v[name] is None, name
    extra = rec.extra
    assert extra["mean_period_tm10_s"] == pytest.approx(9.5, abs=0.01)
    assert extra["mean_period_tm02_s"] == pytest.approx(7.5, abs=0.01)
    assert extra["hs_band_m"] == pytest.approx(
        {"10-12s": 0.3, "12-14s": 0.4, "14-17s": 0.5, "17-21s": 0.6, "21-25s": 0.1, "25-30s": 0.05},
        abs=1e-3,
    )
    assert extra["hs_ge_12s_m"] == pytest.approx(
        math.sqrt(0.16 + 0.25 + 0.36 + 0.01 + 0.0025), 1e-3
    )
    assert extra["wave_cell"]["model_depth_m"] == pytest.approx(999.0, abs=0.1)  # type: ignore[index]
    assert extra["wind_gust_window_h"] == 1.0
    assert extra["wind_cell"]["land_fraction"] == pytest.approx(0.0)  # type: ignore[index]
    # Step 0: the gust has no window yet (constant 0 field) and is dropped, not stored as 0.
    first = recs[0]
    assert first.values["wind_gust_kmh"] is None and "wind_gust_window_h" not in first.extra
    assert first.values["wind_speed_kmh"] == pytest.approx(18.0, abs=0.05)
    # One contiguous wave request + four atmosphere requests (lsm only once) per step.
    assert len(bucket.gets("wave-fc.grib2")) == 2
    assert len(bucket.gets("oper-fc.grib2")) == 5 + 4
    assert result.request_count == len(bucket.gets())
    assert p.last_fetch_stats["slowdown"] == 0 and p.last_fetch_stats["bytes"] > 0


def test_waves_use_nearest_valid_wave_cell_and_wind_the_nearest_sea_cell() -> None:
    home = ij(40.0, -10.0)  # cell nearest to Nazaré's break AND its forecast point
    east = ij(40.0, -8.0)
    scenario = Scenario(
        paint={
            "lsm": {home: 1.0},  # IFS calls the coastal cell land ...
            "10u": {home: 20.0},  # ... and its (land) wind must not be used
            "10v": {home: 0.0},
            "swh": {home: 2.5},  # ... while ecWAM keeps a valid sea cell there
        }
    )
    result = provider(standard_bucket([3], scenario), steps=[3]).fetch([NAZARE], RUN)
    rec = result.records[NAZARE.spot_id][0]
    assert rec.values["sig_wave_height_m"] == pytest.approx(2.5, abs=1e-3)
    assert rec.extra["wave_cell"]["lat"] == 40.0 and rec.extra["wave_cell"]["lon"] == -10.0  # type: ignore[index]
    assert rec.values["wind_speed_kmh"] == pytest.approx(18.0, abs=0.05)  # not 72 km/h
    wind_cell = rec.extra["wind_cell"]
    assert (wind_cell["lat"], wind_cell["lon"]) == (40.0, -8.0)  # type: ignore[index]
    assert wind_cell["land_fraction"] == pytest.approx(0.0)  # type: ignore[index]
    assert ij(40.0, -8.0) == east


def test_wave_cell_skips_land_cells() -> None:
    home = ij(40.0, -10.0)
    scenario = Scenario(wave_land={home}, paint={"swh": {ij(40.0, -8.0): 1.7}})
    result = provider(standard_bucket([3], scenario), steps=[3]).fetch([NAZARE], RUN)
    rec = result.records[NAZARE.spot_id][0]
    assert rec.values["sig_wave_height_m"] == pytest.approx(1.7, abs=1e-3)
    assert rec.extra["wave_cell"]["lon"] == -8.0  # type: ignore[index]


def test_dateline_spot_samples_the_minus_180_column() -> None:
    scenario = Scenario(paint={"swh": {ij(-16.0, -180.0): 3.3, ij(-16.0, 178.0): 1.1}})
    result = provider(standard_bucket([3], scenario), steps=[3]).fetch([DATELINE], RUN)
    rec = result.records[DATELINE.spot_id][0]
    assert rec.values["sig_wave_height_m"] == pytest.approx(3.3, abs=1e-3)
    assert rec.extra["wave_cell"]["lon"] == -180.0  # type: ignore[index]


def test_spot_without_a_wave_cell_is_an_error_not_a_guess() -> None:
    far = SpotPoint(9, "inland", 70.0, 100.0, 70.0, 100.0)  # synthetic inland point
    land = {(j, i) for j in range(NJ) for i in range(NI) if abs((90 - j * D) - 70) <= 6}
    result = provider(standard_bucket([3], Scenario(wave_land=land)), steps=[3]).fetch(
        [NAZARE, far], RUN
    )
    assert far.spot_id not in result.records
    assert "no valid ECMWF wave cell" in result.errors[far.spot_id]


def test_missing_step_is_reported_not_dropped() -> None:
    bucket = FakeBucket()
    bucket.add_step(CYCLE, 0, Scenario())
    bucket.add_step(CYCLE, 3, Scenario(), streams=("oper",))  # wave file never published
    bucket.add_step(CYCLE, 6, Scenario(), streams=("wave",))  # atmosphere file missing
    result = provider(bucket, steps=[0, 3, 6]).fetch([NAZARE, PIPELINE], RUN)
    hours = [r.valid_time.hour for r in result.records[NAZARE.spot_id]]
    assert hours == [0, 6]  # no record without wave data
    six = result.records[NAZARE.spot_id][1]
    assert six.values["sig_wave_height_m"] is not None and six.values["wind_speed_kmh"] is None
    assert "wind_cell" not in six.extra
    assert any(w.startswith("+3h wave: not published") for w in result.warnings)
    assert any(w.startswith("+6h oper: not published") for w in result.warnings)
    err = result.errors[NAZARE.spot_id]
    assert err.startswith("incomplete:") and "[3]" in err and "[6]" in err


def test_missing_gust_parameter_is_reported() -> None:
    bucket = FakeBucket()
    bucket.add_step(CYCLE, 3, Scenario(), skip=("10fg",))
    result = provider(bucket, steps=[3]).fetch([NAZARE], RUN)
    rec = result.records[NAZARE.spot_id][0]
    assert rec.values["wind_gust_kmh"] is None and rec.values["wind_speed_kmh"] is not None
    assert any("oper:10fg" in w for w in result.warnings)


def test_slowdown_is_retried_and_counted() -> None:
    bucket = standard_bucket([0, 3])
    index = urlparse(file_url(BASE, CYCLE, 3, "wave", "index")).path
    bucket.throttle[index] = 3  # 2 attempts fail in the first pass, 1 in the second
    p = provider(bucket, steps=[0, 3])
    result = p.fetch([NAZARE], RUN)
    assert [r.valid_time.hour for r in result.records[NAZARE.spot_id]] == [0, 3]
    assert result.errors == {}
    assert p.last_fetch_stats["slowdown"] == 3 and p.last_fetch_stats["http_503"] == 3
    assert "SlowDown" in result.warnings[0]


def test_persistent_throttling_fails_transiently() -> None:
    bucket = standard_bucket([0])
    bucket.fail_all = 503
    with pytest.raises(TransientProviderError):
        provider(bucket, steps=[0]).fetch([NAZARE], RUN)


def test_unpublished_cycle_fails_permanently() -> None:
    with pytest.raises(PermanentProviderError):
        provider(FakeBucket(), steps=[0]).fetch([NAZARE], RUN)


def test_corrupt_index_is_isolated_to_its_step() -> None:
    bucket = standard_bucket([0, 3])
    bucket.files[urlparse(file_url(BASE, CYCLE, 3, "wave", "index")).path] = b'{"param": "swh"}\n'
    result = provider(bucket, steps=[0, 3]).fetch([NAZARE], RUN)
    assert [r.valid_time.hour for r in result.records[NAZARE.spot_id]] == [0]
    assert any(w.startswith("+3h wave: KeyError") for w in result.warnings)


def test_damaged_message_is_isolated_to_its_step() -> None:
    # Review finding: a message whose header parses but whose data section is damaged made
    # ecCodes raise KeyValueNotFoundError, which _fetch_step did not catch, so one bad
    # message aborted the whole fetch (and left the run RUNNING).
    bucket = standard_bucket([0, 3])
    index = urlparse(file_url(BASE, CYCLE, 3, "wave", "index")).path
    path = urlparse(file_url(BASE, CYCLE, 3, "wave")).path
    rows = [json.loads(line) for line in bucket.files[index].decode().splitlines()]
    swh = next(r for r in rows if r["param"] == "swh")
    start, length = swh["_offset"], swh["_length"]
    blob = bucket.files[path]
    damaged = blob[start : start + 60] + bytes(length - 64) + b"7777"  # same length
    bucket.files[path] = blob[:start] + damaged + blob[start + length :]
    result = provider(bucket, steps=[0, 3]).fetch([NAZARE], RUN)
    assert [r.valid_time.hour for r in result.records[NAZARE.spot_id]] == [0]
    assert any(w.startswith("+3h wave: GribError: cannot decode") for w in result.warnings)
    assert result.errors[NAZARE.spot_id].startswith("incomplete:")


def _global_message(value: float, d: float) -> bytes:
    """SYNTHETIC global message on a d-degree grid from -180 (values: ``value`` + noise)."""
    nj, ni = int(180 / d) + 1, int(360 / d)
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "Ni", ni)
        eccodes.codes_set(h, "Nj", nj)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", 90.0)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", -180.0)
        eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", -90.0)
        eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", 180.0 - d)
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", d)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", d)
        eccodes.codes_set(h, "jScansPositively", 0)
        noise = np.random.default_rng(0).random(nj * ni) * 0.01
        eccodes.codes_set_values(h, value + noise)
        return bytes(eccodes.codes_get_message(h))
    finally:
        eccodes.codes_release(h)


def test_wave_fields_are_decoded_one_at_a_time() -> None:
    # Review finding: _sample_wave kept every decoded wave field of a step alive at once
    # (12 x 8.3 MB = ~100 MB per worker on the real 0.25 deg grid; measured peak 92 MB).
    import tracemalloc

    from app.services.forecasts.ecmwf import WAVE_PARAMS, _RunState

    d = 0.5
    field_mb = (int(180 / d) + 1) * int(360 / d) * 8 / 1e6  # one float64 field: 2.1 MB
    blobs = {name: _global_message(1.0 + k, d) for k, name in enumerate(WAVE_PARAMS)}
    p = provider(FakeBucket())
    state = _RunState(PointSampler(settings().grib_max_cell_distance_km))
    p._sample_wave(blobs, [NAZARE, PIPELINE], state)  # chooses cells, reads the depth
    tracemalloc.start()
    try:
        out = p._sample_wave(blobs, [NAZARE, PIPELINE], state)
        peak_mb = tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()
    assert set(out[NAZARE.spot_id]) == set(WAVE_PARAMS) - {"wmb"}
    assert out[NAZARE.spot_id]["swh"] == pytest.approx(1.0, abs=0.02)  # swh is the first
    assert peak_mb < 5 * field_mb, f"peak {peak_mb:.1f} MB for {field_mb:.1f} MB fields"


def test_naive_run_time_is_utc_whatever_the_server_time_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review finding: _cycle_of() called astimezone() on a naive time, which takes it as
    # the server's local time (00Z became 04Z under America/New_York).
    import time

    naive = CYCLE.replace(tzinfo=None)
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        run = RunInfo(run_key="ecmwf-ifs:2026101000", issued_at=naive, wave_model_run_at=naive)
        assert EcmwfIfsProvider._cycle_of(run) == CYCLE
    finally:
        monkeypatch.undo()
        time.tzset()


# --------------------------------------------------------------------------- runs
def test_current_run_probes_last_step_and_falls_back_a_cycle() -> None:
    bucket = FakeBucket()
    older = datetime(2026, 10, 10, 0, tzinfo=UTC)
    for stream in ("wave", "oper"):  # 00Z complete to +240 h; 12Z not yet
        bucket.files[urlparse(file_url(BASE, older, 240, stream, "index")).path] = b"{}"
    p = provider(bucket)
    run = p.current_run(datetime(2026, 10, 10, 23, 30, tzinfo=UTC))
    assert run.run_key == "ecmwf-ifs:2026101000"
    assert run.issued_at == run.wave_model_run_at == run.atmosphere_model_run_at == older
    heads = [c.url.path for c in bucket.calls if c.method == "HEAD"]
    assert heads[0].endswith("20261010/12z/ifs/0p25/wave/20261010120000-240h-wave-fc.index")
    assert heads[-1].endswith("20261010/00z/ifs/0p25/oper/20261010000000-240h-oper-fc.index")
    assert p.probe_request_count == len(heads) == 3


def test_current_run_requires_both_streams() -> None:
    bucket = FakeBucket()
    cycle = datetime(2026, 10, 10, 12, tzinfo=UTC)
    bucket.files[urlparse(file_url(BASE, cycle, 240, "wave", "index")).path] = b"{}"
    with pytest.raises(TransientProviderError, match="no complete 00/12Z cycle"):
        provider(bucket).current_run(datetime(2026, 10, 10, 23, 30, tzinfo=UTC))


def test_current_run_raises_when_probes_are_throttled() -> None:
    bucket = FakeBucket()
    bucket.fail_all = 503
    with pytest.raises(TransientProviderError):
        provider(bucket).current_run(datetime(2026, 10, 10, 23, 30, tzinfo=UTC))


def test_run_key_without_timestamps_is_parsed() -> None:
    result = provider(standard_bucket([3]), steps=[3]).fetch(
        [NAZARE], RunInfo(run_key="ecmwf-ifs:2026101000", issued_at=CYCLE)
    )
    assert result.records[NAZARE.spot_id][0].valid_time == datetime(2026, 10, 10, 3, tzinfo=UTC)


# --------------------------------------------------------------------------- metadata
def test_source_info_family_and_attribution() -> None:
    info = provider(FakeBucket()).source_info()
    assert info.code == ECMWF_IFS_CODE == "ecmwf_ifs_open"
    assert info.family == "ecmwf_ifs" and info.kind == "deterministic" and not info.is_demo
    assert "ECMWF" in info.attribution and "CC BY 4.0" in info.attribution
    assert LICENSE_URL in info.attribution and "odified" in info.attribution
    assert LICENSE_URL in info.license_notes
    assert "apps.ecmwf.int/datasets/licences/general" in info.license_notes


def test_registry_builds_the_provider_with_capped_concurrency() -> None:
    p = build_provider("ecmwf_ifs", settings(grib_download_workers=16))
    assert isinstance(p, EcmwfIfsProvider) and p.workers == 4
    assert p.max_lead_hours == 240  # ECMWF_MAX_LEAD_HOURS default
    short = build_provider("ecmwf_ifs", settings(forecast_days=3))
    assert isinstance(short, EcmwfIfsProvider) and short.max_lead_hours == 72


def test_explicit_hooks_are_kept() -> None:
    seen: list[int] = []

    def hook(response: httpx.Response) -> None:
        seen.append(response.status_code)

    client = httpx.Client(transport=httpx.MockTransport(standard_bucket([3])))
    client.event_hooks["response"].append(hook)
    p = EcmwfIfsProvider(
        settings(), client=client, steps=[3], backoff_seconds=0, request_interval_seconds=0
    )
    assert p.fetch([NAZARE], RUN).records
    assert seen and all(code in (200, 206) for code in seen)
