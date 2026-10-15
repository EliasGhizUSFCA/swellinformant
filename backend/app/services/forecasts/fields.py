"""Map ocean layers: regrid, quantise and compress global model fields (pure numpy).

The map draws global layers (wave height, period and direction, the swell and wind-sea
partitions, 10 m wind, mean sea-level pressure) in the browser from small byte grids. This
module turns one decoded GRIB field (a :class:`GridLike`, e.g. ``grib.GridField``) into the
stored and served format described in app/schemas/map_fields.py. It does no I/O;
:class:`FieldSink` collects the fields while ``noaa.GfsPointReader`` reads a GFS cycle.

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
the node.) From a 0.5 deg source (GFS ``pgrb2.0p50``) to the 0.5 deg target each node takes
exactly the cell on it. Assumptions and edge cases:

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

Wave partitions (swell1 = primary swell, swell2 = secondary swell, windsea = wind sea;
:data:`PARTITION_SYSTEMS`). WAVEWATCH III numbers the swell partitions by height in every
cell and hour, so "swell 1" in one cell can be another system than "swell 1" next door, and
a box mean across such a swap gives a direction no wave has. The mixing guard (heuristic
values, docs/map-layers.md) therefore makes a partition node nodata when

* the unit vectors of its direction cells average to a resultant shorter than
  :data:`PARTITION_MIN_RESULTANT` (0.8 of the box weight; two equal halves 83 deg apart give
  0.75), or
* the partition exists in less than :data:`PARTITION_PRESENT_FRACTION` (half) of the box's
  sea area (the cells where the total wave height is valid), so a partition never spreads
  half a box beyond where the model has it,

and height, period and direction of one partition share one joint mask: at every node all
three are valid or all three are nodata. An absent partition is nodata (byte 255), never 0.
The core ``wave_dir`` keeps the plain rule above (nodata only where the vectors cancel).

Quantisation (:data:`QUANTISATION`): ``value = offset + scale * byte``, byte 255 = nodata,
other bytes clamped to 0..254 (directions wrap instead: 360 deg = byte 0).

=========== ====== ======== ======== =====================================
variable    unit   offset   scale    representable range
=========== ====== ======== ======== =====================================
hs          m      0        0.1      0 .. 25.4 m (offshore Hs, NOT surf)
tp          s      0        0.1      0 .. 25.4 s (peak period)
wave_dir    deg    0        1.5      0 .. 358.5 deg FROM (bytes 0..239)
wind_u      m/s    -63.5    0.5      -63.5 .. 63.5 m/s (eastward)
wind_v      m/s    -63.5    0.5      -63.5 .. 63.5 m/s (northward)
pressure    hPa    860      1.0      860 .. 1114 hPa
<p>_hs      m      0        0.1      as hs (partition significant height)
<p>_tp      s      0        0.1      as tp (partition period, see noaa.py)
<p>_dir     deg    0        1.5      as wave_dir (degrees FROM, no +180)
=========== ====== ======== ======== =====================================

``<p>`` is swell1, swell2 or windsea. Bytes are row-major from north to south, then zlib
(RFC 1950) compressed at level 6.
"""

from __future__ import annotations

import functools
import logging
import math
import threading
import time
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar, Literal, Protocol

import numpy as np

from app.models.forecast_engine import (
    CORE_MAP_FIELD_VARIABLES,
    MAP_FIELD_VARIABLES,
    PARTITION_MAP_FIELD_VARIABLES,
)

logger = logging.getLogger(__name__)

NODATA = 255
MAX_BYTE = 254
ZLIB_LEVEL = 6
# Overlaps below this (in source-cell units) are floating-point noise, not a real overlap.
MIN_WEIGHT = 1e-9
# Unit vectors averaging to a resultant shorter than this (relative) have no direction.
MIN_RESULTANT = 1e-6
# Partition mixing guard (see the module docs). Both values are heuristic: chosen from the
# audit's box statistics (docs/map-layers.md), not calibrated against observations.
PARTITION_MIN_RESULTANT = 0.8
PARTITION_PRESENT_FRACTION = 0.5


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


