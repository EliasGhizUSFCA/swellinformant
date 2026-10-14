"""Direct NOAA GFS-Wave + GFS ingestion against SYNTHETIC GRIB2 files (no network).

The files are built by tests.synthetic_gfs: made-up constant fields in the real NODD file
layout, served through httpx.MockTransport. Live behaviour is checked separately (see
docs/noaa-direct-ingestion.md).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest

from app.core.config import Settings
from app.services.forecasts.base import RunInfo, SpotPoint
from app.services.forecasts.fields import EncodedField, FieldSink, TargetGrid, complete_times
from app.services.forecasts.grib import parse_ncep_idx
from app.services.forecasts.noaa import (
    GfsPaths,
    GfsPointReader,
    NoaaGfsFieldsProvider,
    NoaaGfsProvider,
    SamplePoint,
    forecast_hours,
    sample_gfs_points,
    wave_grid_for,
)
from app.services.forecasts.registry import NOAA_GFS_CODE, build_provider
from app.services.geo import wind_from_uv
from app.services.http import PermanentProviderError, TransientProviderError
from tests.synthetic_gfs import (
    ATMOS_GRID,
    BASE_URL,
    PATHS,
    WAVE_GRID_GEOMETRY,
    Grid,
    SyntheticGfs,
    map_settings,
    nearest_cell_mask,
    noaa_settings,
)

CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
RUN = RunInfo("noaa-gfs:2026101018", CYCLE, CYCLE, CYCLE, "noaa-s3-idx")
HOURS = [0, 12, 24]

# Synthetic test spots (break lat/lon, then offshore forecast point), one per wave grid.
HAWAII = SpotPoint(1, "hawaii-test", 21.66, -158.05, 21.80, -158.18)
SOUTH = SpotPoint(2, "south-test", -34.03, 24.93, -34.20, 25.00)
NORTH = SpotPoint(3, "north-test", 58.60, -3.51, 58.77, -3.69)


def settings(lead: int = 24, step: int = 12, **kw: object) -> Settings:
    return noaa_settings(lead, step, **kw)


def provider(world: SyntheticGfs, lead: int = 24, step: int = 12) -> NoaaGfsProvider:
    return NoaaGfsProvider(settings(lead, step), client=world.client(), backoff_seconds=0)


def hawaii_break_on_land() -> SyntheticGfs:
    """All three grids, three steps; the GFS cell nearest the Hawaii break is land."""
    world = SyntheticGfs()
    land = nearest_cell_mask([(HAWAII.latitude, HAWAII.longitude)], ATMOS_GRID)
    world.add_cycle(CYCLE, HOURS, land=land)
    return world


# --------------------------------------------------------------------------- pure helpers
def test_forecast_hours_follow_the_published_cadence() -> None:
    three_hourly = forecast_hours(240, 3)
    assert three_hourly[:3] == [0, 3, 6] and three_hourly[-1] == 240
    assert len(three_hourly) == 81  # 41 steps to f120, 40 after
    assert forecast_hours(130, 1)[-4:] == [120, 123, 126, 129]  # hourly only to f120
    assert forecast_hours(130, 4)[-3:] == [116, 120, 126]  # f124 does not exist
    assert forecast_hours(999, 6)[-1] == 384
    s = settings(lead=240, step=3, forecast_days=3)
    assert NoaaGfsProvider(s).forecast_hours()[-1] == 72  # forecast_days caps the lead


@pytest.mark.parametrize(
    ("lat", "grid"),
    [
        (52.5, "global.0p16"),
        (21.8, "global.0p16"),
        (-12.0, "global.0p16"),  # inside both native grids: 1/6 deg preferred
        (-15.0, "global.0p16"),
        (-15.01, "gsouth.0p25"),
        (-34.2, "gsouth.0p25"),
        (-79.5, "gsouth.0p25"),
        (-80.0, "global.0p25"),
        (52.51, "global.0p25"),
        (58.77, "global.0p25"),
    ],
)
def test_wave_grid_by_latitude(lat: float, grid: str) -> None:
    assert wave_grid_for(lat).name == grid


@pytest.mark.parametrize(
    ("u", "v", "from_deg"),
    [
        (0.0, -5.0, 0.0),  # blowing to the south = northerly
        (-5.0, 0.0, 90.0),  # blowing to the west = easterly
        (0.0, 5.0, 180.0),
        (5.0, 0.0, 270.0),
        (-3.0, -3.0, 45.0),  # quadrant NE
        (-3.0, 3.0, 135.0),  # quadrant SE
        (3.0, 3.0, 225.0),  # quadrant SW
        (3.0, -3.0, 315.0),  # quadrant NW
    ],
)
def test_wind_from_direction_in_every_quadrant(u: float, v: float, from_deg: float) -> None:
    speed, direction = wind_from_uv(u, v)
    assert speed == pytest.approx(np.hypot(u, v))
    assert direction == pytest.approx(from_deg)


def test_calm_wind_has_no_direction() -> None:
    assert wind_from_uv(0.0, 0.0) == (0.0, None)


def test_paths_follow_the_v16_layout_and_templates_are_replaceable() -> None:
    assert PATHS.wave(CYCLE, "global.0p16", 6) == (
        f"{BASE_URL}/gfs.20261010/18/wave/gridded/gfswave.t18z.global.0p16.f006.grib2"
    )
    assert PATHS.atmos(CYCLE, 123) == f"{BASE_URL}/gfs.20261010/18/atmos/gfs.t18z.pgrb2.0p25.f123"
    renamed = GfsPaths(BASE_URL, wave_template="v17/{ymd}{hh}/{grid}/f{fhour:03d}.grib2")
    assert (
        renamed.wave(CYCLE, "global.0p16", 6) == f"{BASE_URL}/v17/2026101018/global.0p16/f006.grib2"
    )
    assert GfsPaths.from_settings(settings()).base_url == BASE_URL


def test_source_info_and_registry() -> None:
    p = build_provider("noaa_gfs", settings())
    assert isinstance(p, NoaaGfsProvider) and p.code == NOAA_GFS_CODE == "noaa_gfs_direct"
    info = p.source_info()
    assert info.provider == "noaa_nodd" and info.family == "ncep_gfs"
    assert info.kind == "deterministic" and not info.is_demo
    assert info.atmosphere_model == "GFS 0.25 deg"
    assert "NODD" in info.attribution and "US government work" in info.license_notes
    # forecast_sources column widths (code 40, name 120, provider 40, models 60).
    assert len(info.code) <= 40 and len(info.name) <= 120 and len(info.provider) <= 40
    assert len(info.wave_model or "") <= 60 and len(info.atmosphere_model or "") <= 60


def test_reader_rejects_a_time_that_is_not_a_cycle() -> None:
    with pytest.raises(PermanentProviderError):
        GfsPointReader(httpx.Client(), CYCLE - timedelta(hours=1), [], settings=settings())


# --------------------------------------------------------------------------- fetch
def test_fetch_maps_fields_units_and_grids() -> None:
    world = hawaii_break_on_land()
    p = provider(world)
    result = p.fetch([HAWAII, SOUTH, NORTH], RUN)

    assert result.errors == {}
    assert set(result.records) == {1, 2, 3}
    for spot_id, records in result.records.items():
        assert [r.valid_time for r in records] == [CYCLE + timedelta(hours=h) for h in HOURS]
        assert all(r.period_kind == "tp" for r in records)
        # The chosen cells ride on the first record only (-> wave_forecasts.extra).
        assert records[0].extra == p.last_cells[spot_id]
        assert all(r.extra == {} for r in records[1:])
    assert {k: v["grid"] for k, v in p.last_cells.items()} == {
        1: "global.0p16",
        2: "gsouth.0p25",
        3: "global.0p25",
    }

    first = result.records[1][0].values
    assert first == {
        "sig_wave_height_m": 2.0,
        "wave_period_s": 14.0,
        "wave_direction_deg": 280.0,
        "primary_swell_height_m": 1.5,
        "primary_swell_period_s": 15.0,
        "primary_swell_direction_deg": 290.0,
        "secondary_swell_height_m": 0.8,
        "secondary_swell_period_s": 11.0,
        "secondary_swell_direction_deg": 200.0,
        "tertiary_swell_height_m": 0.3,
        "tertiary_swell_period_s": 18.0,
        "tertiary_swell_direction_deg": 0.0,  # 359.96 rounds to 360 = 0
        "wind_wave_height_m": 0.6,
        "wind_wave_period_s": 6.0,
        "wind_wave_direction_deg": 45.0,
        "wind_speed_kmh": 18.0,  # |(3, 4)| = 5 m/s x 3.6
        "wind_direction_deg": 216.9,  # FROM: (270 - atan2(4, 3)) mod 360
        "wind_gust_kmh": 36.0,  # 10 m/s x 3.6
        "pressure_msl_hpa": 1013.25,  # 101325 Pa / 100
        "sea_level_m": None,  # GFS has no tide
    }
    assert result.records[1][2].values["sig_wave_height_m"] == 2.24  # f024 field

    # Requests: per step one atmosphere index + 3 ranges (+ LAND on the first step) and
    # one index + one range per wave grid. Nothing is retried in this scenario.
    gets = world.paths_requested("GET")
    assert result.request_count == len(gets) == 11 + 10 + 10
    assert result.warnings[0].startswith(f"downloaded {p.last_bytes_downloaded / 1e6:.1f} MB")
    assert sum(p.last_step_bytes.values()) == p.last_bytes_downloaded > 0
    assert "global.0p16=1, gsouth.0p25=1, global.0p25=1 (interpolated)" in result.warnings[1]
    assert not any("differ from the expected" in w for w in result.warnings)


def test_only_the_wave_grids_in_use_are_downloaded() -> None:
    world = hawaii_break_on_land()
    result = provider(world).fetch([HAWAII], RUN)
    assert set(result.records) == {1}
    requested = world.paths_requested("GET")
    assert any("global.0p16" in path for path in requested)
    assert not any("gsouth" in path or "global.0p25" in path for path in requested)


def test_wind_comes_from_the_nearest_sea_cell() -> None:
    world = hawaii_break_on_land()
    p = provider(world)
    values = p.fetch([HAWAII], RUN).records[1][0].values
    # The nearest GFS cell (21.5N 158.0W, 18 km) is land (u=-20 m/s, gust 30 m/s there).
    assert values["wind_speed_kmh"] == 18.0 and values["wind_gust_kmh"] == 36.0
    wind_cell = p.last_cells[1]["wind_cell"]
    assert wind_cell == {"lat": 22.0, "lon": -158.0, "distance_km": 38.2, "grid": "pgrb2.0p25"}


def test_wind_cell_just_west_of_the_prime_meridian() -> None:
    # Review finding: on a global grid starting at 0E, a break between 0.25W (half a cell
    # of the synthetic 0.5 deg atmosphere grid) and 0 got no wind cell at all.
    meridian = SpotPoint(6, "meridian-test", 50.70, -0.10, 50.60, -0.10)  # synthetic point
    world = SyntheticGfs()
    world.add_atmos(CYCLE, 0)
    world.add_wave(CYCLE, "global.0p16", 0, grid=Grid(52.5, 358.0, 1 / 6, 406, 12))
    p = provider(world, lead=0)
    result = p.fetch([meridian], RUN)
    values = result.records[6][0].values
    assert values["wind_speed_kmh"] == 18.0 and values["sig_wave_height_m"] == 2.0
    assert p.last_cells[6]["wind_cell"] == {
        "lat": 50.5,
        "lon": 0.0,
        "distance_km": 23.3,
        "grid": "pgrb2.0p25",
    }
    assert not any("no GFS sea cell" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("u", "v", "from_deg"),
    [(-3.0, -4.0, 36.9), (-3.0, 4.0, 143.1), (3.0, 4.0, 216.9), (3.0, -4.0, 323.1)],
)
def test_provider_wind_direction_is_from_in_every_quadrant(
    u: float, v: float, from_deg: float
) -> None:
    world = SyntheticGfs()
    world.add_atmos(CYCLE, 0, uv=(u, v))
    world.add_wave(CYCLE, "global.0p16", 0)
    values = provider(world, lead=0).fetch([HAWAII], RUN).records[1][0].values
    assert values["wind_direction_deg"] == from_deg
    assert values["wind_speed_kmh"] == 18.0


def test_without_land_the_nearest_cell_is_used() -> None:
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS, grids=("global.0p16",))
    p = provider(world)
    p.fetch([HAWAII], RUN)
    assert p.last_cells[1]["wind_cell"] == {
        "lat": 21.5,
        "lon": -158.0,
        "distance_km": 18.5,
        "grid": "pgrb2.0p25",
    }
    wave_cell = p.last_cells[1]["wave_cell"]
    assert wave_cell == {
        "lat": 21.8333,
        "lon": -158.1667,
        "distance_km": 3.9,
        "anchor": "wave_point",
    }


def test_waves_skip_masked_cells_near_the_forecast_point() -> None:
    world = SyntheticGfs()
    masked = nearest_cell_mask(
        [(HAWAII.forecast_latitude, HAWAII.forecast_longitude)], WAVE_GRID_GEOMETRY["global.0p16"]
    )
    world.add_cycle(
        CYCLE,
        HOURS,
        grids=("global.0p16",),
        wave_valid={"global.0p16": lambda lat, lon: ~masked(lat, lon)},
    )
    p = provider(world)
    result = p.fetch([HAWAII], RUN)
    cell = p.last_cells[1]["wave_cell"]
    assert isinstance(cell, dict)
    assert (cell["lat"], cell["lon"]) != (21.8333, -158.1667)  # the masked nearest cell
    assert cell["anchor"] == "wave_point" and 3.9 < float(cell["distance_km"]) < 20
    assert result.records[1][0].values["sig_wave_height_m"] == 2.0


def test_waves_fall_back_to_the_break_and_unusable_spots_are_errors() -> None:
    world = SyntheticGfs()
    # Only cells south of 21.2N hold wave values (synthetic "land" everywhere else).
    world.add_cycle(
        CYCLE,
        HOURS,
        grids=("global.0p16",),
        wave_valid={"global.0p16": lambda lat, lon: lat < 21.2},
    )
    fallback = SpotPoint(4, "fallback-test", 20.70, -158.00, 22.30, -158.00)
    hopeless = SpotPoint(5, "hopeless-test", 22.40, -158.00, 22.40, -158.00)
    p = provider(world)
    result = p.fetch([fallback, hopeless], RUN)
    assert len(result.records[4]) == 3
    cell = p.last_cells[4]["wave_cell"]
    assert isinstance(cell, dict) and cell["anchor"] == "wind_point"
    assert set(result.errors) == {5}
    assert "no valid GFS-Wave global.0p16 cell within 60 km" in result.errors[5]


def test_missing_steps_leave_gaps_with_warnings() -> None:
    world = hawaii_break_on_land()
    world.fail_url(PATHS.wave(CYCLE, "global.0p16", 12) + ".idx", 404)  # not published
    world.fail_url(PATHS.atmos(CYCLE, 24), 503)  # transient, still failing after retries
    p = provider(world)
    result = p.fetch([HAWAII, SOUTH], RUN)

    # Review finding: the gaps used to leave result.errors empty, so a run with missing
    # hours was stored as SUCCESS. Each affected spot is now "incomplete" (run PARTIAL).
    assert result.errors == {
        1: "incomplete: waves missing at 1 of 3 forecast hours f012; "
        "wind/pressure missing at 1 of 3 forecast hours f024",
        2: "incomplete: wind/pressure missing at 1 of 3 forecast hours f024",
    }
    hawaii = {r.valid_time: r.values for r in result.records[1]}
    assert sorted(hawaii) == [CYCLE, CYCLE + timedelta(hours=24)]  # f012 is a gap
    south = {r.valid_time: r.values for r in result.records[2]}
    assert len(south) == 3
    # f024: waves without wind (the atmosphere file failed), not dropped.
    late = south[CYCLE + timedelta(hours=24)]
    assert late["sig_wave_height_m"] == 2.24 and late["wind_speed_kmh"] is None
    assert any("2 of 3 forecast hours incomplete: f012, f024" in w for w in result.warnings)
    assert any(w.startswith("f012 wave global.0p16:") and "HTTP 404" in w for w in result.warnings)
    assert any(w.startswith("f024 atmosphere:") and "503" in w for w in result.warnings)
    # The 503 was retried (4 attempts) before giving up; request_count counts it once.
    atmos_24 = httpx.URL(PATHS.atmos(CYCLE, 24)).path
    assert world.paths_requested("GET").count(atmos_24) == 4


def test_a_record_from_the_wrong_step_is_refused() -> None:
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS, grids=("global.0p16",))
    f000 = httpx.URL(PATHS.wave(CYCLE, "global.0p16", 0)).path
    f012 = httpx.URL(PATHS.wave(CYCLE, "global.0p16", 12)).path
    world.files[f012], world.files[f012 + ".idx"] = world.files[f000], world.files[f000 + ".idx"]
    result = provider(world).fetch([HAWAII], RUN)
    assert len(result.records[1]) == 2
    assert any("f012 wave global.0p16" in w and "date/time/step" in w for w in result.warnings)


def test_damaged_index_or_message_is_a_gap_not_a_crash() -> None:
    # Review finding: a malformed .idx offset (ValueError) or a damaged GRIB data section
    # (ecCodes KeyValueNotFoundError) escaped read_step and aborted the whole fetch.
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS, grids=("global.0p16",))
    idx = httpx.URL(PATHS.wave(CYCLE, "global.0p16", 12)).path + ".idx"
    world.files[idx] = world.files[idx].replace(b"\n5:", b"\n5:x", 1)
    atmos = httpx.URL(PATHS.atmos(CYCLE, 24)).path
    lines = world.files[atmos + ".idx"].decode().splitlines()
    offsets = [int(line.split(":")[1]) for line in lines]
    n = next(k for k, line in enumerate(lines) if ":UGRD:10 m above ground:" in line)
    start, end = offsets[n], offsets[n + 1]
    blob = world.files[atmos]  # header kept, rest of the U10 message zeroed (same length)
    world.files[atmos] = blob[: start + 60] + bytes(end - start - 64) + b"7777" + blob[end:]
    result = provider(world).fetch([HAWAII], RUN)
    by_hour = {r.valid_time: r.values for r in result.records[1]}
    assert sorted(by_hour) == [CYCLE, CYCLE + timedelta(hours=24)]  # f012 is a wave gap
    assert by_hour[CYCLE + timedelta(hours=24)]["wind_speed_kmh"] is None  # and f024 wind
    assert any(w.startswith("f012 wave global.0p16:") and "line 5" in w for w in result.warnings)
    assert any(w.startswith("f024 atmosphere:") and "GRIB" in w for w in result.warnings)
    assert result.errors[1].startswith("incomplete: ")


def test_missing_partitions_are_none_not_zero() -> None:
    world = SyntheticGfs()
    world.add_atmos(CYCLE, 0)
    world.add_wave(CYCLE, "gsouth.0p25", 0, missing_partition3=True)
    result = provider(world, lead=24, step=24).fetch([SOUTH], RUN)
    values = result.records[2][0].values
    assert values["secondary_swell_height_m"] == 0.8
    assert values["tertiary_swell_height_m"] is None
    assert values["tertiary_swell_period_s"] is None
    assert values["tertiary_swell_direction_deg"] is None


def test_unexpected_grid_geometry_is_reported() -> None:
    world = SyntheticGfs()
    narrow = Grid(30.0, 199.0, 1 / 6, 60, 24)  # 30N..20.2N instead of 52.5N..15S
    for h in HOURS:
        world.add_atmos(CYCLE, h)
        world.add_wave(CYCLE, "global.0p16", h, grid=narrow)
    result = provider(world).fetch([HAWAII], RUN)
    assert len(result.records[1]) == 3
    assert any("global.0p16: decoded latitudes 30..20.1" in w for w in result.warnings)


def test_fetch_fails_when_no_file_can_be_read() -> None:
    with pytest.raises(TransientProviderError, match="no NOAA GFS file could be read"):
        provider(SyntheticGfs()).fetch([HAWAII], RUN)


# --------------------------------------------------------------------------- current_run
def _publish_last_step(world: SyntheticGfs, cycle: datetime, *, skip: str | None = None) -> None:
    urls = [PATHS.atmos(cycle, 24)] + [
        PATHS.wave(cycle, g, 24) for g in ("global.0p16", "gsouth.0p25", "global.0p25")
    ]
    for url in urls:
        if skip is None or skip not in url:
            world.files[httpx.URL(url).path + ".idx"] = b""


def test_current_run_falls_back_to_an_older_complete_cycle() -> None:
    now = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # nominal cycle (5 h lag): 18Z
    world = SyntheticGfs()
    _publish_last_step(world, CYCLE, skip="gsouth")  # 18Z f024 not complete yet
    _publish_last_step(world, CYCLE - timedelta(hours=6))
    run = provider(world).current_run(now)
    assert run.run_key == "noaa-gfs:2026101012"
    twelve = datetime(2026, 10, 10, 12, tzinfo=UTC)
    assert run.issued_at == run.wave_model_run_at == run.atmosphere_model_run_at == twelve
    assert run.metadata_source == "noaa-s3-idx"
    heads = world.paths_requested("HEAD")
    assert all(path.endswith("f024.idx") or path.endswith("f024.grib2.idx") for path in heads)
    assert len(heads) == 3 + 4  # 18Z stops at the first missing file


def test_current_run_raises_when_no_recent_cycle_is_complete() -> None:
    world = SyntheticGfs()
    _publish_last_step(world, CYCLE - timedelta(hours=30))  # five cycles back: too old
    with pytest.raises(TransientProviderError, match="no complete NOAA GFS cycle up to f024"):
        provider(world).current_run(datetime(2026, 10, 10, 23, 40, tzinfo=UTC))


def test_current_run_network_failure_does_not_fall_back() -> None:
    seen: list[str] = []

    def broken(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    p = NoaaGfsProvider(
        settings(), client=httpx.Client(transport=httpx.MockTransport(broken)), backoff_seconds=0
    )
    with pytest.raises(TransientProviderError):
        p.current_run(datetime(2026, 10, 10, 23, 40, tzinfo=UTC))
    assert seen and all("gfs.20261010/18/" in path for path in seen)


# --------------------------------------------------------------------------- helper
def test_sample_gfs_points_at_arbitrary_positions() -> None:
    world = SyntheticGfs()
    world.add_cycle(CYCLE, [12], grids=("global.0p16",))
    buoy = SamplePoint.at("buoy:51201", 21.67, -158.12)
    outside = SamplePoint.at("outside", 0.0, 0.0)  # not covered by the synthetic grid
    s = sample_gfs_points(
        [buoy, outside], CYCLE, 12, settings=settings(), client=world.client(), backoff_seconds=0
    )
    assert s.valid_time == CYCLE + timedelta(hours=12) and s.failed == []
    assert s.values["buoy:51201"]["sig_wave_height_m"] == 2.12
    assert s.values["buoy:51201"]["wind_speed_kmh"] == 18.0
    assert "outside" not in s.values
    assert s.cells["outside"]["wave_cell"] is None
    assert s.cells["buoy:51201"]["grid"] == "global.0p16"
    assert s.request_count == len(world.paths_requested("GET")) and s.bytes_downloaded > 0


# --------------------------------------------------------------------------- map fields
def field_provider(world: SyntheticGfs, map_lead: int = 24, map_step: int = 12) -> NoaaGfsProvider:
    """Spot provider in the "with_noaa" map-fields mode (spot hours f000-f024 every 12 h)."""
    s = map_settings(24, 12, map_lead=map_lead, map_step=map_step)
    return NoaaGfsProvider(s, client=world.client(), backoff_seconds=0)


def fields_only(world: SyntheticGfs, map_lead: int = 24) -> NoaaGfsFieldsProvider:
    s = map_settings(map_lead=map_lead, providers="demo", map_fields_enabled=True)
    return NoaaGfsFieldsProvider(s, client=world.client(), backoff_seconds=0)


def gets(world: SyntheticGfs, url: str) -> list[httpx.Request]:
    path = httpx.URL(url).path
    return [r for r in world.requests if r.method == "GET" and r.url.path == path]


def offsets(world: SyntheticGfs, url: str) -> dict[str, int]:
    """Record key -> byte offset, from the synthetic file's .idx."""
    text = world.files[httpx.URL(url).path + ".idx"].decode()
    return {e.key: e.offset for e in parse_ncep_idx(text)}


