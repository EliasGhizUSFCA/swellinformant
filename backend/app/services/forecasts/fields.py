"""Map ocean layers: regrid, quantise and compress global model fields (pure numpy).

The map draws global layers (wave height, period and direction, 10 m wind, mean sea-level
pressure) in the browser from small byte grids. This module turns one decoded GRIB field
(a :class:`GridLike`, e.g. ``grib.GridField``) into the stored and served format described
in app/schemas/map_fields.py. It does no I/O; :class:`FieldSink` collects the fields while
``noaa.GfsPointReader`` reads a GFS cycle.

Target grid (:class:`TargetGrid`, ``d`` = MAP_FIELD_RESOLUTION_DEG, default 0.5): nodes at
``lat = 90 - j * d`` (row 0 = 90N, last row = 90S) and ``lon = -180 + i * d`` for
``i < 360 / d``, i.e. ``lat0 = 90``, ``lon0 = -180``, ``dlat = dlon = d``.

Regridding (:func:`regrid_mean`). The value at a node is the area-weighted mean of the
valid source cells inside the node's ``d x d`` box. A source cell is the ``s x s`` area
centred on its grid point; a cell that straddles the box edge counts with the fraction of
its area inside the box. The average is therefore centred exactly on the node. From the
0.25 deg GFS grids to 0.5 deg the weights are 1 for the cell on the node, 1/2 for its four
edge neighbours and 1/4 for the four corners. (Taking every cell with ``lat`` in
``[c - d/2, c + d/2)`` instead would centre the mean half a source cell, 0.125 deg, off
the node.) Assumptions and edge cases:

* NaN cells (land and ice in the wave bitmap, missing values) are left out. A node whose
  box holds no valid source cell is nodata; a coastal node averages only its sea cells.
* Longitudes: the source may start anywhere (GFS: 0E, 0..359.75). A source that is global
  in longitude wraps, so the box of the 180W node takes cells on both sides of 180.
* Poles: boxes and cells are cut at +-90 (nothing lies beyond a pole), so a pole row
  averages the source rows within ``d/2`` of the pole and its mean sits ``d/4`` from the
  pole in latitude. All longitudes meet there, so this is a sub-cell distance.
* Weights are plain latitude x longitude areas. Within one box cos(lat) varies by about
  ``d x tan(lat)`` (1.5 % at 60 deg for d = 0.5), which is ignored.
* Directions (degrees FROM) are averaged as unit vectors, never as plain numbers (350 and
  10 average to 0, not 180). Where the vectors cancel, the node is nodata.
* Vector components (10 m wind u, v) are averaged as numbers, which is correct for
  Cartesian components. The speed is computed by the client from the averaged u and v.

Quantisation (:data:`QUANTISATION`): ``value = offset + scale * byte``, byte 255 = nodata,
other bytes clamped to 0..254 (directions wrap instead: 360 deg = byte 0).

======== ====== ======== ======== =====================================
variable unit   offset   scale    representable range
======== ====== ======== ======== =====================================
hs       m      0        0.1      0 .. 25.4 m (offshore Hs, NOT surf)
tp       s      0        0.1      0 .. 25.4 s (peak period)
wave_dir deg    0        1.5      0 .. 358.5 deg FROM (bytes 0..239)
wind_u   m/s    -63.5    0.5      -63.5 .. 63.5 m/s (eastward)
wind_v   m/s    -63.5    0.5      -63.5 .. 63.5 m/s (northward)
pressure hPa    860      1.0      860 .. 1114 hPa
======== ====== ======== ======== =====================================

Bytes are row-major from north to south, then zlib (RFC 1950) compressed at level 6.
"""

from __future__ import annotations

import functools
import logging
import math
import threading
import time
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol

import numpy as np

logger = logging.getLogger(__name__)

NODATA = 255
MAX_BYTE = 254
ZLIB_LEVEL = 6
# Overlaps below this (in source-cell units) are floating-point noise, not a real overlap.
MIN_WEIGHT = 1e-9
# Unit vectors averaging to a resultant shorter than this (relative) have no direction.
MIN_RESULTANT = 1e-6


@dataclass(frozen=True)
class Quantisation:
    """Fixed byte encoding of one variable: value = offset + scale * byte."""

    unit: str
    offset: float
    scale: float
    cyclic: bool = False  # directions: wrap at 360 instead of clamping

    def encode(self, values: np.ndarray) -> np.ndarray:
        """uint8 bytes for ``values`` (NaN -> NODATA; out of range clamped to 0..254)."""
        out = np.full(values.shape, NODATA, dtype=np.uint8)
        ok = np.isfinite(values)
        if self.cyclic:
            steps = round(360.0 / self.scale)
            raw = np.rint(np.mod(values[ok], 360.0) / self.scale) % steps
        else:
            raw = np.clip(np.rint((values[ok] - self.offset) / self.scale), 0, MAX_BYTE)
        out[ok] = raw.astype(np.uint8)
        return out


