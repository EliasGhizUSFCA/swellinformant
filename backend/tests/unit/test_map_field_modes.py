"""Map-field modes, hours, cycles, the 0.5 deg atmosphere and the per-run byte budget.

Download costs are computed from the REAL .idx files in tests/fixtures/idx (GFS and
GFS-Wave 2026-10-13 12Z, see its README; offsets only, no GRIB data). Everything else uses
SYNTHETIC grids and files (tests.synthetic_gfs, tests.synthetic_map_fields).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.core.config import Settings
from app.services.forecasts.base import RunInfo, SpotPoint
from app.services.forecasts.fields import (
    EncodedField,
    TargetGrid,
    complete_times,
    encode_field,
    regrid_mean,
)
from app.services.forecasts.grib import parse_ncep_idx, select_entries
from app.services.forecasts.noaa import (
    FIELD_ATMOS_RECORDS,
    FIELD_PARTITION_RECORDS,
    FIELD_WAVE_RECORDS,
    NoaaGfsFieldsProvider,
    NoaaGfsProvider,
)
from app.services.http import PermanentProviderError
from app.services.map_fields import is_map_cycle, map_field_hours, map_fields_mode
from tests.synthetic_gfs import PATHS, SyntheticGfs
from tests.synthetic_map_fields import add_map_hours, field_settings, map_atmos_url

IDX = Path(__file__).resolve().parents[1] / "fixtures" / "idx"
CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # nominal cycle with the 5 h lag: 18Z
MB = 1_000_000


# --------------------------------------------------------------------------- modes
@pytest.mark.parametrize(
    ("providers", "enabled", "mode"),
    [
        ("open_meteo", None, "standalone"),
        ("open_meteo,open_meteo_ecmwf", None, "standalone"),
        ("open_meteo", True, "standalone"),
        ("open_meteo", False, "off"),
        ("noaa_gfs", None, "with_noaa"),
        ("noaa_gfs", False, "off"),
        ("demo", None, "off"),
        ("demo", True, "standalone"),
    ],
)
def test_standalone_is_automatic_without_a_grib_spot_provider(
    providers: str, enabled: bool | None, mode: str
) -> None:
    s = Settings(forecast_providers=providers, map_fields_enabled=enabled)
    assert map_fields_mode(s) == mode


# --------------------------------------------------------------------------- hours, cycles
def test_default_map_hours_are_3_hourly_then_6_hourly() -> None:
    hours = map_field_hours(Settings())
    assert len(hours) == 61
    assert hours == list(range(0, 121, 3)) + list(range(126, 241, 6))
    assert Settings().map_field_step_ranges == [(0, 120, 3), (120, 240, 6)]


def test_only_map_cycles_are_read() -> None:
    s = Settings()
    assert s.map_field_cycle_hours == [0, 12]
    cycles = [datetime(2026, 10, 10, h, tzinfo=UTC) for h in (0, 6, 12, 18)]
    assert [c.hour for c in cycles if is_map_cycle(s, c)] == [0, 12]


# --------------------------------------------------------------------------- 0.5 deg atmosphere
def test_a_0p50_source_keeps_the_target_geometry_and_values() -> None:
    # pgrb2.0p50 layout: 361 x 720 cells, 90N to 90S, 0E eastwards (synthetic values).
    values = np.random.default_rng(5).uniform(-20.0, 20.0, (361, 720))

    class Src:
        def __init__(self) -> None:
            self.values, self.lat0, self.lon0, self.dlat, self.dlon = values, 90.0, 0.0, -0.5, 0.5

    t = TargetGrid(0.5)
    out = regrid_mean(Src(), t)
    # Each node takes exactly the cell on it; the target starts at 180W (source column 360).
    assert np.allclose(out, np.roll(values, -360, axis=1))
    field = encode_field("wind_u", CYCLE, Src(), t)
    geometry = (field.nx, field.ny, field.lat0, field.lon0, field.dlat, field.dlon)
    assert geometry == (720, 361, 90.0, -180.0, 0.5, 0.5)
    assert np.nanmax(np.abs(field.values() - np.roll(values, -360, axis=1))) <= 0.25 + 1e-9


def test_map_atmosphere_path_and_fallback() -> None:
    s = Settings()
    assert s.map_field_atmos_path("20261013", "12", 6) == (
        "gfs.20261013/12/atmos/gfs.t12z.pgrb2.0p50.f006"
    )
    urls = PATHS.map_atmos(CYCLE, 6)
    assert [u.rsplit("/", 1)[1] for u in urls] == [
        "gfs.t18z.pgrb2.0p50.f006",
        "gfs.t18z.pgrb2.0p25.f006",
    ]
    same = PATHS.__class__(PATHS.base_url, map_atmos_grid="0p25").map_atmos(CYCLE, 6)
    assert len(same) == 1  # 0p25 has no other fallback


# --------------------------------------------------------------------------- costs (real .idx)
def _record_bytes(name: str, keys: list[str]) -> int:
    entries = select_entries(parse_ncep_idx((IDX / name).read_text()), keys)
    assert set(entries) == set(keys), name
    lengths = [entries[k].length for k in keys]
    assert all(n is not None for n in lengths)
    return sum(n for n in lengths if n is not None)


@pytest.mark.parametrize("fhour", ["006", "120", "240"])
def test_bytes_per_base_map_hour_from_the_recorded_indexes(fhour: str) -> None:
    atmos_idx = (IDX / f"gfs.t12z.pgrb2.0p50.f{fhour}.idx").stat().st_size
    wave_idx = (IDX / f"gfswave.t12z.global.0p25.f{fhour}.grib2.idx").stat().st_size
    atmos = _record_bytes(f"gfs.t12z.pgrb2.0p50.f{fhour}.idx", list(FIELD_ATMOS_RECORDS))
    waves = _record_bytes(f"gfswave.t12z.global.0p25.f{fhour}.grib2.idx", list(FIELD_WAVE_RECORDS))
    parts = _record_bytes(
        f"gfswave.t12z.global.0p25.f{fhour}.grib2.idx", list(FIELD_PARTITION_RECORDS)
    )
    base = atmos_idx + atmos + wave_idx + waves
    assert base <= 2.8 * MB  # the acceptance limit for a base map hour
    assert 0.80 * MB <= atmos <= 0.95 * MB  # U/V 10 m + PRMSL from pgrb2.0p50
    # The same three records from pgrb2.0p25 cost about 3.4 times as much.
    old = _record_bytes(f"gfs.t12z.pgrb2.0p25.f{fhour}.idx", list(FIELD_ATMOS_RECORDS))
    assert old > 3 * atmos
    assert 5.0 * MB <= parts <= 5.4 * MB  # the nine partition records
    # A whole default cycle (61 map hours, partitions on) fits MAP_FIELD_MAX_BYTES_PER_RUN.
    assert 61 * (base + parts) <= Settings().map_field_max_bytes_per_run


# --------------------------------------------------------------------------- run discovery
def _publish(world: SyntheticGfs, cycle: datetime, fhour: int, *, atmos: str = "0p50") -> None:
    first = map_atmos_url(cycle, fhour) if atmos == "0p50" else PATHS.atmos(cycle, fhour)
    for url in (first, PATHS.wave(cycle, "global.0p25", fhour)):
        world.files[httpx.URL(url).path + ".idx"] = b""


def fields_only(world: SyntheticGfs, **kw: object) -> NoaaGfsFieldsProvider:
    s = field_settings(providers="demo", map_fields_enabled=True, **kw)  # type: ignore[arg-type]
    return NoaaGfsFieldsProvider(s, client=world.client(), backoff_seconds=0)


def test_current_run_skips_cycles_that_are_not_map_cycles() -> None:
    world = SyntheticGfs()
    _publish(world, CYCLE, 24)  # 18Z is complete, but not a map cycle
    _publish(world, CYCLE - timedelta(hours=6), 24)  # 12Z
    run = fields_only(world, cycles="00,12").current_run(NOW)
    assert run.run_key == "noaa-gfs-fields:2026101012"
    heads = world.paths_requested("HEAD")
    assert len(heads) == 2 and all("/12/" in h for h in heads)  # nothing asked about 18Z


def test_current_run_accepts_the_0p25_atmosphere_when_0p50_is_missing() -> None:
    world = SyntheticGfs()
    _publish(world, CYCLE, 24, atmos="0p25")
    run = fields_only(world).current_run(NOW)
    assert run.run_key == "noaa-gfs-fields:2026101018"
    heads = world.paths_requested("HEAD")
    assert [h.rsplit("/", 1)[1] for h in heads] == [
        "gfs.t18z.pgrb2.0p50.f024.idx",
        "gfs.t18z.pgrb2.0p25.f024.idx",
        "gfswave.t18z.global.0p25.f024.grib2.idx",
    ]


def test_an_hour_without_its_0p50_file_falls_back_to_0p25() -> None:
    world = SyntheticGfs()
    add_map_hours(world, CYCLE, [0])
    world.add_atmos(CYCLE, 12)  # only the spots' pgrb2.0p25 file
    world.add_wave(CYCLE, "global.0p25", 12)
    p = fields_only(world, map_lead=12)
    result = p.fetch([], p.run_for_cycle(CYCLE))
    assert complete_times(result.fields) == [CYCLE, CYCLE + timedelta(hours=12)]
    assert result.field_warnings == [
        "f012: gfs.20261010/18/atmos/gfs.t18z.pgrb2.0p50.f012 not published; read "
        "gfs.20261010/18/atmos/gfs.t18z.pgrb2.0p25.f012 instead"
    ]


def test_a_spot_run_of_another_cycle_stores_no_map_fields() -> None:
    world = SyntheticGfs()
    world.add_cycle(CYCLE, [0, 12, 24])
    s = field_settings(cycles="00,12")
    provider = NoaaGfsProvider(s, client=world.client(), backoff_seconds=0)
    assert provider.map_fields  # the "with_noaa" mode
    run = RunInfo("noaa-gfs:2026101018", CYCLE, CYCLE, CYCLE, "noaa-s3-idx")
    hawaii = SpotPoint(1, "hawaii-test", 21.66, -158.05, 21.80, -158.18)  # synthetic spot
    result = provider.fetch([hawaii], run)
    assert result.records and result.fields == [] and result.field_warnings == []
    assert not any("global.0p25" in path for path in world.paths_requested())


# --------------------------------------------------------------------------- byte budget
def _hour_bytes(map_lead: int = 24) -> dict[int, int]:
    world = SyntheticGfs()
    add_map_hours(world, CYCLE, [0, 12, 24])
    p = fields_only(world, map_lead=map_lead, grib_download_workers=1)
    p.fetch([], p.run_for_cycle(CYCLE))
    return dict(p.last_step_bytes)


def test_the_byte_budget_stops_the_run() -> None:
    per_hour = _hour_bytes()
    budget = per_hour[0] + per_hour[12] // 3  # f000 fits, f012 does not
    world = SyntheticGfs()
    add_map_hours(world, CYCLE, [0, 12, 24])
    p = fields_only(world, max_bytes=budget, grib_download_workers=1)
    result = p.fetch([], p.run_for_cycle(CYCLE))
    assert complete_times(result.fields) == [CYCLE]
    assert result.stats is not None and result.stats.bytes_downloaded <= budget
    assert result.stats.bytes_downloaded == p.last_bytes_downloaded
    stop = next(w for w in result.field_warnings if "MAP_FIELD_MAX_BYTES_PER_RUN" in w)
    assert stop.startswith("f012: map fields stopped;") and "later map hours are not read" in stop
    assert not any("f024" in path for path in world.paths_requested())  # never asked for
    assert "f024 map fields incomplete: missing hs" in " ".join(result.field_warnings)


def test_a_budget_too_small_for_one_hour_fails_for_good() -> None:
    world = SyntheticGfs()
    add_map_hours(world, CYCLE, [0])
    p = fields_only(world, map_lead=0, max_bytes=1)
    with pytest.raises(PermanentProviderError, match="MAP_FIELD_MAX_BYTES_PER_RUN"):
        p.fetch([], p.run_for_cycle(CYCLE))


def test_the_budget_drops_the_partitions_before_the_core() -> None:
    def run(
        max_bytes: int | None, partitions: str
    ) -> tuple[NoaaGfsFieldsProvider, list[EncodedField]]:
        world = SyntheticGfs()
        add_map_hours(world, CYCLE, [0, 12])
        p = fields_only(
            world,
            map_lead=12,
            partitions=partitions,
            grib_download_workers=1,
            max_bytes=max_bytes or 600 * MB,
        )
        return p, p.fetch([], p.run_for_cycle(CYCLE)).fields

    full, _ = run(None, "on")
    core, _ = run(None, "off")
    # f000 complete with partitions, f012's core, and one byte short of f012's partitions.
    budget = full.last_step_bytes[0] + core.last_step_bytes[12]
    budget += full.last_step_bytes[12] - core.last_step_bytes[12] - 1
    p, fields = run(budget, "on")
    assert complete_times(fields) == [CYCLE, CYCLE + timedelta(hours=12)]
    names = {(f.valid_time, f.variable) for f in fields}
    assert (CYCLE, "swell1_dir") in names and (
        CYCLE + timedelta(hours=12),
        "swell1_dir",
    ) not in names
    assert p.last_bytes_downloaded <= budget