def by_variable(fields: list[EncodedField], valid_time: datetime) -> dict[str, np.ndarray]:
    return {f.variable: f.values() for f in fields if f.valid_time == valid_time}


def test_map_fields_are_off_by_default_and_an_idle_sink_changes_nothing() -> None:
    p = provider(hawaii_break_on_land())
    assert p.map_fields is False  # FORECAST_PROVIDERS=demo in the tests: mode "off"
    result = p.fetch([HAWAII, SOUTH, NORTH], RUN)
    assert result.fields == [] and result.field_warnings == []
    assert not any(w.startswith("map fields") for w in result.warnings)

    plain, idle = hawaii_break_on_land(), hawaii_break_on_land()
    points = [SamplePoint.for_spot(s) for s in (HAWAII, SOUTH, NORTH)]
    a = GfsPointReader(plain.client(), CYCLE, points, settings=settings(), backoff_seconds=0)
    b = GfsPointReader(
        idle.client(),
        CYCLE,
        points,
        settings=settings(),
        backoff_seconds=0,
        field_sink=FieldSink(CYCLE, [], 0.5),  # wants no hour
    )
    assert [s.values for s in a.read_steps(HOURS)] == [s.values for s in b.read_steps(HOURS)]

    def ranges(world: SyntheticGfs) -> list[tuple[str, str, str | None]]:
        return sorted((r.method, r.url.path, r.headers.get("Range")) for r in world.requests)

    assert ranges(plain) == ranges(idle)  # the same files and the same byte ranges


