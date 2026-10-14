"""GRIB2 index parsing, byte-range fetching, decoding and land-aware point sampling."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import numpy as np
import pytest

from app.services.forecasts.grib import (
    GribError,
    GribFetcher,
    GridField,
    NotPublishedError,
    PointSampler,
    RangeNotHonouredError,
    decode_message,
    haversine_km,
    load_eccodes,
    nearest_valid_cell,
    parse_ecmwf_index,
    parse_ncep_idx,
    select_entries,
)

NCEP_IDX = """\
1:0:d=2026101000:WIND:surface:24 hour fcst:
2:100:d=2026101000:WDIR:surface:24 hour fcst:
3:250:d=2026101000:HTSGW:surface:24 hour fcst:
4:400:d=2026101000:SWELL:1 in sequence:24 hour fcst:
5:520:d=2026101000:SWELL:2 in sequence:24 hour fcst:
"""


def test_parse_ncep_idx_lengths_and_keys() -> None:
    entries = parse_ncep_idx(NCEP_IDX)
    assert [e.length for e in entries] == [100, 150, 150, 120, None]
    assert entries[2].key == "HTSGW:surface"
    assert entries[3].key == "SWELL:1 in sequence"
    assert entries[2].range_header() == "bytes=250-399"
    assert entries[4].range_header() == "bytes=520-"
    picked = select_entries(entries, ["HTSGW:surface", "SWELL:2 in sequence", "SWELL", "NOPE"])
    assert picked["SWELL"].level == "1 in sequence"  # bare variable: first match
    assert "NOPE" not in picked


def test_parse_ecmwf_index() -> None:
    lines = [
        {"param": "swh", "levtype": "sfc", "step": "24", "_offset": 0, "_length": 10},
        {"param": "pp1d", "levtype": "sfc", "step": "24", "_offset": 10, "_length": 5},
    ]
    entries = parse_ecmwf_index("\n".join(json.dumps(x) for x in lines) + "\n")
    assert [(e.variable, e.offset, e.length) for e in entries] == [("swh", 0, 10), ("pp1d", 10, 5)]
    assert entries[0].meta["step"] == "24"


def _ranged_transport(blob: bytes, seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing.grib2"):
            return httpx.Response(404)
        rng = request.headers.get("Range", "")
        seen.append(rng)
        start_s, end_s = rng.removeprefix("bytes=").split("-")
        start = int(start_s)
        end = int(end_s) if end_s else len(blob) - 1
        return httpx.Response(206, content=blob[start : end + 1])

    return httpx.MockTransport(handler)


def test_fetcher_merges_adjacent_ranges() -> None:
    blob = bytes(range(256)) * 3
    seen: list[str] = []
    fetcher = GribFetcher(
        httpx.Client(transport=_ranged_transport(blob, seen)), "test", backoff_seconds=0
    )
    entries = parse_ncep_idx(NCEP_IDX)
    wanted = [entries[0], entries[1], entries[3], entries[4]]  # 0-249 contiguous, 400-end
    parts = fetcher.ranges("https://x/file.grib2", wanted)
    assert seen == ["bytes=0-249", "bytes=400-"]
    assert parts[0] == blob[0:100] and parts[1] == blob[100:250]
    assert parts[2] == blob[400:520] and parts[3] == blob[520:]
    assert fetcher.request_count == 2


def test_fetcher_reports_unpublished_files() -> None:
    fetcher = GribFetcher(
        httpx.Client(transport=_ranged_transport(b"", [])), "test", backoff_seconds=0
    )
    with pytest.raises(NotPublishedError):
        fetcher.text("https://x/missing.grib2")


class _WholeFile(httpx.SyncByteStream):
    """A SYNTHETIC whole-file body that records whether anyone read it."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.read = False

    def __iter__(self) -> Iterator[bytes]:
        self.read = True
        yield bytes(self.size)


def _range_ignoring_client(
    body: _WholeFile, *hooks: Callable[[httpx.Response], None]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(body.size)}, stream=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    if hooks:
        client.event_hooks = {"response": list(hooks)}
    return client


def test_a_200_to_a_range_request_is_refused_unread() -> None:
    """Audit DA-12: a server ignoring Range sends the whole file; it is never downloaded."""
    from app.services.http import HttpTally

    body = _WholeFile(5_000_000)
    tally = HttpTally()  # installed before the fetcher, as the ECMWF provider does
    fetcher = GribFetcher(_range_ignoring_client(body, tally), "test", backoff_seconds=0)
    entries = parse_ncep_idx(NCEP_IDX)
    with pytest.raises(RangeNotHonouredError, match="HTTP 200"):
        fetcher.ranges("https://x/file.grib2", [entries[2]])  # bytes=250-399
    assert isinstance(RangeNotHonouredError("x"), GribError)
    assert body.read is False and tally.snapshot()[1] == 0 and fetcher.bytes_downloaded == 0


def test_a_200_to_a_bounded_range_at_offset_zero_is_refused_too() -> None:
    body = _WholeFile(5_000_000)
    fetcher = GribFetcher(_range_ignoring_client(body), "test", backoff_seconds=0)
    with pytest.raises(RangeNotHonouredError):
        fetcher.ranges("https://x/file.grib2", [parse_ncep_idx(NCEP_IDX)[0]])  # bytes=0-99
    assert body.read is False


def test_a_200_is_accepted_when_the_range_is_the_whole_file() -> None:
    body = _WholeFile(100)
    fetcher = GribFetcher(_range_ignoring_client(body), "test", backoff_seconds=0)
    parts = fetcher.ranges("https://x/file.grib2", [parse_ncep_idx(NCEP_IDX)[0]])  # bytes=0-99
    assert parts == [bytes(100)] and body.read is True