_HEIGHT = Quantisation("m", 0.0, 0.1)
_PERIOD = Quantisation("s", 0.0, 0.1)
_DIRECTION = Quantisation("deg", 0.0, 1.5, cyclic=True)

# Keys and units match models.forecast_engine.MAP_FIELD_VARIABLES (core, then partitions).
QUANTISATION: dict[str, Quantisation] = {
    "hs": _HEIGHT,
    "tp": _PERIOD,
    "wave_dir": _DIRECTION,
    "wind_u": Quantisation("m/s", -63.5, 0.5),
    "wind_v": Quantisation("m/s", -63.5, 0.5),
    "pressure": Quantisation("hPa", 860.0, 1.0),
    **{
        name: {"m": _HEIGHT, "s": _PERIOD, "deg": _DIRECTION}[unit]
        for name, unit in PARTITION_MAP_FIELD_VARIABLES.items()
    },
}
assert {k: q.unit for k, q in QUANTISATION.items()} == MAP_FIELD_VARIABLES
CORE_FIELD_VARIABLES = tuple(CORE_MAP_FIELD_VARIABLES)
PARTITION_FIELD_VARIABLES = tuple(PARTITION_MAP_FIELD_VARIABLES)
FIELD_VARIABLES = CORE_FIELD_VARIABLES + PARTITION_FIELD_VARIABLES
DIRECTIONAL = frozenset({"wave_dir", *(v for v in PARTITION_FIELD_VARIABLES if v.endswith("_dir"))})

SystemName = Literal["primary", "secondary", "windsea"]


@dataclass(frozen=True)
class PartitionSystem:
    """One wave-model partition and its three map variables (height, period, direction)."""

    prefix: str  # variable prefix: swell1, swell2, windsea
    system: SystemName
    label: str

    @property
    def variables(self) -> tuple[str, str, str]:
        return (f"{self.prefix}_hs", f"{self.prefix}_tp", f"{self.prefix}_dir")


PARTITION_SYSTEMS: tuple[PartitionSystem, ...] = (
    PartitionSystem("swell1", "primary", "primary swell"),
    PartitionSystem("swell2", "secondary", "secondary swell"),
    PartitionSystem("windsea", "windsea", "wind sea"),
)
SYSTEM_OF: dict[str, PartitionSystem] = {v: p for p in PARTITION_SYSTEMS for v in p.variables}
assert set(SYSTEM_OF) == set(PARTITION_FIELD_VARIABLES)


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
    source: GridLike,
    target: TargetGrid,
    *,
    directional: bool = False,
    factor: float = 1.0,
    min_resultant: float = MIN_RESULTANT,
    present_fraction: float | None = None,
    area: np.ndarray | None = None,
) -> np.ndarray:
    """Area-weighted mean of the valid source cells in each target box (NaN = nodata).

    ``directional``: the values are directions in degrees and are averaged as unit
    vectors; the result is in [0, 360). A node whose unit vectors average to a resultant no
    longer than ``min_resultant`` times its weight (1 = all cells agree) is nodata. ``factor``
    multiplies the result (unit conversion, e.g. 0.01 for Pa -> hPa); it is applied after
    averaging, which is the same for a linear change of unit.

    ``present_fraction``: a node is nodata unless its valid cells cover at least this
    fraction of the box's area, counted over the source cells that ``area`` marks (a boolean
    array of the source's shape, e.g. the sea cells of the total wave height; default every
    cell). The mean itself is still taken over the valid cells only.
    """
    plan = _plan(source, target)
    values = source.values
    valid = ~np.isnan(values)
    weight = plan.reduce(valid.astype("float64"))
    empty = weight <= 0.0
    if present_fraction is not None:
        if area is not None and area.shape != values.shape:
            raise ValueError(f"area mask {area.shape} does not match the source {values.shape}")
        base = plan.reduce(np.ones(values.shape) if area is None else area.astype("float64"))
        empty |= weight < present_fraction * base - MIN_WEIGHT
    with np.errstate(invalid="ignore", divide="ignore"):
        if directional:
            rad = np.radians(np.where(valid, values, 0.0))
            sin = plan.reduce(np.where(valid, np.sin(rad), 0.0))
            cos = plan.reduce(np.where(valid, np.cos(rad), 0.0))
            out = np.degrees(np.arctan2(sin, cos)) % 360.0
            out[out >= 360.0] = 0.0  # -1e-15 % 360 rounds to 360.0
            out[empty | (np.hypot(sin, cos) <= min_resultant * weight)] = np.nan
            return out
        out = plan.reduce(np.where(valid, values, 0.0)) / weight
    out[empty] = np.nan
    return out * factor if factor != 1.0 else out