def test_an_invalid_map_resolution_never_costs_the_spots() -> None:
    # 0.7 deg passes the setting's own range check but does not divide 180.
    s = map_settings().model_copy(update={"map_field_resolution_deg": 0.7})
    p = NoaaGfsProvider(s, client=hawaii_break_on_land().client(), backoff_seconds=0)
    result = p.fetch([HAWAII, SOUTH], RUN)
    baseline = provider(hawaii_break_on_land()).fetch([HAWAII, SOUTH], RUN)
    assert result.records == baseline.records and result.errors == baseline.errors == {}
    assert result.fields == [] and len(result.field_warnings) == 1
    assert "must divide 180" in result.field_warnings[0]

    s = map_settings(providers="demo", map_fields_enabled=True)
    fields = NoaaGfsFieldsProvider(
        s.model_copy(update={"map_field_resolution_deg": 0.7}),
        client=SyntheticGfs().client(),
        backoff_seconds=0,
    )
    with pytest.raises(PermanentProviderError, match="must divide 180"):
        fields.fetch([], fields.run_for_cycle(CYCLE))


def test_reader_hands_fields_to_the_sink_for_wanted_hours_only() -> None:
    world = SyntheticGfs()
    world.add_cycle(CYCLE, HOURS, grids=("global.0p16", "global.0p25"))
    sink = FieldSink(CYCLE, [0, 24], 0.5)
    reader = GfsPointReader(
        world.client(),
        CYCLE,
        [SamplePoint.for_spot(HAWAII)],
        settings=settings(),
        backoff_seconds=0,
        field_sink=sink,
    )
    steps = reader.read_steps(HOURS)
    assert complete_times(sink.fields) == [CYCLE, CYCLE + timedelta(hours=24)]
    assert len(sink.fields) == 12 and sink.warnings == []
    assert gets(world, PATHS.wave(CYCLE, "global.0p25", 12)) == []  # f012 is not a map hour
    assert all(s.failed == [] for s in steps) and len(steps[1].values) == 1


