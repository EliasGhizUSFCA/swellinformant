"""Map-field regridding, quantisation and compression (app.services.forecasts.fields).

All arrays here are SYNTHETIC test patterns (linear ramps, single hot cells, constants),
not model output.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.core.config import Settings
from app.services.forecasts.fields import (
    FIELD_VARIABLES,
    NODATA,
    QUANTISATION,
    EncodedField,
    FieldSink,
    TargetGrid,
    complete_times,
    decode_bytes,
    encode_field,
    regrid_mean,
    summarise,
)
from app.services.forecasts.noaa import map_field_hours
from app.services.map_fields import map_fields_mode

VALID = datetime(2026, 10, 10, 18, tzinfo=UTC)
T = TargetGrid(0.5)


@dataclass
class Src:
    """A synthetic source grid (GridLike)."""

    values: np.ndarray
    lat0: float
    lon0: float
    dlat: float
    dlon: float


def gfs_like(fill: float = 0.0) -> Src:
    """Shape and geometry of the GFS 0.25 deg global grids: 90N..90S, 0E..359.75E."""
    return Src(np.full((721, 1440), fill), 90.0, 0.0, -0.25, 0.25)


def mesh(src: Src) -> tuple[np.ndarray, np.ndarray]:
    nj, ni = src.values.shape
    lats = src.lat0 + src.dlat * np.arange(nj)
    lons = (src.lon0 + src.dlon * np.arange(ni)) % 360.0
    lon2, lat2 = np.meshgrid(lons, lats)
    return lat2, np.where(lon2 >= 180.0, lon2 - 360.0, lon2)


def cell(src: Src, lat: float, lon: float) -> tuple[int, int]:
    j = round((lat - src.lat0) / src.dlat)
    i = round(((lon - src.lon0) % 360.0) / src.dlon)
    return j, i


# --------------------------------------------------------------------------- grid
def test_target_grid_geometry() -> None:
    assert (T.nx, T.ny, T.lat0, T.lon0) == (720, 361, 90.0, -180.0)
    assert T.lats()[0] == 90.0 and T.lats()[-1] == -90.0
    assert T.lons()[0] == -180.0 and T.lons()[-1] == 179.5
    assert T.node(-33.9, 25.2) == (248, 410)  # nearest node (-34.0, 25.0)
    assert T.node(10.0, 179.9) == (160, 0)  # wraps to 180W
    assert TargetGrid(0.25).nx == 1440 and TargetGrid(1.0).ny == 181
    with pytest.raises(ValueError, match="must divide 180"):
        TargetGrid(0.7)


# --------------------------------------------------------------------------- regridding
def test_a_single_hot_cell_lands_on_its_node() -> None:
    src = gfs_like()
    src.values[cell(src, 10.0, 20.0)] = 1.0
    out = regrid_mean(src, T)
    j, i = np.unravel_index(np.argmax(out), out.shape)
    assert (T.lats()[j], T.lons()[i]) == (10.0, 20.0)
    assert out[j, i] == pytest.approx(0.25)  # weight 1 of 4 (centre 1, edges 1/2, corners 1/4)
    assert np.count_nonzero(out) == 1


def test_a_cell_between_nodes_is_shared_symmetrically() -> None:
    src = gfs_like()
    src.values[cell(src, 10.25, 20.0)] = 1.0  # half way between the 10.0 and 10.5 rows
    out = regrid_mean(src, T)
    j10, j105 = T.node(10.0, 20.0)[0], T.node(10.5, 20.0)[0]
    i = T.node(10.0, 20.0)[1]
    assert out[j10, i] == pytest.approx(0.125) and out[j105, i] == pytest.approx(0.125)
    assert np.count_nonzero(out) == 2


def test_linear_ramps_are_reproduced_exactly_no_half_cell_shift() -> None:
    src = gfs_like()
    lat, lon = mesh(src)
    src.values = lat.copy()
    out = regrid_mean(src, T)
    assert np.allclose(out[1:-1], T.lats()[1:-1, None])  # every row except the poles
    src.values = lon.copy()
    out = regrid_mean(src, T)
    away = np.abs(T.lons()) < 179  # the ramp jumps at 180 (not a regridding error)
    assert np.allclose(out[:, away], T.lons()[None, away])


def test_longitudes_wrap_at_180_and_at_the_source_start() -> None:
    src = gfs_like()
    src.values[cell(src, 0.0, 180.0)] = 1.0  # source column 720 = target column 0 (180W)
    src.values[cell(src, 30.0, 359.75)] = 4.0  # last source column, next to 0E
    out = regrid_mean(src, T)
    assert out[T.node(0.0, -180.0)] == pytest.approx(0.25)
    # Shared between the 0E and 0.5W nodes (half its area in each box).
    assert out[T.node(30.0, 0.0)] == pytest.approx(4.0 * 0.5 / 4)
    assert out[T.node(30.0, -0.5)] == pytest.approx(4.0 * 0.5 / 4)
    assert np.count_nonzero(out) == 3


def test_source_rows_south_to_north_give_the_same_result() -> None:
    src = gfs_like()
    lat, lon = mesh(src)
    src.values = np.sin(np.radians(lat)) * np.cos(np.radians(lon))
    flipped = Src(src.values[::-1].copy(), -90.0, 0.0, 0.25, 0.25)
    assert np.allclose(regrid_mean(src, T), regrid_mean(flipped, T))


def test_poles_are_cut_and_stay_finite() -> None:
    src = gfs_like(1013.0)
    out = regrid_mean(src, T)
    assert out.shape == (361, 720)
    assert np.allclose(out, 1013.0)  # a constant field is constant everywhere, poles too
    lat, _ = mesh(src)
    src.values = lat.copy()
    out = regrid_mean(src, T)
    # The pole box holds the rows within d/2 = 0.25 deg of the pole, cut at the pole.
    assert np.allclose(out[0], (90.0 + 89.75) / 2)
    assert np.allclose(out[-1], (-90.0 - 89.75) / 2)


def test_nan_cells_are_skipped_and_empty_boxes_are_nodata() -> None:
    src = gfs_like(2.0)
    lat, lon = mesh(src)
    land = (lat > 40.0) & (lat < 50.0) & (lon > 0.0) & (lon < 10.0)
    src.values[land] = np.nan
    src.values[cell(src, 39.75, 5.0)] = 4.0  # a sea cell on the coast's southern edge
    out = regrid_mean(src, T)
    assert np.isnan(out[T.node(45.0, 5.0)])  # the whole box is land
    # Coastal node at 40N: only the sea cells count. Row weights: 39.75 = 1/2, 40.0 = 1
    # (sea, as land starts above 40N), 40.25 = land; column weights 1/2, 1, 1/2.
    expected = (4.0 * 0.5 * 1 + 2.0 * 0.5 * 0.5 * 2 + 2.0 * 1 * 2) / (0.5 * 2 + 2)
    assert out[T.node(40.0, 5.0)] == pytest.approx(expected)
    assert not np.isnan(out).all() and np.isnan(out).sum() > 0


def test_directions_are_averaged_as_unit_vectors() -> None:
    src = gfs_like(np.nan)
    j, i = cell(src, 0.0, 20.0)
    src.values[j, i] = 350.0  # on the node: weight 1
    src.values[j, i - 1] = src.values[j, i + 1] = 10.0  # edge neighbours: weight 1/2 each
    out = regrid_mean(src, T, directional=True)
    value = out[T.node(0.0, 20.0)]
    # 350 (weight 1) and 10 (weight 1/2 twice): the vector mean is 0 deg, never 116.7 or 180.
    assert min(value, 360.0 - value) == pytest.approx(0.0, abs=1e-9)
    assert np.nanmin(out) >= 0.0 and np.nanmax(out) < 360.0


def test_opposite_directions_cancel_to_nodata() -> None:
    src = gfs_like(np.nan)
    j, i = cell(src, 0.0, 20.0)
    src.values[j, i - 1], src.values[j, i + 1] = 90.0, 270.0  # equal weights, opposite
    out = regrid_mean(src, T, directional=True)
    assert np.isnan(out[T.node(0.0, 20.0)])


def test_non_global_source_only_fills_the_boxes_it_overlaps() -> None:
    src = Src(np.full((5, 5), 3.0), 1.0, 10.0, -0.5, 0.5)  # 1N..1S, 10E..12E
    out = regrid_mean(src, T)
    filled = np.argwhere(~np.isnan(out))
    lats, lons = T.lats()[filled[:, 0]], T.lons()[filled[:, 1]]
    assert lats.min() == -1.0 and lats.max() == 1.0
    assert lons.min() == 10.0 and lons.max() == 12.0
    assert np.allclose(out[~np.isnan(out)], 3.0)


def test_factor_converts_units_after_averaging() -> None:
    src = gfs_like(101325.0)
    assert np.allclose(regrid_mean(src, T, factor=0.01), 1013.25)


# --------------------------------------------------------------------------- quantisation
def test_quantisation_table_is_the_documented_one() -> None:
    table = {k: (q.unit, q.offset, q.scale) for k, q in QUANTISATION.items()}
    assert table == {
        "hs": ("m", 0.0, 0.1),
        "tp": ("s", 0.0, 0.1),
        "wave_dir": ("deg", 0.0, 1.5),
        "wind_u": ("m/s", -63.5, 0.5),
        "wind_v": ("m/s", -63.5, 0.5),
        "pressure": ("hPa", 860.0, 1.0),
    }
    assert FIELD_VARIABLES == ("hs", "tp", "wave_dir", "wind_u", "wind_v", "pressure")


@pytest.mark.parametrize(
    ("variable", "lo", "hi"),
    [
        ("hs", 0.0, 25.4),
        ("tp", 0.0, 25.4),
        ("wave_dir", 0.0, 359.99),
        ("wind_u", -63.5, 63.5),
        ("wind_v", -63.5, 63.5),
        ("pressure", 860.0, 1114.0),
    ],
)
def test_round_trip_within_half_a_step(variable: str, lo: float, hi: float) -> None:
    q = QUANTISATION[variable]
    values = np.random.default_rng(7).uniform(lo, hi, (361, 720))
    values[::17, ::13] = np.nan
    src = Src(values, 90.0, -180.0, -0.5, 0.5)  # already on the target grid: identity
    field = encode_field(variable, VALID, src, T)
    raw = zlib.decompress(field.data)
    assert len(raw) == 720 * 361
    decoded = decode_bytes(field.data, field.nx, field.ny, field.scale, field.offset)
    assert np.array_equal(np.isnan(decoded), np.isnan(values))
    diff = np.abs(decoded - values)
    if q.cyclic:
        diff = np.minimum(diff, 360.0 - diff)
    assert np.nanmax(diff) <= q.scale / 2 + 1e-9


def test_out_of_range_values_are_clamped_and_255_is_nodata() -> None:
    q = QUANTISATION["hs"]
    raw = q.encode(np.array([-1.0, 0.04, 25.4, 30.0, np.nan, np.inf]))
    assert raw.tolist() == [0, 0, 254, 254, NODATA, NODATA]
    p = QUANTISATION["pressure"].encode(np.array([850.0, 1013.25, 1200.0]))
    assert p.tolist() == [0, 153, 254]
    w = QUANTISATION["wind_u"].encode(np.array([-80.0, 0.0, 3.0, 80.0]))
    assert w.tolist() == [0, 127, 133, 254]


def test_directions_wrap_instead_of_clamping() -> None:
    raw = QUANTISATION["wave_dir"].encode(np.array([0.0, 359.5, 360.0, -1.5, 180.0, 721.5]))
    assert raw.tolist() == [0, 0, 0, 239, 120, 1]
    assert raw.max() < NODATA


def test_bytes_are_row_major_north_to_south_zlib_level_6() -> None:
    src = gfs_like(1.0)
    src.values[: 4 * 90 + 1] = 5.0  # northern hemisphere and the equator row: 5 m
    field = encode_field("hs", VALID, src, T)
    assert field.data[:2] == b"\x78\x9c"  # RFC 1950 header, default (level 6) compression
    raw = np.frombuffer(zlib.decompress(field.data), dtype=np.uint8).reshape(361, 720)
    assert raw[0, 0] == 50 and raw[179, 0] == 50  # row 0 is 90N
    assert raw[181, 0] == 10 and raw[-1, -1] == 10  # last row is 90S
    meta = (field.nx, field.ny, field.lat0, field.lon0, field.dlat, field.dlon)
    assert meta == (720, 361, 90.0, -180.0, 0.5, 0.5)
    assert (field.scale, field.offset, field.unit) == (0.1, 0.0, "m")


# --------------------------------------------------------------------------- sink
def _field(variable: str, when: datetime) -> EncodedField:
    return encode_field(variable, when, gfs_like(1.0), T)


def test_sink_keeps_wanted_hours_and_reports_incomplete_ones() -> None:
    sink = FieldSink(VALID, [0, 3, 6], 0.5)
    assert sink.wants(3) and not sink.wants(4)
    sink.put([sink.encode(0, v, gfs_like(1.0)) for v in FIELD_VARIABLES])
    sink.put([sink.encode(3, "hs", gfs_like(1.0)), None])
    sink.put([sink.encode(3, "hs", gfs_like(2.0))])  # same key: replaced, not duplicated
    assert len(sink.fields) == 7
    assert [f.variable for f in sink.fields][:6] == list(FIELD_VARIABLES)
    assert complete_times(sink.fields) == [VALID]
    assert np.nanmax(sink.fields[-1].values()) == pytest.approx(2.0)
    assert sink.warnings == [
        "f003 map fields incomplete: missing tp, wave_dir, wind_u, wind_v, pressure",
        "f006 map fields incomplete: missing hs, tp, wave_dir, wind_u, wind_v, pressure",
    ]


def test_sink_encoding_failure_is_a_warning_not_an_exception() -> None:
    sink = FieldSink(VALID, [0], 0.5)
    bad = Src(np.ones((3, 3)), 0.0, 0.0, 0.0, 0.5)  # zero latitude step
    assert sink.encode(0, "hs", bad) is None
    assert sink.warnings[0].startswith("f000 hs: could not encode (ValueError")


def test_summary_of_a_run() -> None:
    fields = [_field(v, VALID) for v in FIELD_VARIABLES] + [_field("hs", VALID + timedelta(3))]
    out = summarise(fields, ["w"])
    assert out["count"] == 7 and out["bytes"] == sum(len(f.data) for f in fields)
    assert out["valid_times"] == [VALID.isoformat()] and out["warnings"] == ["w"]


# --------------------------------------------------------------------------- settings
def test_map_hours_are_published_multiples_of_the_step() -> None:
    assert map_field_hours(12, 3) == [0, 3, 6, 9, 12]
    default = map_field_hours(240, 3)
    assert len(default) == 81 and default[-1] == 240
    assert map_field_hours(150, 4)[-3:] == [120, 132, 144]  # after f120: 3-hourly files only
    assert map_field_hours(999, 6)[-1] == 384


@pytest.mark.parametrize(
    ("providers", "enabled", "mode"),
    [
        ("noaa_gfs", None, "with_noaa"),
        ("noaa_gfs,ecmwf_ifs", True, "with_noaa"),
        ("noaa_gfs", False, "off"),
        ("demo", None, "off"),
        ("demo", True, "standalone"),
        ("open_meteo", True, "standalone"),
        ("open_meteo", False, "off"),
    ],
)
def test_map_fields_mode(providers: str, enabled: bool | None, mode: str) -> None:
    s = Settings(forecast_providers=providers, map_fields_enabled=enabled)
    assert map_fields_mode(s) == mode