def regrid_variable(
    variable: str,
    source: GridLike,
    target: TargetGrid,
    *,
    factor: float = 1.0,
    area: np.ndarray | None = None,
) -> np.ndarray:
    """:func:`regrid_mean` with the rules of ``variable``: directions as unit vectors, and
    for a partition variable the mixing guard (module docs; ``area`` = the sea cells)."""
    directional = variable in DIRECTIONAL
    if variable not in SYSTEM_OF:
        return regrid_mean(source, target, directional=directional, factor=factor)
    return regrid_mean(
        source,
        target,
        directional=directional,
        factor=factor,
        min_resultant=PARTITION_MIN_RESULTANT if directional else MIN_RESULTANT,
        present_fraction=PARTITION_PRESENT_FRACTION,
        area=area,
    )


def joint_mask(grids: Mapping[str, np.ndarray]) -> None:
    """Make every grid nodata where any of them is (in place): one mask per partition."""
    missing = np.zeros(next(iter(grids.values())).shape, dtype=bool)
    for grid in grids.values():
        missing |= np.isnan(grid)
    for grid in grids.values():
        grid[missing] = np.nan


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
    area: np.ndarray | None = None,
) -> EncodedField:
    """Regrid ``source`` to ``target``, quantise it and compress it (see module docs).

    A partition variable gets the mixing guard but no joint mask here (that needs the other
    two variables of its partition: :meth:`FieldSink.stage`)."""
    grid = regrid_variable(variable, source, target, factor=factor, area=area)
    return encode_grid(variable, valid_time, grid, target)


def encode_grid(
    variable: str, valid_time: datetime, grid: np.ndarray, target: TargetGrid
) -> EncodedField:
    """Quantise and compress a grid already on ``target`` (NaN = nodata)."""
    q = QUANTISATION[variable]
    if grid.shape != (target.ny, target.nx):
        raise ValueError(f"{variable}: grid {grid.shape} is not ({target.ny}, {target.nx})")
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


def _times_with(fields: Iterable[EncodedField], wanted: Iterable[str]) -> list[datetime]:
    seen: dict[datetime, set[str]] = {}
    for f in fields:
        seen.setdefault(f.valid_time, set()).add(f.variable)
    names = set(wanted)
    return sorted(t for t, have in seen.items() if have >= names)


def complete_times(fields: Iterable[EncodedField]) -> list[datetime]:
    """Valid times at which every core variable (CORE_MAP_FIELD_VARIABLES) is present."""
    return _times_with(fields, CORE_FIELD_VARIABLES)


def partition_times(fields: Iterable[EncodedField]) -> list[datetime]:
    """Valid times at which all three partitions (nine variables) are present."""
    return _times_with(fields, PARTITION_FIELD_VARIABLES)