def test_fetch_with_map_fields_reads_three_wave_records_and_extra_hours() -> None:
    world = hawaii_break_on_land()  # f000, f012 and f024 of every grid
    world.add_field_hours(CYCLE, [36, 48])  # map hours beyond the spots' last hour
    p = field_provider(world, map_lead=48)
    assert p.map_fields is True
    result = p.fetch([HAWAII, SOUTH], RUN)

    # Spot output is exactly what the provider gives without map fields.
    baseline = provider(hawaii_break_on_land()).fetch([HAWAII, SOUTH], RUN)
    assert result.records == baseline.records and result.errors == baseline.errors == {}

    hours = [0, 12, 24, 36, 48]
    assert complete_times(result.fields) == [CYCLE + timedelta(hours=h) for h in hours]
    assert len(result.fields) == 30 and result.field_warnings == []
    # No spot uses global.0p25: per map hour one range with exactly HTSGW, PERPW, DIRPW.
    for h in hours:
        url = PATHS.wave(CYCLE, "global.0p25", h)
        ranges = [r.headers["Range"] for r in gets(world, url)]
        at = offsets(world, url)
        assert ranges == [f"bytes={at['HTSGW:surface']}-{at['WVHGT:surface'] - 1}"]
    # Hours for the fields only: PRMSL and UGRD/VGRD, never GUST or LAND.
    url = PATHS.atmos(CYCLE, 36)
    at = offsets(world, url)
    assert sorted(r.headers["Range"] for r in gets(world, url)) == sorted(
        [
            f"bytes=0-{at['CLMR:1 hybrid level'] - 1}",
            f"bytes={at['UGRD:10 m above ground']}-{at['ICEG:10 m above mean sea level'] - 1}",
        ]
    )
    assert [s.fhour for s in p.last_field_steps] == [36, 48]
    assert result.request_count == len(world.paths_requested("GET"))
    assert "+ 2 hours for map fields only" in result.warnings[0]
    assert result.warnings[2].startswith("map fields: 30 fields, 5 of 5 map hours complete")

    # Synthetic values survive regridding and quantisation (within half a step).
    t = TargetGrid(0.5)
    f000 = by_variable(result.fields, CYCLE)
    node = t.node(58.5, -3.5)  # inside the synthetic global.0p25 band (5W..2W)
    assert f000["hs"][node] == pytest.approx(2.0, abs=0.05)
    assert f000["tp"][node] == pytest.approx(14.0, abs=0.05)
    assert f000["wave_dir"][node] == pytest.approx(280.0, abs=0.75)  # DIRPW: degrees FROM
    assert np.isnan(f000["hs"][t.node(58.5, 20.0)])  # outside the synthetic wave band
    sea = t.node(-40.0, 100.0)
    assert f000["wind_u"][sea] == 3.0 and f000["wind_v"][sea] == 4.0
    assert np.allclose(f000["pressure"], 1013.0)  # 101325 Pa = 1013.25 hPa, 1 hPa steps
    f048 = by_variable(result.fields, CYCLE + timedelta(hours=48))
    assert f048["hs"][node] == pytest.approx(2.5, abs=0.05)  # HTSGW + fhour / 100 = 2.48