# Keys and units match models.forecast_engine.MAP_FIELD_VARIABLES.
QUANTISATION: dict[str, Quantisation] = {
    "hs": Quantisation("m", 0.0, 0.1),
    "tp": Quantisation("s", 0.0, 0.1),
    "wave_dir": Quantisation("deg", 0.0, 1.5, cyclic=True),
    "wind_u": Quantisation("m/s", -63.5, 0.5),
    "wind_v": Quantisation("m/s", -63.5, 0.5),
    "pressure": Quantisation("hPa", 860.0, 1.0),
}
DIRECTIONAL = frozenset({"wave_dir"})
FIELD_VARIABLES = tuple(QUANTISATION)


class GridLike(Protocol):
    """A decoded field on a regular lat/lon grid (``grib.GridField`` satisfies this)."""

    values: np.ndarray  # (nj, ni); NaN = missing
    lat0: float  # latitude of row 0
    lon0: float  # longitude of column 0
    dlat: float  # signed latitude step between rows
    dlon: float  # longitude step between columns (> 0)


@dataclass(frozen=True)
class TargetGrid:
    """The served grid: rows from 90N to 90S, columns from 180W eastwards, ``d`` degrees."""

    d: float = 0.5
    lat0: ClassVar[float] = 90.0
    lon0: ClassVar[float] = -180.0

    def __post_init__(self) -> None:
        if self.d <= 0 or abs(180.0 / self.d - round(180.0 / self.d)) > 1e-9:
            raise ValueError(f"map field resolution {self.d} deg must divide 180 evenly")

    @property
    def nx(self) -> int:
        return round(360.0 / self.d)

    @property
    def ny(self) -> int:
        return round(180.0 / self.d) + 1

    def lats(self) -> np.ndarray:
        return self.lat0 - self.d * np.arange(self.ny)

    def lons(self) -> np.ndarray:
        return self.lon0 + self.d * np.arange(self.nx)

    def node(self, lat: float, lon: float) -> tuple[int, int]:
        """(row, column) of the node nearest to (lat, lon)."""
        j = min(self.ny - 1, max(0, round((self.lat0 - lat) / self.d)))
        i = round((((lon + 180.0) % 360.0) - 180.0 - self.lon0) / self.d) % self.nx
        return int(j), int(i)