# --------------------------------------------------------------------------- collecting
class FieldSink:
    """Collects map fields while a GFS cycle is read (thread-safe).

    The reader hands every decoded core field it wants stored to :meth:`encode`, which
    regrids, quantises and compresses it at once, so only the compressed bytes (about
    0.1 MB per field at 0.5 deg) are kept, never the full-resolution array. Partition fields
    go to :meth:`stage` (regridded at once, about 1 MB each at 0.5 deg) and are encoded by
    :meth:`flush` once the step is read, three per partition with one joint mask. Failures
    never raise: they become warnings, because a map field must never break spot forecasts.

    ``partitions``: whether the partition layers are wanted (MAP_FIELD_PARTITIONS); only
    then are missing partitions reported. ``max_bytes`` (MAP_FIELD_MAX_BYTES_PER_RUN) is
    the download budget for the fields: the reader asks :meth:`reserve` before every byte
    range it downloads for them. When the partitions no longer fit, they stop for the rest
    of the run; when the core records no longer fit, the run's map fields stop.
    """

    def __init__(
        self,
        cycle: datetime,
        hours: Iterable[int],
        resolution_deg: float,
        *,
        partitions: bool = False,
        max_bytes: int | None = None,
    ) -> None:
        self.cycle = cycle
        self.hours = sorted(set(hours))
        self._wanted = frozenset(self.hours)
        self.target = TargetGrid(resolution_deg)
        self.partitions = partitions
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fields: dict[tuple[datetime, str], EncodedField] = {}
        self._staged: dict[int, dict[str, np.ndarray]] = {}
        self._warnings: list[str] = []
        # Requests and bytes made only for the map fields (not needed by any spot).
        self.request_count = 0
        self.bytes_downloaded = 0
        self.encode_seconds = 0.0
        # Download budget: bytes reserved for ranges plus index files already read.
        self.budget_used = 0
        self.stopped: str | None = None  # why the map fields stopped (byte budget)
        self.partitions_stopped = False

    def wants(self, fhour: int) -> bool:
        return fhour in self._wanted

    def wants_partitions(self, fhour: int) -> bool:
        """True while partition layers are wanted for this hour and still fit the budget."""
        return self.partitions and fhour in self._wanted and not self.partitions_stopped

    def valid_time(self, fhour: int) -> datetime:
        return self.cycle + timedelta(hours=fhour)

    def _timed(self, started: float) -> None:
        with self._lock:
            self.encode_seconds += time.perf_counter() - started

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
            self._timed(started)
        return field

    def stage(
        self, fhour: int, variable: str, source: GridLike, *, area: np.ndarray | None = None
    ) -> None:
        """Regrid one partition field (with the mixing guard) and hold it until :meth:`flush`.

        ``area``: the source cells that count as the box's sea area (where the total wave
        height of the same file is valid)."""
        started = time.perf_counter()
        try:
            grid = regrid_variable(variable, source, self.target, area=area).astype("float32")
        except Exception as exc:  # as in encode: never fail the step
            logger.warning("map field %s f%03d could not be regridded: %s", variable, fhour, exc)
            self.warn(f"f{fhour:03d} {variable}: could not encode ({type(exc).__name__}: {exc})")
            return
        finally:
            self._timed(started)
        with self._lock:
            self._staged.setdefault(fhour, {})[variable] = grid

    def flush(self, fhour: int) -> None:
        """Encode the staged partitions of ``fhour``: a partition whose three variables are
        all staged gets one joint mask; one with a missing variable is not stored at all."""
        with self._lock:
            staged = self._staged.pop(fhour, {})
        if not staged:
            return
        started = time.perf_counter()
        out: list[EncodedField | None] = []
        try:
            for system in PARTITION_SYSTEMS:
                grids = {v: staged[v] for v in system.variables if v in staged}
                if not grids:
                    continue
                if len(grids) < len(system.variables):
                    missing = [v for v in system.variables if v not in grids]
                    self.warn(
                        f"f{fhour:03d} {system.label} not stored: {', '.join(missing)} missing"
                    )
                    continue
                joint_mask(grids)
                for variable, grid in grids.items():
                    out.append(self._encode_grid(fhour, variable, grid))
        finally:
            self._timed(started)
        self.put(out)

    def discard(self, fhour: int) -> None:
        """Drop what :meth:`stage` holds for ``fhour`` (its file failed half-way)."""
        with self._lock:
            self._staged.pop(fhour, None)

    def _encode_grid(self, fhour: int, variable: str, grid: np.ndarray) -> EncodedField | None:
        try:
            return encode_grid(variable, self.valid_time(fhour), grid, self.target)
        except Exception as exc:  # as in encode
            self.warn(f"f{fhour:03d} {variable}: could not encode ({type(exc).__name__}: {exc})")
            return None

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

    # ------------------------------------------------------------------ byte budget
    def charge(self, nbytes: int) -> None:
        """Count bytes already downloaded for the fields (e.g. an index file)."""
        with self._lock:
            self.budget_used += nbytes

    def reserve(self, fhour: int, nbytes: int, *, partitions: bool = False) -> bool:
        """Reserve ``nbytes`` of the budget for a download, or refuse (and stop) if they no
        longer fit: the partition layers for the rest of the run, or (core records) the
        run's map fields. Each stop is one warning."""
        with self._lock:
            if self.stopped is not None or (partitions and self.partitions_stopped):
                return False
            if self.max_bytes is None or self.budget_used + nbytes <= self.max_bytes:
                self.budget_used += nbytes
                return True
            used, limit = self.budget_used / 1e6, self.max_bytes / 1e6
            detail = (
                f"MAP_FIELD_MAX_BYTES_PER_RUN ({limit:.1f} MB) would be exceeded: "
                f"{used:.1f} MB used, {nbytes / 1e6:.2f} MB more needed"
            )
            if partitions:
                self.partitions_stopped = True
                self._warnings.append(
                    f"f{fhour:03d}: partition layers stopped for the rest of the run; {detail}"
                )
            else:
                self.stopped = f"f{fhour:03d}: map fields stopped; {detail}"
                self._warnings.append(f"{self.stopped}; later map hours are not read")
            return False

    # ------------------------------------------------------------------ results
    @property
    def fields(self) -> list[EncodedField]:
        with self._lock:
            items = list(self._fields.values())
        order = {v: n for n, v in enumerate(FIELD_VARIABLES)}
        return sorted(items, key=lambda f: (f.valid_time, order[f.variable]))

    @property
    def warnings(self) -> list[str]:
        """Failures, then one line per wanted hour that lacks a core variable, then (with
        partition layers wanted) one line per wanted hour that lacks a partition."""
        with self._lock:
            out = list(dict.fromkeys(self._warnings))
            have: dict[datetime, set[str]] = {}
            for t, name in self._fields:
                have.setdefault(t, set()).add(name)
        for h in self.hours:
            got = have.get(self.valid_time(h), set())
            missing = [v for v in CORE_FIELD_VARIABLES if v not in got]
            if missing:
                out.append(f"f{h:03d} map fields incomplete: missing {', '.join(missing)}")
        if self.partitions:
            for h in self.hours:
                got = have.get(self.valid_time(h), set())
                absent = [p.label for p in PARTITION_SYSTEMS if p.variables[0] not in got]
                if absent:
                    out.append(f"f{h:03d} map partitions incomplete: missing {', '.join(absent)}")
        return out


def summarise(fields: list[EncodedField], warnings: list[str]) -> dict[str, Any]:
    """The ``map_fields`` entry of a run's details."""
    return {
        "count": len(fields),
        "bytes": sum(len(f.data) for f in fields),
        "valid_times": [t.isoformat() for t in complete_times(fields)],
        "partition_times": [t.isoformat() for t in partition_times(fields)],
        "warnings": warnings[:50],
    }