def test_a_spot_on_the_global_grid_shares_its_records_with_the_map() -> None:
    world = hawaii_break_on_land()
    result = field_provider(world).fetch([NORTH], RUN)
    baseline = provider(hawaii_break_on_land()).fetch([NORTH], RUN)
    assert result.records == baseline.records
    assert result.request_count == baseline.request_count  # nothing extra downloaded
    assert len(complete_times(result.fields)) == 3
    for h in HOURS:  # one range: the spot's 15 wave records, which include the map's three
        assert len(gets(world, PATHS.wave(CYCLE, "global.0p25", h))) == 1


def test_a_failed_map_field_is_a_warning_never_a_spot_error() -> None:
    world = hawaii_break_on_land()
    world.fail_url(PATHS.wave(CYCLE, "global.0p25", 12) + ".idx", 404)
    world.add_field_hours(CYCLE, [36])
    world.fail_url(PATHS.atmos(CYCLE, 36), 503)  # still failing after the retries
    result = field_provider(world, map_lead=36).fetch([HAWAII], RUN)

    assert result.errors == {} and len(result.records[1]) == 3
    assert not any("forecast hours incomplete" in w for w in result.warnings)
    assert complete_times(result.fields) == [CYCLE, CYCLE + timedelta(hours=24)]
    warnings = result.field_warnings
    assert any(w.startswith("f012 map fields wave global.0p25:") and "404" in w for w in warnings)
    assert any(w.startswith("f036 map fields atmosphere:") and "503" in w for w in warnings)
    assert "f012 map fields incomplete: missing hs, tp, wave_dir" in warnings
    assert "f036 map fields incomplete: missing wind_u, wind_v, pressure" in warnings