# --------------------------------------------------------------------------- regridding
def _overlaps(
    centre: np.ndarray,
    half: float,
    n: int,
    *,
    wrap: bool,
    cut: tuple[float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Source indices and overlap weights for the boxes ``[centre - half, centre + half]``.

    Everything is in source-index units: source cell k covers ``[k - 0.5, k + 0.5]``.
    ``cut`` clips boxes and cells to an index range (the poles). With ``wrap`` indices are
    taken modulo ``n`` (a grid that is global in longitude); otherwise cells outside
    ``0..n-1`` get weight 0. Returns two arrays of shape ``(len(centre), K)``.
    """
    width = int(math.ceil(2 * half)) + 3
    first = np.floor(centre - half - 0.5).astype(np.int64)
    ks = first[:, None] + np.arange(width)[None, :]
    lo = np.maximum(ks - 0.5, (centre - half)[:, None])
    hi = np.minimum(ks + 0.5, (centre + half)[:, None])
    if cut is not None:
        lo, hi = np.maximum(lo, cut[0]), np.minimum(hi, cut[1])
    weights = np.clip(hi - lo, 0.0, None)
    weights[weights < MIN_WEIGHT] = 0.0
    if wrap:
        ks %= n
    else:
        weights[(ks < 0) | (ks >= n)] = 0.0
        ks = np.clip(ks, 0, n - 1)
    used = weights.any(axis=0)  # drop candidate columns that never overlap
    return ks[:, used], weights[:, used]


@dataclass(frozen=True)
class _Plan:
    """Row and column overlaps from one source geometry to the target grid."""

    rows: tuple[np.ndarray, np.ndarray]  # (ny, K) indices and weights
    cols: tuple[np.ndarray, np.ndarray]  # (nx, K) indices and weights

    def reduce(self, arr: np.ndarray) -> np.ndarray:
        """Weighted sums of ``arr`` (source shape) over every target box."""
        r_idx, r_w = self.rows
        c_idx, c_w = self.cols
        by_row = np.zeros((r_idx.shape[0], arr.shape[1]))
        for k in range(r_idx.shape[1]):
            by_row += r_w[:, k, None] * arr[r_idx[:, k], :]
        out = np.zeros((r_idx.shape[0], c_idx.shape[0]))
        for k in range(c_idx.shape[1]):
            out += by_row[:, c_idx[:, k]] * c_w[None, :, k]
        return out


def _plan(source: GridLike, target: TargetGrid) -> _Plan:
    nj, ni = source.values.shape
    return _cached_plan(nj, ni, source.lat0, source.lon0, source.dlat, source.dlon, target.d)


@functools.lru_cache(maxsize=16)
def _cached_plan(
    nj: int, ni: int, lat0: float, lon0: float, dlat: float, dlon: float, d: float
) -> _Plan:
    """Overlaps for one source geometry (computed once per grid, reused for every field)."""
    if dlat == 0 or dlon <= 0:
        raise ValueError("source grid steps must be non-zero (and dlon > 0)")
    target = TargetGrid(d)
    # Rows: fractional source row of each target latitude, cut at the poles.
    rows = (target.lats() - lat0) / dlat
    north, south = (90.0 - lat0) / dlat, (-90.0 - lat0) / dlat
    row_plan = _overlaps(
        rows, d / (2 * abs(dlat)), nj, wrap=False, cut=(min(north, south), max(north, south))
    )
    # Columns: longitude offset from the source's first column, in [-180, 180).
    offsets = ((target.lons() - lon0 + 180.0) % 360.0) - 180.0
    is_global = abs(ni * dlon - 360.0) < dlon * 0.5
    col_plan = _overlaps(offsets / dlon, d / (2 * dlon), ni, wrap=is_global)
    return _Plan(row_plan, col_plan)


def regrid_mean(
    source: GridLike, target: TargetGrid, *, directional: bool = False, factor: float = 1.0
) -> np.ndarray:
    """Area-weighted mean of the valid source cells in each target box (NaN = nodata).

    ``directional``: the values are directions in degrees and are averaged as unit
    vectors; the result is in [0, 360). ``factor`` multiplies the result (unit
    conversion, e.g. 0.01 for Pa -> hPa); it is applied after averaging, which is the same
    for a linear change of unit.
    """
    plan = _plan(source, target)
    values = source.values
    valid = ~np.isnan(values)
    weight = plan.reduce(valid.astype("float64"))
    empty = weight <= 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        if directional:
            rad = np.radians(np.where(valid, values, 0.0))
            sin = plan.reduce(np.where(valid, np.sin(rad), 0.0))
            cos = plan.reduce(np.where(valid, np.cos(rad), 0.0))
            out = np.degrees(np.arctan2(sin, cos)) % 360.0
            out[out >= 360.0] = 0.0  # -1e-15 % 360 rounds to 360.0
            out[empty | (np.hypot(sin, cos) <= MIN_RESULTANT * weight)] = np.nan
            return out
        out = plan.reduce(np.where(valid, values, 0.0)) / weight
    out[empty] = np.nan
    return out * factor if factor != 1.0 else out


# --------------------------------------------------------------------------- encoding
@dataclass(frozen=True)
class EncodedField:
    """One regridded, quantised and compressed field, ready for the map_fields table."""

    variable: str
    valid_time: datetime
    nx: int
    ny: int
    lat0: float
    lon0: float
    dlat: float
    dlon: float
    scale: float
    offset: float
    data: bytes  # zlib (RFC 1950) compressed uint8, row-major north to south

    @property
    def unit(self) -> str:
        return QUANTISATION[self.variable].unit

    def values(self) -> np.ndarray:
        """Decoded values (NaN = nodata), shape (ny, nx). For tests and diagnostics."""
        return decode_bytes(self.data, self.nx, self.ny, self.scale, self.offset)


def decode_bytes(data: bytes, nx: int, ny: int, scale: float, offset: float) -> np.ndarray:
    """Inverse of the encoding: zlib-decompress and dequantise (NODATA -> NaN)."""
    raw = np.frombuffer(zlib.decompress(data), dtype=np.uint8)
    if raw.size != nx * ny:
        raise ValueError(f"field holds {raw.size} cells, expected {nx}x{ny}")
    values = offset + scale * raw.astype("float64")
    values[raw == NODATA] = np.nan
    return values.reshape(ny, nx)


def encode_field(
    variable: str,
    valid_time: datetime,
    source: GridLike,
    target: TargetGrid,
    *,
    factor: float = 1.0,
) -> EncodedField:
    """Regrid ``source`` to ``target``, quantise it and compress it (see module docs)."""
    q = QUANTISATION[variable]
    grid = regrid_mean(source, target, directional=variable in DIRECTIONAL, factor=factor)
    packed = zlib.compress(q.encode(grid).tobytes(order="C"), ZLIB_LEVEL)
    return EncodedField(
        variable=variable,
        valid_time=valid_time,
        nx=target.nx,
        ny=target.ny,
        lat0=target.lat0,
        lon0=target.lon0,
        dlat=target.d,
        dlon=target.d,
        scale=q.scale,
        offset=q.offset,
        data=packed,
    )


def complete_times(fields: Iterable[EncodedField]) -> list[datetime]:
    """Valid times at which every variable of FIELD_VARIABLES is present, in order."""
    seen: dict[datetime, set[str]] = {}
    for f in fields:
        seen.setdefault(f.valid_time, set()).add(f.variable)
    return sorted(t for t, names in seen.items() if names >= set(FIELD_VARIABLES))


# --------------------------------------------------------------------------- collecting
class FieldSink:
    """Collects map fields while a GFS cycle is read (thread-safe).

    The reader hands every decoded field it wants stored to :meth:`encode`, which regrids,
    quantises and compresses it at once, so only the compressed bytes (about 0.1 MB per
    field at 0.5 deg) are kept, never the full-resolution array. Failures never raise:
    they become warnings, because a map field must never break spot forecasts.
    """

    def __init__(self, cycle: datetime, hours: Iterable[int], resolution_deg: float) -> None:
        self.cycle = cycle
        self.hours = sorted(set(hours))
        self._wanted = frozenset(self.hours)
        self.target = TargetGrid(resolution_deg)
        self._lock = threading.Lock()
        self._fields: dict[tuple[datetime, str], EncodedField] = {}
        self._warnings: list[str] = []
        # Requests and bytes made only for the map fields (not needed by any spot).
        self.request_count = 0
        self.bytes_downloaded = 0
        self.encode_seconds = 0.0

    def wants(self, fhour: int) -> bool:
        return fhour in self._wanted

    def valid_time(self, fhour: int) -> datetime:
        return self.cycle + timedelta(hours=fhour)

    def encode(
        self, fhour: int, variable: str, source: GridLike, *, factor: float = 1.0
    ) -> EncodedField | None:
        """The encoded field, or None (and a warning) if it could not be encoded."""
        started = time.perf_counter()
        try:
            field = encode_field(
                variable, self.valid_time(fhour), source, self.target, factor=factor
            )
        except Exception as exc:  # numpy raises many error types; never fail the step
            logger.warning("map field %s f%03d could not be encoded: %s", variable, fhour, exc)
            self.warn(f"f{fhour:03d} {variable}: could not encode ({type(exc).__name__}: {exc})")
            return None
        finally:
            with self._lock:
                self.encode_seconds += time.perf_counter() - started
        return field

    def put(self, fields: Iterable[EncodedField | None]) -> None:
        """Keep ``fields`` (None entries are skipped); a repeated key replaces the old one."""
        with self._lock:
            for f in fields:
                if f is not None:
                    self._fields[(f.valid_time, f.variable)] = f

    def warn(self, message: str) -> None:
        with self._lock:
            self._warnings.append(message)

    def count_download(self, requests: int, nbytes: int) -> None:
        with self._lock:
            self.request_count += requests
            self.bytes_downloaded += nbytes

    @property
    def fields(self) -> list[EncodedField]:
        with self._lock:
            items = list(self._fields.values())
        order = {v: n for n, v in enumerate(FIELD_VARIABLES)}
        return sorted(items, key=lambda f: (f.valid_time, order[f.variable]))

    @property
    def warnings(self) -> list[str]:
        """Failures, then one line per wanted hour that lacks a variable."""
        with self._lock:
            out = list(dict.fromkeys(self._warnings))
            have: dict[datetime, set[str]] = {}
            for t, name in self._fields:
                have.setdefault(t, set()).add(name)
        for h in self.hours:
            missing = [v for v in FIELD_VARIABLES if v not in have.get(self.valid_time(h), set())]
            if missing:
                out.append(f"f{h:03d} map fields incomplete: missing {', '.join(missing)}")
        return out


def summarise(fields: list[EncodedField], warnings: list[str]) -> dict[str, Any]:
    """The ``map_fields`` entry of a run's details."""
    return {
        "count": len(fields),
        "bytes": sum(len(f.data) for f in fields),
        "valid_times": [t.isoformat() for t in complete_times(fields)],
        "warnings": warnings[:50],
    }