def _grib_message(values: np.ndarray, lat0: float, lon0: float, step: float) -> bytes:
    eccodes = load_eccodes()
    nj, ni = values.shape
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "Ni", ni)
        eccodes.codes_set(h, "Nj", nj)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", lat0)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon0)
        eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", lat0 - (nj - 1) * step)
        eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", lon0 + (ni - 1) * step)
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", step)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", step)
        eccodes.codes_set(h, "jScansPositively", 0)
        eccodes.codes_set(h, "bitmapPresent", 1)
        eccodes.codes_set(h, "missingValue", 9999)
        eccodes.codes_set_values(h, np.where(np.isnan(values), 9999, values).ravel())
        return bytes(eccodes.codes_get_message(h))
    finally:
        eccodes.codes_release(h)


def test_decode_message_round_trip_with_missing_land() -> None:
    vals = np.arange(12, dtype="float64").reshape(3, 4) / 4.0
    vals[1, 1] = np.nan  # land
    grid = decode_message(_grib_message(vals, 10.0, 350.0, 1.0))
    assert grid.shape == (3, 4)
    assert grid.lat0 == 10.0 and grid.dlat == -1.0 and grid.lon0 == 350.0
    assert np.isnan(grid.values[1, 1])
    np.testing.assert_allclose(grid.values[0], vals[0], atol=1e-3)
    assert grid.value(1, 1) is None


def _grid(values: np.ndarray, lat0: float = 10.0, lon0: float = 0.0, d: float = 1.0) -> GridField:
    return GridField("swh", values, lat0, lon0, -d, d)


def test_nearest_valid_cell_skips_land_and_respects_distance() -> None:
    vals = np.ones((5, 5))
    vals[2, 2] = np.nan  # the point's own cell is land
    grid = _grid(vals)
    choice = nearest_valid_cell(grid, 8.0, 2.0, max_distance_km=200)
    assert choice is not None and (choice.j, choice.i) != (2, 2)
    assert choice.distance_km == pytest.approx(
        haversine_km(8.0, 2.0, choice.latitude, choice.longitude)
    )
    assert nearest_valid_cell(grid, 8.0, 2.0, max_distance_km=50) is None  # nearest sea ~110 km


def test_nearest_valid_cell_wraps_longitude() -> None:
    vals = np.full((3, 360), np.nan)
    vals[1, 0] = 2.0  # lon 0
    vals[1, 359] = 3.0  # lon 359 (= -1)
    grid = _grid(vals, lat0=1.0)
    choice = nearest_valid_cell(grid, 0.0, -0.8, max_distance_km=100)
    assert choice is not None and choice.i == 359 and choice.longitude == pytest.approx(-1.0)


def test_point_sampler_caches_per_geometry_and_mask() -> None:
    grid = _grid(np.ones((5, 5)))
    sampler = PointSampler(max_distance_km=500)
    a = sampler.cell(grid, "spot", 8.0, 2.0)
    mask = np.zeros((5, 5), dtype=bool)
    mask[0, 0] = True
    b = sampler.cell(grid, "spot", 8.0, 2.0, valid=mask, mask_name="sea")
    assert a is not None and b is not None and (b.j, b.i) == (0, 0) != (a.j, a.i)
    assert sampler.sample(grid, a) == 1.0
    assert sampler.sample(grid, None) is None


def test_points_in_the_last_half_column_of_a_global_grid_wrap_to_column_0() -> None:
    # Review finding: GridField.contains() rejected fractional column indexes in
    # (ni - 0.5, ni) even on a grid that wraps, so on the NOAA/GEFS grids (first longitude
    # 0E) every point between 0.125W and 0 (0.25 deg grids) got no cell at all.
    grid = _grid(np.ones((5, 360)), lat0=52.0)  # 1 deg, global from 0E
    assert grid.is_global_in_longitude
    for lon in (-0.3, -0.01, 359.7):
        assert grid.contains(50.0, lon)
        choice = nearest_valid_cell(grid, 50.0, lon, max_distance_km=60)
        assert choice is not None and (choice.j, choice.i) == (2, 0), lon
        assert choice.longitude == 0.0
        assert choice.distance_km == pytest.approx(haversine_km(50.0, lon, 50.0, 0.0))
    # Grids starting at -180 (ECMWF; decoded lon0 = 180): 179.8E wraps to the -180 column.
    ecmwf = GridField("swh", np.ones((5, 360)), 52.0, 180.0, -1.0, 1.0)
    choice = nearest_valid_cell(ecmwf, 50.0, 179.8, max_distance_km=60)
    assert choice is not None and choice.i == 0 and choice.longitude == -180.0
    # A grid that is not global still ends half a cell beyond its outer columns.
    regional = _grid(np.ones((5, 5)), lat0=52.0, lon0=350.0)  # 10W..6W
    assert regional.contains(50.0, -10.4) and not regional.contains(50.0, -10.6)
    assert regional.contains(50.0, -5.6) and not regional.contains(50.0, -5.4)


def test_decode_message_raises_grib_error_for_a_damaged_data_section() -> None:
    good = _grib_message(np.ones((3, 4)), 10.0, 350.0, 1.0)
    # Header intact, the rest zeroed: ecCodes parses the message but codes_get_values
    # raises its own KeyValueNotFoundError, which used to escape every caller's isolation.
    damaged = good[:60] + bytes(len(good) - 64) + b"7777"
    with pytest.raises(GribError, match="cannot decode GRIB message"):
        decode_message(damaged)


def test_parse_ncep_idx_rejects_a_malformed_offset() -> None:
    text = "1:0:d=2026101100:HTSGW:surface:anl:\n2:12x:d=2026101100:PERPW:surface:anl:\n"
    with pytest.raises(GribError, match="invalid NCEP index line 2"):
        parse_ncep_idx(text)