def test_a_spot_file_failure_is_reported_for_the_map_fields_too() -> None:
    world = hawaii_break_on_land()
    world.fail_url(PATHS.atmos(CYCLE, 24), 503)
    result = field_provider(world).fetch([HAWAII], RUN)
    # The spot keeps today's behaviour (wind gap, run PARTIAL) ...
    assert result.errors[1] == "incomplete: wind/pressure missing at 1 of 3 forecast hours f024"
    # ... and the map fields say why f024 lacks wind and pressure.
    assert any(
        w.startswith("f024 map fields atmosphere:") and "503" in w for w in result.field_warnings
    )
    assert "f024 map fields incomplete: missing wind_u, wind_v, pressure" in result.field_warnings


def test_a_hot_cell_lands_on_its_node_through_grib() -> None:
    g = WAVE_GRID_GEOMETRY["global.0p25"]
    lat, lon = g.mesh()
    hot = np.isclose(lat, 58.5) & np.isclose(lon, -3.5)
    world = SyntheticGfs()
    world.add_atmos(CYCLE, 0)
    world.add_wave(CYCLE, "global.0p25", 0, arrays={"HTSGW:surface": np.where(hot, 9.0, 1.0)})
    p = fields_only(world, map_lead=0)
    hs = by_variable(p.fetch([], p.run_for_cycle(CYCLE)).fields, CYCLE)["hs"]
    t = TargetGrid(0.5)
    # Box weights 1 (centre), 1/2 (edges), 1/4 (corners), total 4: (9 + 3 x 1) / 4 = 3.
    assert hs[t.node(58.5, -3.5)] == pytest.approx(3.0)
    assert hs[t.node(58.5, -3.0)] == pytest.approx(1.0)
    assert hs[t.node(59.0, -3.5)] == pytest.approx(1.0)
    assert np.nanmax(hs) == pytest.approx(3.0)
    columns = np.flatnonzero(~np.isnan(hs).all(axis=0))
    assert t.lons()[columns].tolist() == [-5.0, -4.5, -4.0, -3.5, -3.0, -2.5, -2.0]


def _publish(world: SyntheticGfs, cycle: datetime, fhour: int, grids: tuple[str, ...]) -> None:
    for url in [PATHS.atmos(cycle, fhour)] + [PATHS.wave(cycle, g, fhour) for g in grids]:
        world.files[httpx.URL(url).path + ".idx"] = b""


def test_current_run_waits_for_the_last_map_hour() -> None:
    now = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)
    all_grids = ("global.0p16", "gsouth.0p25", "global.0p25")
    world = SyntheticGfs()
    _publish(world, CYCLE, 24, all_grids)  # 18Z is complete for the spots (f024) ...
    _publish(world, CYCLE - timedelta(hours=6), 48, all_grids)  # ... 12Z also to f048
    assert provider(world).current_run(now).run_key == "noaa-gfs:2026101018"
    assert field_provider(world, map_lead=48).current_run(now).run_key == "noaa-gfs:2026101012"


def test_fields_provider_source_and_run_discovery() -> None:
    world = SyntheticGfs()
    _publish(world, CYCLE, 24, ("global.0p25",))  # only what the fields need
    p = fields_only(world)
    info = p.source_info()
    assert (info.code, info.kind, info.family) == ("noaa_gfs_fields", "fields", "ncep_gfs")
    assert info.name == "NOAA GFS-Wave + GFS global fields" and not info.is_demo
    assert info.attribution == NoaaGfsProvider(settings()).source_info().attribution
    assert len(info.name) <= 120 and len(info.wave_model or "") <= 60

    run = p.current_run(datetime(2026, 10, 10, 23, 40, tzinfo=UTC))
    assert run.run_key == "noaa-gfs-fields:2026101018" and run.issued_at == CYCLE
    heads = world.paths_requested("HEAD")
    assert len(heads) == 2 and all("f024" in h for h in heads)
    assert any("global.0p25" in h for h in heads) and any("atmos" in h for h in heads)
    assert p.run_for_cycle(CYCLE) == run
    with pytest.raises(PermanentProviderError):
        p.run_for_cycle(CYCLE + timedelta(hours=3))


def test_fields_provider_reads_only_the_six_field_records() -> None:
    world = SyntheticGfs()
    world.add_field_hours(CYCLE, HOURS)
    p = fields_only(world)
    result = p.fetch([HAWAII], p.run_for_cycle(CYCLE))  # spots are ignored
    assert result.records == {} and result.errors == {}
    assert len(result.fields) == 18 and len(complete_times(result.fields)) == 3
    # Per hour: atmosphere index + PRMSL + UGRD/VGRD, wave index + one range.
    assert result.request_count == len(world.paths_requested("GET")) == 15
    assert sum(p.last_step_bytes.values()) == p.last_bytes_downloaded > 0
    assert all(s.seconds > 0 for s in p.last_field_steps)
    assert result.warnings[0].startswith("downloaded ")
    assert result.warnings[1].startswith("map fields: 18 fields, 3 of 3 map hours complete")


def test_fields_provider_fails_when_nothing_can_be_read() -> None:
    p = fields_only(SyntheticGfs())
    with pytest.raises(TransientProviderError, match="no NOAA GFS file could be read for any map"):
        p.fetch([], p.run_for_cycle(CYCLE))
