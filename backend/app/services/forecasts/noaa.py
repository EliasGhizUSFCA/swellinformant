"""NOAA GFS-Wave (WAVEWATCH III) + GFS atmosphere, read directly from NOAA open data.

Files come from the NOAA Open Data Dissemination (NODD) bucket on AWS
(``settings.noaa_gfs_base_url``). Only the needed GRIB2 records are downloaded, by HTTP
``Range`` from the offsets in each file's ``.idx`` (see :mod:`app.services.forecasts.grib`).
The layout, cadence, record lists and grid geometry below come from the research notes
(docs/surf-forecast-model-research.md, GFS and GFS-Wave sections) and were re-checked live
against the 2026-10-10 18Z cycle; docs/noaa-direct-ingestion.md summarises them.

Files per forecast hour FFF (GFS v16 layout):

* waves: ``gfs.YYYYMMDD/HH/wave/gridded/gfswave.tHHz.{grid}.fFFF.grib2``. Records 5-19
  (HTSGW, PERPW, DIRPW, WVHGT, SWELL 1-3, WVPER, SWPER 1-3, WVDIR, SWDIR 1-3) are
  contiguous, so each grid costs one index and one range request per step (8.26 MB for
  global.0p16, 3.59 MB for gsouth.0p25 and 8.64 MB for global.0p25 at f006).
* atmosphere: ``gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF``. UGRD/VGRD at 10 m,
  GUST, PRMSL, plus LAND until the land-sea mask is known (3.5 MB in 3-4 ranges).
  Record numbers shift between steps (UGRD 10 m is record 585 at f000 and 588 at f006),
  so records are matched by ``VAR:LEVEL``, never by number or offset.

Both files are published hourly to f120 and 3-hourly to f384 (f121, f122, f124 and f125
return HTTP 404; checked live for both files).

Wave grids, chosen per spot from the latitude of its forecast point (research notes,
GFS v16 post script grid IDs):

* ``global.0p16``: native WW3 grid gnh_10m, 1/6 deg, 52.5N to 15S (2160 x 406)
* ``gsouth.0p25``: native gsh_15m, 1/4 deg, 10.5S to 79.5S (1440 x 277)
* ``global.0p25``: glo_15mxt, 1/4 deg global, *interpolated* from the native grids, so it
  is only a fallback for points outside both (e.g. north of 52.5N: Thurso, Mullaghmore).

Conventions (research notes, from WW3 ``ww3_grib.F90`` and the WW3 manual):

* PERPW is the peak period Tp (``period_kind="tp"``). SWPER and WVPER are the partition
  periods; NCEP's inventory calls them mean periods ("Mean Period of Swell Waves", "Mean
  Period of Wind Waves"), and the ww3_grib source was not checked for them, so the map
  labels them "period", not "peak period".
* DIRPW, SWDIR and WVDIR are nautical degrees FROM in the GRIB files (``ww3_grib`` writes
  ``MOD(630 - RADE*theta, 360)``), so no +180 is applied. Only the station ``.bull`` and
  ``.spec`` files use the going-to convention.
* WW3 orders swell partitions by wave height at each hour. "Primary" is the largest swell
  partition at that hour, not a swell tracked through time, so partition indices can swap
  between hours, runs and models. A partition that does not exist is a missing value
  (None), not zero.
* HTSGW is the offshore significant wave height of the combined sea state. It is NOT the
  breaking (surf) height.
* Wind speed is ``|(u, v)|`` m/s x 3.6 = km/h and its direction FROM is
  ``atan2(-u, -v) mod 360`` (``geo.wind_from_uv``; None when calm). GUST (a boundary-layer
  mixing diagnostic, not an observed gust) is m/s x 3.6. PRMSL is Pa / 100 = hPa. GFS has
  no tide, so ``sea_level_m`` is always None.

Point sampling never crosses land. Waves come from the nearest cell with a valid HTSGW
value (land is masked in the GRIB bitmap) to the spot's forecast point, falling back to
the break location. Wind comes from the nearest GFS cell with LAND < 0.5 to the break,
because coastal cells that straddle land bias 10 m winds (research notes: 13 of 50 spot
coordinates sit on a GFS land cell). Both choices are made once per grid geometry and
reused for every step.

GFS v17 (proposed for late 2026) is expected to rename directories and files. The bucket
comes from ``settings.noaa_gfs_base_url`` and the path templates from
``settings.noaa_gfs_wave_path_template`` / ``noaa_gfs_atmos_path_template`` (defaults:
``WAVE_PATH_TEMPLATE`` / ``ATMOS_PATH_TEMPLATE``), so a rename is a configuration change.

Map ocean layers (docs/map-layers.md). A reader built with a ``field_sink``
(:class:`app.services.forecasts.fields.FieldSink`) also hands the global fields of every
map hour (:func:`app.services.map_fields.map_field_hours`) to the sink:

* wind and pressure: UGRD/VGRD 10 m and PRMSL. In spot hours they come from the
  ``pgrb2.0p25`` file the spots read anyway; for the fields alone they come from
  MAP_FIELD_ATMOS_PATH_TEMPLATE (default ``pgrb2.0p50``: the same three records in 0.86 MB
  instead of 2.93 MB per hour at f006), falling back to ``pgrb2.0p25`` for an hour whose
  file is not published.
* waves: HTSGW, PERPW and DIRPW from ``global.0p25``, the only GFS-Wave grid that covers
  the whole globe (1440 x 721 cells, 90N to 90S, 0E to 359.75E; decoded live on the
  2026-10-11 12Z cycle), in one range (1.82 MB at f006) when no spot uses that grid. With
  MAP_FIELD_PARTITIONS=on also the swell and wind-sea partitions (FIELD_PARTITION_RECORDS:
  WVHGT/WVPER/WVDIR and SWELL/SWPER/SWDIR 1 and 2, three more ranges, 5.15 MB at f006).
  The third swell partition is not read. Partition directions are degrees FROM like
  DIRPW (no +180); a missing partition is nodata, never 0, and a partition whose three
  records are not all in the index is not stored at all.

Map hours beyond the spots' hours are read for the fields alone, and only cycles in
MAP_FIELD_CYCLES carry map fields. MAP_FIELD_MAX_BYTES_PER_RUN bounds what is downloaded
for the fields alone (the sink's budget: partitions stop first, then the whole run). The
fields-only source :class:`NoaaGfsFieldsProvider` reads nothing else.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Hashable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np

from app.core.config import Settings, get_settings
from app.services.forecasts.base import (
    FORECAST_FIELDS,
    FetchResult,
    FetchStats,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
    nominal_gfs_cycle,
)
from app.services.forecasts.fields import (
    PARTITION_SYSTEMS,
    EncodedField,
    FieldSink,
    complete_times,
    partition_times,
)
from app.services.forecasts.grib import (
    CellChoice,
    GribError,
    GribFetcher,
    GridField,
    IndexEntry,
    NotPublishedError,
    PointSampler,
    decode_message,
    head_published,
    parse_ncep_idx,
    select_entries,
)
from app.services.forecasts.registry import NOAA_GFS_CODE
from app.services.geo import wind_from_uv
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
)
from app.services.map_fields import is_map_cycle, map_field_hours, map_fields_mode

logger = logging.getLogger(__name__)

HTTP_PROVIDER = "noaa_gfs"  # label for retries, logs and error messages
USER_AGENT = "SwellTravelAgent/1.0 (forecast ingestion)"
MS_TO_KMH = 3.6

# Output cadence of both the GFS-Wave gridded and the GFS pgrb2.0p25 files (v16).
HOURLY_UNTIL_HOUR = 120
LATE_STEP_HOURS = 3
MAX_LEAD_HOURS = 384
CYCLE_HOURS = 6
# current_run tries the nominal cycle and then up to this many older ones.
OLDER_CYCLES_TO_TRY = 4

WAVE_PATH_TEMPLATE = "gfs.{ymd}/{hh}/wave/gridded/gfswave.t{hh}z.{grid}.f{fhour:03d}.grib2"
ATMOS_PATH_TEMPLATE = "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fhour:03d}"
ATMOS_GRID = "pgrb2.0p25"
# Atmosphere read for the map fields alone ({grid} = MAP_FIELD_ATMOS_GRID).
MAP_ATMOS_PATH_TEMPLATE = "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.{grid}.f{fhour:03d}"
MAP_ATMOS_GRID = "0p50"


@dataclass(frozen=True)
class WaveGrid:
    """A GFS-Wave output grid and the latitudes it covers (decoded geometry, verified live)."""

    name: str
    north: float
    south: float
    native: bool  # False: interpolated from the native grids

    def covers(self, lat: float) -> bool:
        return self.south <= lat <= self.north


# In order of preference: native grids first. All three are global in longitude.
WAVE_GRIDS: tuple[WaveGrid, ...] = (
    WaveGrid("global.0p16", 52.5, -15.0, native=True),
    WaveGrid("gsouth.0p25", -10.5, -79.5, native=True),
    WaveGrid("global.0p25", 90.0, -90.0, native=False),
)


def wave_grid_for(lat: float) -> WaveGrid:
    """The preferred GFS-Wave grid for a point at latitude ``lat``."""
    for grid in WAVE_GRIDS:
        if grid.covers(lat):
            return grid
    return WAVE_GRIDS[-1]


HTSGW = "HTSGW:surface"
# GFS-Wave index key -> FORECAST_FIELDS name. HTSGW comes first: its bitmap (land, and any
# cell WW3 masks) decides which cells are valid for every wave field.
WAVE_FIELDS: dict[str, str] = {
    HTSGW: "sig_wave_height_m",
    "PERPW:surface": "wave_period_s",
    "DIRPW:surface": "wave_direction_deg",
    "SWELL:1 in sequence": "primary_swell_height_m",
    "SWPER:1 in sequence": "primary_swell_period_s",
    "SWDIR:1 in sequence": "primary_swell_direction_deg",
    "SWELL:2 in sequence": "secondary_swell_height_m",
    "SWPER:2 in sequence": "secondary_swell_period_s",
    "SWDIR:2 in sequence": "secondary_swell_direction_deg",
    "SWELL:3 in sequence": "tertiary_swell_height_m",
    "SWPER:3 in sequence": "tertiary_swell_period_s",
    "SWDIR:3 in sequence": "tertiary_swell_direction_deg",
    "WVHGT:surface": "wind_wave_height_m",
    "WVPER:surface": "wind_wave_period_s",
    "WVDIR:surface": "wind_wave_direction_deg",
}
U10 = "UGRD:10 m above ground"
V10 = "VGRD:10 m above ground"
GUST = "GUST:surface"
PRMSL = "PRMSL:mean sea level"
LAND = "LAND:surface"  # land-sea mask: 1 = land, 0 = sea (binary in GFS)
ATMOS_FIELDS = (U10, V10, GUST, PRMSL)

# Map ocean layers: index key -> map_fields variable. DIRPW is degrees FROM (as stored in
# GRIB2, see above); PRMSL is converted from Pa to hPa after regridding.
FIELD_WAVE_GRID = WAVE_GRIDS[-1]  # global.0p25: the only GFS-Wave grid covering the globe
FIELD_WAVE_RECORDS = {HTSGW: "hs", "PERPW:surface": "tp", "DIRPW:surface": "wave_dir"}
FIELD_ATMOS_RECORDS = {U10: "wind_u", V10: "wind_v", PRMSL: "pressure"}
# Partition layers (MAP_FIELD_PARTITIONS=on): index key -> map_fields variable. WW3 orders
# the swell partitions by height in each cell and hour ("1" = the largest swell there and
# then), so a partition number is not a swell system tracked across cells or hours.
FIELD_PARTITION_RECORDS = {
    "SWELL:1 in sequence": "swell1_hs",
    "SWPER:1 in sequence": "swell1_tp",
    "SWDIR:1 in sequence": "swell1_dir",
    "SWELL:2 in sequence": "swell2_hs",
    "SWPER:2 in sequence": "swell2_tp",
    "SWDIR:2 in sequence": "swell2_dir",
    "WVHGT:surface": "windsea_hs",
    "WVPER:surface": "windsea_tp",
    "WVDIR:surface": "windsea_dir",
}
# Index keys of each partition (height, period, direction), as the sink stores them.
PARTITION_KEYS: dict[str, tuple[str, ...]] = {
    system.prefix: tuple(
        k for v in system.variables for k, n in FIELD_PARTITION_RECORDS.items() if n == v
    )
    for system in PARTITION_SYSTEMS
}
PA_TO_HPA = 0.01
FIELDS_SOURCE_CODE = "noaa_gfs_fields"


def forecast_hours(max_lead_hours: int, step_hours: int) -> list[int]:
    """Forecast hours f000..``max_lead_hours`` every ``step_hours``, on the published cadence.

    Beyond f120 only every third hour exists, so a step that lands between published
    hours moves forward to the next one (1 or 2 h steps become 3-hourly, 4 or 5 h steps
    6-hourly). Nothing beyond f384 exists.
    """
    hours: list[int] = []
    h, step = 0, max(1, step_hours)
    while h <= min(max_lead_hours, MAX_LEAD_HOURS):
        hours.append(h)
        h += step
        if h > HOURLY_UNTIL_HOUR and h % LATE_STEP_HOURS:
            h += LATE_STEP_HOURS - h % LATE_STEP_HOURS
    return hours


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _direction(value: float | None) -> float | None:
    # 359.96 rounds to 360.0, which is 0 (and GRIB SWDIR does hold 360.0 values).
    return None if value is None else round(value % 360.0, 1) % 360.0


def _wave_value(name: str, value: float | None) -> float | None:
    if name.endswith("_direction_deg"):
        return _direction(value)
    return _rounded(value, 2 if name.endswith("_period_s") else 3)


def _atmos_values(raw: dict[str, float | None]) -> dict[str, float | None]:
    u, v, gust, prmsl = raw.get(U10), raw.get(V10), raw.get(GUST), raw.get(PRMSL)
    speed: float | None = None
    direction: float | None = None
    if u is not None and v is not None:
        speed, direction = wind_from_uv(u, v)
    return {
        "wind_speed_kmh": _rounded(None if speed is None else speed * MS_TO_KMH, 2),
        "wind_direction_deg": _direction(direction),
        "wind_gust_kmh": _rounded(None if gust is None else gust * MS_TO_KMH, 2),
        "pressure_msl_hpa": _rounded(None if prmsl is None else prmsl / 100.0, 2),
    }


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _as_utc(when: datetime) -> datetime:
    """Aware UTC datetime (a naive value is taken to be UTC already)."""
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _range_bytes(found: dict[str, IndexEntry], keys: Iterable[str]) -> int:
    """Bytes of the records ``keys`` (0 for a record that runs to the end of the file)."""
    return sum(found[k].length or 0 for k in keys)


class _BudgetExhausted(Exception):
    """The map fields' byte budget refused a download (the sink has the warning)."""


def _decode(blob: bytes, key: str) -> GridField:
    """decode_message, with ecCodes failures beyond message parsing also as GribError."""
    try:
        return decode_message(blob)
    except GribError:
        raise
    except Exception as exc:  # ecCodes raises many internal error types
        raise GribError(f"cannot decode {key}: {exc}") from exc


# --------------------------------------------------------------------------- locations
@dataclass(frozen=True)
class GfsPaths:
    """Builds file URLs from the bucket URL and the (GFS-version specific) path templates."""

    base_url: str
    wave_template: str = WAVE_PATH_TEMPLATE
    atmos_template: str = ATMOS_PATH_TEMPLATE
    # The atmosphere read for the map fields alone (MAP_FIELD_ATMOS_PATH_TEMPLATE and
    # MAP_FIELD_ATMOS_GRID); atmos_template is its fallback.
    map_atmos_template: str = MAP_ATMOS_PATH_TEMPLATE
    map_atmos_grid: str = MAP_ATMOS_GRID

    @classmethod
    def from_settings(cls, settings: Settings) -> GfsPaths:
        wave = settings.noaa_gfs_wave_path_template or WAVE_PATH_TEMPLATE
        atmos = settings.noaa_gfs_atmos_path_template or ATMOS_PATH_TEMPLATE
        map_atmos = settings.map_field_atmos_path_template or MAP_ATMOS_PATH_TEMPLATE
        return cls(
            settings.noaa_gfs_base_url.rstrip("/"),
            wave,
            atmos,
            map_atmos,
            settings.map_field_atmos_grid,
        )

    def _format(self, template: str, cycle: datetime, fhour: int, grid: str = "") -> str:
        c = _as_utc(cycle)
        path = template.format(ymd=f"{c:%Y%m%d}", hh=f"{c:%H}", grid=grid, fhour=fhour)
        return f"{self.base_url}/{path}"

    def wave(self, cycle: datetime, grid: str, fhour: int) -> str:
        return self._format(self.wave_template, cycle, fhour, grid)

    def atmos(self, cycle: datetime, fhour: int) -> str:
        return self._format(self.atmos_template, cycle, fhour)

    def map_atmos(self, cycle: datetime, fhour: int) -> list[str]:
        """Atmosphere files for the map fields alone, in order of preference: the map's
        own grid, then the spots' ``pgrb2.0p25`` file (if that is another file)."""
        first = self._format(self.map_atmos_template, cycle, fhour, self.map_atmos_grid)
        return list(dict.fromkeys([first, self.atmos(cycle, fhour)]))

    def shorten(self, text: str) -> str:
        """``text`` (a URL or an error message) without the bucket prefix."""
        return text.replace(f"{self.base_url}/", "")


@dataclass(frozen=True)
class SamplePoint:
    """Where to sample: waves near (wave_lat, wave_lon), 10 m wind near (wind_lat, wind_lon).

    For a surf spot the wave point is its offshore ``forecast_latitude/longitude`` and the
    wind point the break itself; for a buoy both are the buoy position. When no valid wave
    cell lies within the distance limit of the wave point, the wind point is tried for the
    waves too (for a spot: the break location).
    """

    key: Hashable
    wave_lat: float
    wave_lon: float
    wind_lat: float
    wind_lon: float

    @classmethod
    def at(cls, key: Hashable, lat: float, lon: float) -> SamplePoint:
        return cls(key, lat, lon, lat, lon)

    @classmethod
    def for_spot(cls, spot: SpotPoint) -> SamplePoint:
        return cls(
            spot.spot_id,
            spot.forecast_latitude,
            spot.forecast_longitude,
            spot.latitude,
            spot.longitude,
        )


@dataclass(frozen=True)
class PointCells:
    """Grid cells chosen for one point (a cell is None when none is valid close enough)."""

    grid: str
    wave_cell: CellChoice | None
    # "wave_point" (for a spot: its forecast point) or "wind_point" (the break, fallback).
    wave_anchor: str | None
    wind_cell: CellChoice | None

    def as_dict(self) -> dict[str, object]:
        wave = None
        if self.wave_cell is not None:
            wave = {**self.wave_cell.as_dict(), "anchor": self.wave_anchor}
        wind = None if self.wind_cell is None else {**self.wind_cell.as_dict(), "grid": ATMOS_GRID}
        return {"grid": self.grid, "wave_cell": wave, "wind_cell": wind}


@dataclass
class StepSample:
    """What one forecast hour of one cycle gave at every point."""

    fhour: int
    valid_time: datetime
    # Point key -> FORECAST_FIELDS values, for points whose wave grid was read and that have
    # a valid wave cell. Wind fields are None when the atmosphere file failed.
    values: dict[Hashable, dict[str, float | None]] = field(default_factory=dict)
    wind_ok: bool = False
    files_read: int = 0
    failed: list[str] = field(default_factory=list)  # files that could not be read, and why
    # Points whose chosen wave cell held no value at this step (e.g. masked by sea ice).
    masked: list[Hashable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    request_count: int = 0  # logical requests (retries of one request are not counted)
    bytes_downloaded: int = 0
    seconds: float = 0.0  # wall time (set for map-field reads only)


# --------------------------------------------------------------------------- reading
class GfsPointReader:
    """Reads one GFS / GFS-Wave cycle and samples it at fixed points, one step at a time.

    This is the core of :class:`NoaaGfsProvider` and the building block for verification
    code (e.g. GFS-Wave gridded forecasts at buoy positions): build one reader per cycle
    and call :meth:`read_step` for each forecast hour, from any number of threads. Each
    point's cells are chosen on the first step that decodes its grid (grib.PointSampler)
    and reused, and the GFS land-sea mask is downloaded only until it is known.

    Assumption: the wave bitmap and the land-sea mask do not change within a cycle. If a
    later step masks a chosen wave cell (e.g. sea ice), that point has no values at that
    step (listed in ``StepSample.masked``).

    With a ``field_sink``, every step the sink wants also yields the global map fields
    (see the module docstring); :meth:`read_field_steps` reads hours for the fields alone.
    A field that cannot be read or encoded is a sink warning, never a failed step. Without
    a sink (the default) nothing of this runs.
    """

    def __init__(
        self,
        client: httpx.Client,
        cycle: datetime,
        points: Sequence[SamplePoint],
        *,
        settings: Settings | None = None,
        paths: GfsPaths | None = None,
        backoff_seconds: float = 1.0,
        attempts: int = 4,
        field_sink: FieldSink | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or get_settings()
        self.cycle = _as_utc(cycle)
        if self.cycle.hour % CYCLE_HOURS or self.cycle != self.cycle.replace(
            minute=0, second=0, microsecond=0
        ):
            raise PermanentProviderError(f"{self.cycle:%Y-%m-%d %H:%M}Z is not a GFS cycle time")
        self.points = list(points)
        if len({p.key for p in self.points}) != len(self.points):
            raise ValueError("sample point keys must be unique")
        self.paths = paths or GfsPaths.from_settings(self.settings)
        self.backoff_seconds = backoff_seconds
        self.attempts = attempts
        self.max_distance_km = self.settings.grib_max_cell_distance_km
        self.grid_of: dict[Hashable, WaveGrid] = {
            p.key: wave_grid_for(p.wave_lat) for p in self.points
        }
        self._sampler = PointSampler(self.max_distance_km)
        self._lock = threading.Lock()
        self._sea: np.ndarray | None = None  # True where the GFS LAND field says sea
        self._sea_geometry: tuple[float, float, float, float, int, int] | None = None
        self._wave_cells: dict[Hashable, tuple[CellChoice | None, str | None]] = {}
        self._wind_cells: dict[Hashable, CellChoice | None] = {}
        self._checked_grids: set[str] = set()
        self.field_sink = field_sink

    @property
    def grids_used(self) -> list[WaveGrid]:
        used = set(self.grid_of.values())
        return [g for g in WAVE_GRIDS if g in used]

    def point_cells(self, key: Hashable) -> PointCells | None:
        """Cells chosen for ``key``, or None while its wave grid has not been decoded."""
        with self._lock:
            if key not in self._wave_cells:
                return None
            cell, anchor = self._wave_cells[key]
            return PointCells(self.grid_of[key].name, cell, anchor, self._wind_cells.get(key))

    def read_steps(self, hours: Sequence[int], workers: int = 1) -> list[StepSample]:
        """Read several forecast hours (in order), the first alone so later steps reuse its
        cell choices and land-sea mask instead of computing them concurrently."""
        if not hours:
            return []
        first = self.read_step(hours[0])
        if len(hours) == 1:
            return [first]
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="noaa") as pool:
            return [first, *pool.map(self.read_step, hours[1:])]

    def read_field_steps(self, hours: Sequence[int], workers: int = 1) -> list[StepSample]:
        """Read the map fields alone for several forecast hours (needs a field sink)."""
        if not hours:
            return []
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="noaa") as pool:
            return list(pool.map(self.read_field_step, hours))

    def read_field_step(self, fhour: int) -> StepSample:
        """Read only the map fields of one forecast hour: no spot sampling, LAND or GUST.

        For map hours the spots do not need and for the fields-only source. Failed files
        are listed in the result and given to the sink as warnings, never raised.
        """
        if self.field_sink is None:
            raise ValueError("read_field_step needs a field sink")
        return self._read_fields(
            fhour,
            (
                ("atmosphere", self._read_field_atmos),
                (f"wave {FIELD_WAVE_GRID.name}", self._read_field_waves),
            ),
        )

    def _new_fetcher(self) -> GribFetcher:
        return GribFetcher(
            self.client,
            HTTP_PROVIDER,
            attempts=self.attempts,
            backoff_seconds=self.backoff_seconds,
        )

    def read_step(self, fhour: int) -> StepSample:
        """Download and sample one forecast hour; failed files are listed, not raised."""
        # One fetcher per step: its counters are not shared between threads.
        fetcher = self._new_fetcher()
        step = StepSample(fhour, self.cycle + timedelta(hours=fhour))
        wind: dict[Hashable, dict[str, float | None]] = {}
        try:
            wind = self._read_atmos(fetcher, fhour, step.warnings)
            step.wind_ok = True
            step.files_read += 1
        except (ProviderError, httpx.HTTPError) as exc:
            step.failed.append(f"atmosphere: {self.paths.shorten(str(exc))}")
        for grid in self.grids_used:
            members = [p for p in self.points if self.grid_of[p.key] is grid]
            try:
                waves = self._read_wave(fetcher, grid, fhour, members, step.warnings)
            except (ProviderError, httpx.HTTPError) as exc:
                step.failed.append(f"wave {grid.name}: {self.paths.shorten(str(exc))}")
                continue
            step.files_read += 1
            for key, values in waves.items():
                if all(v is None for v in values.values()):
                    step.masked.append(key)
                    continue
                row: dict[str, float | None] = dict.fromkeys(FORECAST_FIELDS)
                row.update(values)
                row.update(wind.get(key, {}))
                step.values[key] = row
        step.request_count = fetcher.request_count
        step.bytes_downloaded = fetcher.bytes_downloaded
        if self.field_sink is not None and self._sink_wants(fhour):
            for failure in step.failed:  # files the map fields needed as well
                if failure.startswith(("atmosphere", f"wave {FIELD_WAVE_GRID.name}")):
                    self.field_sink.warn(f"f{fhour:03d} map fields {failure}")
        if self._sink_wants(fhour) and FIELD_WAVE_GRID not in self.grids_used:
            # No spot uses the global wave grid: fetch its three map records on their own.
            # Their failures stay out of step.failed (a map field is never a spot gap).
            extra = self._read_fields(
                fhour, ((f"wave {FIELD_WAVE_GRID.name}", self._read_field_waves),)
            )
            step.request_count += extra.request_count
            step.bytes_downloaded += extra.bytes_downloaded
        if step.failed:
            logger.warning("NOAA GFS f%03d incomplete: %s", fhour, "; ".join(step.failed))
        return step

    # ------------------------------------------------------------------ map fields
    def _sink_wants(self, fhour: int) -> bool:
        return self.field_sink is not None and self.field_sink.wants(fhour)

    def _encode(self, fhour: int, key: str, data: GridField) -> EncodedField | None:
        """Regrid, quantise and compress one decoded record for the sink at once."""
        assert self.field_sink is not None
        name = FIELD_ATMOS_RECORDS.get(key) or FIELD_WAVE_RECORDS[key]
        factor = PA_TO_HPA if key == PRMSL else 1.0
        return self.field_sink.encode(fhour, name, data, factor=factor)

    def _read_fields(
        self,
        fhour: int,
        readers: Sequence[tuple[str, Callable[[GribFetcher, int, list[str]], None]]],
    ) -> StepSample:
        """Run field readers with their own fetcher; failures become sink warnings."""
        sink = self.field_sink
        assert sink is not None
        started = time.monotonic()
        fetcher = self._new_fetcher()
        out = StepSample(fhour, self.cycle + timedelta(hours=fhour))
        for label, read in readers:
            if sink.stopped is not None:  # the byte budget is spent: nothing more is read
                break
            try:
                read(fetcher, fhour, out.warnings)
                out.files_read += 1
            except _BudgetExhausted:
                break
            except (ProviderError, httpx.HTTPError) as exc:
                out.failed.append(f"{label}: {self.paths.shorten(str(exc))}")
        out.request_count = fetcher.request_count
        out.bytes_downloaded = fetcher.bytes_downloaded
        out.seconds = time.monotonic() - started
        sink.count_download(out.bytes_downloaded)
        for message in out.warnings:
            sink.warn(message)
        for failure in out.failed:
            sink.warn(f"f{fhour:03d} map fields {failure}")
        if out.failed:
            logger.warning("NOAA GFS f%03d map fields incomplete: %s", fhour, "; ".join(out.failed))
        return out

    def _read_field_atmos(self, fetcher: GribFetcher, fhour: int, warnings: list[str]) -> None:
        """UGRD/VGRD 10 m and PRMSL of one step, for the sink only: from the map's own
        atmosphere file (MAP_FIELD_ATMOS_PATH_TEMPLATE, default pgrb2.0p50), or from
        pgrb2.0p25 when that hour of the map's file is not published."""
        urls = self.paths.map_atmos(self.cycle, fhour)
        keys = list(FIELD_ATMOS_RECORDS)  # U10 first: its date/time/step is checked
        for n, url in enumerate(urls):
            try:
                self._read_field_records(fetcher, fhour, url, keys, (U10, V10), None, warnings)
                return
            except NotPublishedError:
                if n + 1 == len(urls):
                    raise
                warnings.append(
                    f"f{fhour:03d}: {self.paths.shorten(url)} not published; read "
                    f"{self.paths.shorten(urls[n + 1])} instead"
                )

    def _read_field_waves(self, fetcher: GribFetcher, fhour: int, warnings: list[str]) -> None:
        """HTSGW, PERPW and DIRPW of ``global.0p25`` (one range request), plus the partition
        records (three more ranges) while the sink wants them, for the sink."""
        assert self.field_sink is not None
        url = self.paths.wave(self.cycle, FIELD_WAVE_GRID.name, fhour)
        keys = list(FIELD_WAVE_RECORDS)  # HTSGW first
        if self.field_sink.wants_partitions(fhour):
            keys += list(FIELD_PARTITION_RECORDS)
        self._read_field_records(fetcher, fhour, url, keys, (HTSGW,), FIELD_WAVE_GRID, warnings)

    @staticmethod
    def _partition_keys(
        fhour: int,
        short: str,
        keys: Sequence[str],
        found: dict[str, IndexEntry],
        warnings: list[str],
    ) -> list[str]:
        """The partition records among ``keys`` worth downloading: a partition is read only
        when the index has all three of its records (height, period, direction)."""
        chosen: set[str] = set()
        for system in PARTITION_SYSTEMS:
            wanted = [k for k in PARTITION_KEYS[system.prefix] if k in keys]
            absent = [k for k in wanted if k not in found]
            if absent:
                warnings.append(
                    f"f{fhour:03d}: {short} index lacks {', '.join(absent)}; "
                    f"{system.label} not stored"
                )
            else:
                chosen.update(wanted)
        return [k for k in keys if k in chosen]

    def _read_field_records(
        self,
        fetcher: GribFetcher,
        fhour: int,
        url: str,
        keys: list[str],
        required: tuple[str, ...],
        grid: WaveGrid | None,
        warnings: list[str],
    ) -> None:
        sink = self.field_sink
        assert sink is not None
        short = self.paths.shorten(url)
        before = fetcher.bytes_downloaded
        found = select_entries(parse_ncep_idx(fetcher.text(url + ".idx")), keys)
        sink.charge(fetcher.bytes_downloaded - before)
        missing = [k for k in keys if k not in found]
        if any(k in missing for k in required):
            raise GribError(f"{short}.idx lacks {', '.join(missing)}")
        core_missing = [k for k in missing if k not in FIELD_PARTITION_RECORDS]
        if core_missing:
            warnings.append(f"f{fhour:03d}: {short} index lacks {', '.join(core_missing)}")
        core = [k for k in keys if k in found and k not in FIELD_PARTITION_RECORDS]
        partitions = self._partition_keys(fhour, short, keys, found, warnings)
        # The byte budget, before anything is downloaded: core records first.
        if not sink.reserve(fhour, _range_bytes(found, core)):
            raise _BudgetExhausted
        if partitions and not sink.reserve(fhour, _range_bytes(found, partitions), partitions=True):
            partitions = []
        present = core + partitions  # the reference record (HTSGW or U10) first
        start = fetcher.bytes_downloaded
        blobs = fetcher.ranges(url, [found[k] for k in present])
        # A record that runs to the end of the file had no known size to reserve.
        sink.charge(max(0, fetcher.bytes_downloaded - start - _range_bytes(found, present)))
        pending: list[EncodedField | None] = []
        reference: tuple[tuple[float, float, float, float, int, int], str] | None = None
        sea: np.ndarray | None = None  # cells with a valid HTSGW: the partitions' box area
        # One decoded field at a time; only its compressed (core) or regridded (partition)
        # version is kept.
        try:
            for key, blob in zip(present, blobs, strict=True):
                data = _decode(blob, key)
                if reference is None:
                    reference = (data.geometry_key(), key)
                    self._check_valid_time(data, fhour, url)
                    if grid is not None:
                        self._check_geometry(grid, data, warnings)
                elif data.geometry_key() != reference[0]:
                    raise GribError(f"{key} in {short} is on a different grid than {reference[1]}")
                if key == HTSGW and partitions:
                    sea = ~np.isnan(data.values)
                if key in FIELD_PARTITION_RECORDS:
                    sink.stage(fhour, FIELD_PARTITION_RECORDS[key], data, area=sea)
                else:
                    pending.append(self._encode(fhour, key, data))
        except BaseException:
            sink.discard(fhour)  # nothing of a file that failed half-way is stored
            raise
        sink.put(pending)
        sink.flush(fhour)

    # ------------------------------------------------------------------ checks
    def _check_valid_time(self, data: GridField, fhour: int, url: str) -> None:
        """Refuse a record that belongs to another cycle or forecast hour than requested."""
        expected = (int(f"{self.cycle:%Y%m%d}"), self.cycle.hour * 100, float(fhour))
        actual = (data.data_date, data.data_time, data.step_hours)
        if any(a is not None and a != e for a, e in zip(actual, expected, strict=True)):
            raise GribError(
                f"{self.paths.shorten(url)} holds date/time/step {actual}, expected {expected}"
            )

    def _check_geometry(self, grid: WaveGrid, data: GridField, warnings: list[str]) -> None:
        """Warn once per grid if the decoded latitudes differ from the expected coverage."""
        with self._lock:
            if grid.name in self._checked_grids:
                return
            self._checked_grids.add(grid.name)
        lats = (data.lat_of(0), data.lat_of(data.shape[0] - 1))
        north, south = max(lats), min(lats)
        if abs(north - grid.north) > 0.01 or abs(south - grid.south) > 0.01:
            warnings.append(
                f"{grid.name}: decoded latitudes {north:g}..{south:g} differ from the expected "
                f"{grid.north:g}..{grid.south:g} (file layout changed?)"
            )

    @staticmethod
    def _same_geometry(reference: GridField, data: GridField, key: str, url: str) -> None:
        # Cells are chosen on one field and applied by (j, i) to the others.
        if data.geometry_key() != reference.geometry_key():
            raise GribError(f"{key} in {url} is on a different grid than {reference.short_name}")

    # ------------------------------------------------------------------ atmosphere
    def _read_atmos(
        self, fetcher: GribFetcher, fhour: int, warnings: list[str]
    ) -> dict[Hashable, dict[str, float | None]]:
        url = self.paths.atmos(self.cycle, fhour)
        short = self.paths.shorten(url)
        entries = parse_ncep_idx(fetcher.text(url + ".idx"))
        with self._lock:
            need_land = self._sea is None
        keys = ([LAND] if need_land else []) + list(ATMOS_FIELDS)
        found = select_entries(entries, keys)
        missing = [k for k in keys if k not in found]
        if any(k in missing for k in (LAND, U10, V10)):
            raise GribError(f"{short}.idx lacks {', '.join(missing)}")
        if missing:
            warnings.append(f"f{fhour:03d}: atmosphere index lacks {', '.join(missing)}")
        present = [k for k in keys if k in found]  # LAND (when needed) first, then U10
        blobs = fetcher.ranges(url, [found[k] for k in present])
        raw: dict[Hashable, dict[str, float | None]] = {p.key: {} for p in self.points}
        reference: tuple[tuple[float, float, float, float, int, int], str] | None = None
        land: tuple[np.ndarray, tuple[float, float, float, float, int, int]] | None = None
        cells: dict[Hashable, CellChoice | None] | None = None
        fields: list[EncodedField | None] = []  # map fields, kept once the file is read
        # Decode and sample one field at a time: a decoded pgrb2.0p25 field is 8.3 MB as
        # float64. Cells are chosen on U10 and applied by (j, i) to the other fields.
        for key, blob in zip(present, blobs, strict=True):
            data = _decode(blob, key)
            if reference is None:
                reference = (data.geometry_key(), key)
            elif data.geometry_key() != reference[0]:
                raise GribError(f"{key} in {short} is on a different grid than {reference[1]}")
            if key == LAND:
                land = (~np.isnan(data.values) & (data.values < 0.5), data.geometry_key())
                continue
            if key == U10:
                self._check_valid_time(data, fhour, url)
                if land is not None:  # this file's LAND matches its checked U10 record
                    self._set_land_mask(*land)
                cells = {p.key: self._wind_cell(data, p) for p in self.points}
            assert cells is not None  # U10 precedes the other fields in ``present``
            for p in self.points:
                raw[p.key][key] = self._sampler.sample(data, cells[p.key])
            if key in FIELD_ATMOS_RECORDS and self._sink_wants(fhour):
                fields.append(self._encode(fhour, key, data))
        if fields and self.field_sink is not None:
            self.field_sink.put(fields)
        return {k: _atmos_values(v) for k, v in raw.items()}

    def _set_land_mask(
        self, sea: np.ndarray, geometry: tuple[float, float, float, float, int, int]
    ) -> None:
        """Keep the first land-sea mask (True = sea, i.e. LAND < 0.5) and its grid."""
        with self._lock:
            if self._sea is None:
                self._sea = sea
                self._sea_geometry = geometry

    def _wind_cell(self, grid: GridField, p: SamplePoint) -> CellChoice | None:
        """Nearest GFS sea cell (LAND < 0.5) to the wind point (for a spot: the break)."""
        with self._lock:
            if self._sea is None or self._sea_geometry != grid.geometry_key():
                raise GribError("GFS wind grid does not match the land-sea mask grid")
            cell = self._sampler.cell(
                grid, ("wind", p.key), p.wind_lat, p.wind_lon, valid=self._sea, mask_name="sea"
            )
            self._wind_cells[p.key] = cell
            return cell

    # ------------------------------------------------------------------ waves
    def _read_wave(
        self,
        fetcher: GribFetcher,
        grid: WaveGrid,
        fhour: int,
        members: list[SamplePoint],
        warnings: list[str],
    ) -> dict[Hashable, dict[str, float | None]]:
        url = self.paths.wave(self.cycle, grid.name, fhour)
        short = self.paths.shorten(url)
        entries = parse_ncep_idx(fetcher.text(url + ".idx"))
        found = select_entries(entries, list(WAVE_FIELDS))
        if HTSGW not in found:
            raise GribError(f"{short}.idx has no {HTSGW} record")
        missing = [k for k in WAVE_FIELDS if k not in found]
        if missing:
            warnings.append(f"f{fhour:03d}: {grid.name} index lacks {', '.join(missing)}")
        present = [k for k in WAVE_FIELDS if k in found]  # HTSGW first
        blobs = fetcher.ranges(url, [found[k] for k in present])
        cells: dict[Hashable, CellChoice] = {}
        out: dict[Hashable, dict[str, float | None]] = {}
        reference: GridField | None = None
        fields: list[EncodedField | None] = []  # map fields (global.0p25 only)
        sink = self.field_sink if grid is FIELD_WAVE_GRID and self._sink_wants(fhour) else None
        # The spots read every partition record anyway, so the map's cost nothing extra.
        partitions = sink is not None and sink.wants_partitions(fhour)
        sea: np.ndarray | None = None
        # Decode one field at a time: a decoded global.0p25 field is 8 MB as float64.
        try:
            for idx_key, blob in zip(present, blobs, strict=True):
                data = _decode(blob, idx_key)
                if reference is None:  # HTSGW
                    reference = data
                    self._check_valid_time(data, fhour, url)
                    self._check_geometry(grid, data, warnings)
                    for p in members:
                        cell = self._wave_cell(data, p)
                        if cell is not None:
                            cells[p.key] = cell
                            out[p.key] = {}
                    if partitions:
                        sea = ~np.isnan(data.values)
                else:
                    self._same_geometry(reference, data, idx_key, short)
                name = WAVE_FIELDS[idx_key]
                for key, cell in cells.items():
                    out[key][name] = _wave_value(name, self._sampler.sample(data, cell))
                if sink is None:
                    continue
                if idx_key in FIELD_WAVE_RECORDS:
                    fields.append(self._encode(fhour, idx_key, data))
                elif partitions and idx_key in FIELD_PARTITION_RECORDS:
                    sink.stage(fhour, FIELD_PARTITION_RECORDS[idx_key], data, area=sea)
        except BaseException:
            if sink is not None:
                sink.discard(fhour)
            raise
        if sink is not None:
            sink.put(fields)
            sink.flush(fhour)
        return out

    def _wave_cell(self, htsgw: GridField, p: SamplePoint) -> CellChoice | None:
        """Nearest valid wave cell to the wave point, else to the wind point (the break)."""
        with self._lock:
            cell = self._sampler.cell(htsgw, ("wave", p.key), p.wave_lat, p.wave_lon)
            anchor = "wave_point"
            if cell is None and (p.wind_lat, p.wind_lon) != (p.wave_lat, p.wave_lon):
                cell = self._sampler.cell(htsgw, ("wave-fallback", p.key), p.wind_lat, p.wind_lon)
                anchor = "wind_point"
            self._wave_cells[p.key] = (cell, anchor if cell is not None else None)
            return cell


def _field_summary_line(sink: FieldSink, fields: list[EncodedField]) -> str:
    stored = sum(len(f.data) for f in fields) / 1e6
    complete = len(complete_times(fields))
    line = (
        f"map fields: {len(fields)} fields, {complete} of {len(sink.hours)} map hours complete "
        f"(f000-f{sink.hours[-1]:03d} at {sink.target.d:g} deg), {stored:.1f} MB stored; "
        f"{sink.bytes_downloaded / 1e6:.1f} MB downloaded for the fields only"
    )
    if sink.partitions:
        line += f"; partitions at {len(partition_times(fields))} of {len(sink.hours)} map hours"
    return line


@dataclass
class PointSamples:
    """Result of :func:`sample_gfs_points` for one cycle and forecast hour."""

    cycle: datetime
    fhour: int
    valid_time: datetime
    values: dict[Hashable, dict[str, float | None]]
    cells: dict[Hashable, dict[str, object]]
    failed: list[str]
    warnings: list[str]
    request_count: int
    bytes_downloaded: int


def sample_gfs_points(
    points: Sequence[SamplePoint],
    cycle: datetime,
    fhour: int,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    backoff_seconds: float = 1.0,
) -> PointSamples:
    """Sample GFS-Wave waves and GFS 10 m wind at arbitrary points for one cycle and step.

    Meant for verification, e.g. collecting the GFS-Wave *gridded* forecast at buoy
    positions (``SamplePoint.at("ndbc:51201", 21.67, -158.12)``) to pair with observations
    by valid time. Values use the provider's fields, units and conventions
    (FORECAST_FIELDS; ``wave_period_s`` is Tp; directions FROM; heights are offshore Hs,
    not surf height). A point with no valid wave cell within
    ``settings.grib_max_cell_distance_km`` is absent from ``values`` (its ``cells`` entry
    shows ``wave_cell: None``). Wind fields are None when the atmosphere file failed (see
    ``failed``) or no sea cell is close enough. ``fhour`` must be a published hour (hourly
    to 120, then every 3 h); an unpublished one shows up in ``failed``.

    One call downloads one step of every grid the points need (about 4-24 MB). For many
    steps of one cycle, use :class:`GfsPointReader` directly so that the cell choices and
    the land-sea mask are reused.
    """
    settings = settings or get_settings()
    own_client = client is None
    http = client or httpx.Client(
        timeout=settings.forecast_http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    )
    try:
        reader = GfsPointReader(
            http, cycle, points, settings=settings, backoff_seconds=backoff_seconds
        )
        step = reader.read_step(fhour)
    finally:
        if own_client:
            http.close()
    cells: dict[Hashable, dict[str, object]] = {}
    for p in points:
        chosen = reader.point_cells(p.key)
        if chosen is not None:
            cells[p.key] = chosen.as_dict()
    return PointSamples(
        cycle=reader.cycle,
        fhour=fhour,
        valid_time=step.valid_time,
        values=step.values,
        cells=cells,
        failed=step.failed,
        warnings=_unique(step.warnings),
        request_count=step.request_count,
        bytes_downloaded=step.bytes_downloaded,
    )


# --------------------------------------------------------------------------- provider
class NoaaGfsProvider(ForecastProvider):
    """GFS-Wave + GFS for every spot, read from the NOAA NODD bucket by byte range.

    ``fetch`` reads f000 to ``min(noaa_max_lead_hours, forecast_days * 24, 384)`` every
    ``noaa_step_hours`` (see :func:`forecast_hours`), with ``grib_download_workers``
    threads sharing one HTTP client. A forecast hour whose file fails after retries (or
    whose wave cell is masked) leaves a gap and a warning, and every spot it affects keeps
    its other hours but gets an ``incomplete: ...`` error, so the run is PARTIAL rather than
    SUCCESS; a spot without a usable wave cell is an error for that spot. A spot without a
    GFS sea cell near the break keeps its wave records with empty wind fields and a warning
    (as the Open-Meteo provider does when wind is unavailable).

    The cells chosen for each spot (wave grid, wave cell, wind cell, distances) are put in
    the ``extra`` of the spot's FIRST record only, i.e. ``wave_forecasts.extra`` of its
    earliest valid time, because ingestion stores per-record extras only. They are also
    kept in :attr:`last_cells` for diagnostics.

    ``request_count`` counts the requests made by ``fetch`` (index and range GETs; a retry
    of the same request is not counted again); the ``current_run`` probes are not included.

    Map ocean layers: when ``map_fields`` is on (by default when
    :func:`app.services.map_fields.map_fields_mode` is ``with_noaa``), ``fetch`` of a map
    cycle (MAP_FIELD_CYCLES) also returns the global fields of every map hour
    (:meth:`map_hours`) in ``FetchResult.fields``. Map hours beyond the spots' hours are
    read for the fields alone, and ``current_run`` then also waits for the last map hour of
    a map cycle. Field problems are ``FetchResult.field_warnings``, never spot errors.
    """

    code = NOAA_GFS_CODE

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
        map_fields: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        )
        self.backoff_seconds = backoff_seconds
        self.paths = GfsPaths.from_settings(self.settings)
        # Diagnostics of the last fetch.
        self.last_cells: dict[int, dict[str, object]] = {}
        self.last_hours: list[int] = []
        self.last_bytes_downloaded = 0
        self.last_step_bytes: dict[int, int] = {}
        # None: decided by the settings (MAP_FIELDS_ENABLED and FORECAST_PROVIDERS).
        self.map_fields = (
            map_fields_mode(self.settings) == "with_noaa" if map_fields is None else map_fields
        )
        # Map-field hours read for the fields alone in the last fetch (diagnostics).
        self.last_field_steps: list[StepSample] = []

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="NOAA GFS-Wave + GFS (direct from NOAA open data)",
            provider="noaa_nodd",
            # forecast_sources.wave_model holds 60 characters; WAVE_GRIDS has the details.
            wave_model="GFS-Wave WW3 global.0p16+gsouth.0p25 (global.0p25 fallback)",
            atmosphere_model="GFS 0.25 deg",
            is_demo=False,
            attribution=(
                "NOAA/NCEP Global Forecast System (GFS) and GFS-Wave (WAVEWATCH III), "
                "via the NOAA Open Data Dissemination (NODD) program."
            ),
            license_notes=(
                "US government work, not subject to copyright in the US. NODD terms: data "
                "are open to the public and can be used as desired; NOAA requests "
                "attribution and that no endorsement by or affiliation with NOAA be "
                "stated or implied. Values here are sampled, rounded and converted (km/h, "
                "hPa), so they must not be presented as original, unaltered NOAA data."
            ),
            family="ncep_gfs",
            kind="deterministic",
        )

    def forecast_hours(self) -> list[int]:
        s = self.settings
        return forecast_hours(
            min(s.noaa_max_lead_hours, s.forecast_days * 24, MAX_LEAD_HOURS), s.noaa_step_hours
        )

    def map_hours(self) -> list[int]:
        """Hours with map fields: MAP_FIELD_STEPS up to MAP_FIELD_MAX_LEAD_HOURS."""
        return map_field_hours(self.settings)

    def _new_sink(self, cycle: datetime) -> FieldSink:
        s = self.settings
        try:
            return FieldSink(
                _as_utc(cycle),
                self.map_hours(),
                s.map_field_resolution_deg,
                partitions=s.map_field_partitions == "on",
                max_bytes=s.map_field_max_bytes_per_run,
            )
        except ValueError as exc:  # e.g. MAP_FIELD_RESOLUTION_DEG=0.7 does not divide 180
            raise PermanentProviderError(f"map fields: {exc}") from exc

    # ------------------------------------------------------------------ run discovery
    def _published(self, url: str) -> bool:
        """True if ``url`` exists. Network failures raise instead of meaning "not yet"."""
        return head_published(self.client, url, HTTP_PROVIDER, backoff_seconds=self.backoff_seconds)

    def current_run(self, now: datetime) -> RunInfo:
        """Newest cycle whose last needed step exists for the atmosphere and every wave grid.

        Uploads arrive roughly in step order but not strictly (research notes: 45 of 120
        hourly .idx files appeared before the previous hour's), and f384 lands about
        5.2 h after the cycle. A cycle counts as complete when the ``.idx`` of its last
        needed step is published for the atmosphere file and all of WAVE_GRIDS (the grids
        in use depend on the spots, which are not known here). A step that is still
        missing despite this shows up as a gap in ``fetch``. A network failure raises
        TransientProviderError rather than falling back to an older cycle. With map fields
        on, the last needed step of a map cycle (MAP_FIELD_CYCLES) is the later of the
        spots' and the map's last hour.
        """
        spot_last = self.forecast_hours()[-1]
        map_last = self.map_hours()[-1] if self.map_fields else spot_last

        def needs(cycle: datetime) -> list[list[str]]:
            last = max(spot_last, map_last) if is_map_cycle(self.settings, cycle) else spot_last
            return [[self.paths.atmos(cycle, last)]] + [
                [self.paths.wave(cycle, g.name, last)] for g in WAVE_GRIDS
            ]

        return self._newest_cycle(now, "noaa-gfs", needs, max(spot_last, map_last))

    def _newest_cycle(
        self,
        now: datetime,
        key_prefix: str,
        needs: Callable[[datetime], list[list[str]] | None],
        last: int,
    ) -> RunInfo:
        """Newest cycle (the nominal one, then up to OLDER_CYCLES_TO_TRY older ones) whose
        needed files are published (the ``.idx`` of each). ``needs(cycle)`` lists the files,
        each as alternatives of which one suffices, or None to skip that cycle; ``last``
        names the last forecast hour in the error message."""
        nominal = nominal_gfs_cycle(now, self.settings.noaa_availability_lag_hours)
        incomplete: list[str] = []
        for back in range(OLDER_CYCLES_TO_TRY + 1):
            cycle = nominal - timedelta(hours=CYCLE_HOURS * back)
            files = needs(cycle)
            if files is None:
                continue
            # Alternatives in order: a fallback is asked for only when the first is missing.
            missing = next(
                (alt[0] for alt in files if not any(self._published(u + ".idx") for u in alt)),
                None,
            )
            if missing is None:
                return RunInfo(
                    run_key=f"{key_prefix}:{cycle:%Y%m%d%H}",
                    issued_at=cycle,
                    wave_model_run_at=cycle,
                    atmosphere_model_run_at=cycle,
                    metadata_source="noaa-s3-idx",
                )
            incomplete.append(f"{cycle:%Y%m%d%H}Z lacks {self.paths.shorten(missing)}.idx")
        raise TransientProviderError(
            f"no complete NOAA GFS cycle up to f{last:03d}: "
            + ("; ".join(incomplete) or "no map cycle (MAP_FIELD_CYCLES) among the cycles tried")
        )

    # ------------------------------------------------------------------ data
    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        result = FetchResult()
        self.last_cells, self.last_hours, self.last_bytes_downloaded = {}, [], 0
        self.last_step_bytes = {}
        self.last_field_steps = []
        if not spots:
            return result
        cycle = run.wave_model_run_at or run.issued_at
        sink: FieldSink | None = None
        if self.map_fields and not is_map_cycle(self.settings, cycle):
            logger.info("NOAA GFS %s: not a map cycle (MAP_FIELD_CYCLES)", run.run_key)
        elif self.map_fields:
            try:
                sink = self._new_sink(cycle)
            except PermanentProviderError as exc:  # a map setting never costs the spots
                logger.error("NOAA GFS %s: no map fields: %s", run.run_key, exc)
                result.field_warnings.append(str(exc))
        reader = GfsPointReader(
            self.client,
            cycle,
            [SamplePoint.for_spot(s) for s in spots],
            settings=self.settings,
            paths=self.paths,
            backoff_seconds=self.backoff_seconds,
            field_sink=sink,
        )
        hours = self.forecast_hours()
        workers = self.settings.grib_download_workers
        steps = reader.read_steps(hours, workers=workers)
        field_steps: list[StepSample] = []
        if sink is not None and any(s.files_read for s in steps):
            # Map hours the spots do not need (e.g. beyond FORECAST_DAYS): fields only.
            spot_hours = set(hours)
            extra = [h for h in sink.hours if h not in spot_hours]
            field_steps = reader.read_field_steps(extra, workers=workers)
        all_steps = [*steps, *field_steps]
        result.request_count = sum(s.request_count for s in all_steps)
        total_bytes = sum(s.bytes_downloaded for s in all_steps)
        result.stats = FetchStats(bytes_downloaded=total_bytes, requests=result.request_count)
        self.last_hours, self.last_bytes_downloaded = hours, total_bytes
        self.last_step_bytes = {s.fhour: s.bytes_downloaded for s in all_steps}
        self.last_field_steps = field_steps
        if not any(s.files_read for s in steps):
            first = next((f for s in steps for f in s.failed), "no file attempted")
            raise TransientProviderError(
                f"{run.run_key}: no NOAA GFS file could be read for any step ({first})"
            )

        km = self.settings.grib_max_cell_distance_km
        spot_warnings: list[str] = []
        for spot in spots:
            grid = reader.grid_of[spot.spot_id]
            cells = reader.point_cells(spot.spot_id)
            if cells is None:
                result.errors[spot.spot_id] = (
                    f"GFS-Wave {grid.name} could not be read for any step (see warnings)"
                )
                continue
            self.last_cells[spot.spot_id] = cells.as_dict()
            if cells.wave_cell is None:
                result.errors[spot.spot_id] = (
                    f"no valid GFS-Wave {grid.name} cell within {km:g} km of the forecast "
                    f"point ({spot.forecast_latitude:.2f}, {spot.forecast_longitude:.2f}) "
                    "or the break"
                )
                continue
            records = [
                ForecastRecord(s.valid_time, s.values[spot.spot_id], period_kind="tp")
                for s in steps
                if spot.spot_id in s.values
            ]
            masked = [s.fhour for s in steps if spot.spot_id in s.masked]
            if masked:
                spot_warnings.append(
                    f"{spot.slug}: GFS-Wave cell held no value at {len(masked)} step(s) "
                    f"(first f{masked[0]:03d}); those hours are gaps"
                )
            if not records:
                result.errors[spot.spot_id] = f"no GFS-Wave {grid.name} values for any step"
                continue
            if cells.wind_cell is None and any(s.wind_ok for s in steps):
                spot_warnings.append(
                    f"{spot.slug}: no GFS sea cell within {km:g} km of the break; wind left empty"
                )
            records[0].extra = cells.as_dict()
            result.records[spot.spot_id] = records
            # Hours that are gaps for this spot make it "incomplete" (run PARTIAL), as in the
            # ECMWF provider: a run with missing hours must not be reported as a success.
            no_wave = [s.fhour for s in steps if spot.spot_id not in s.values]
            no_wind = (
                []
                if cells.wind_cell is None
                else [s.fhour for s in steps if spot.spot_id in s.values and not s.wind_ok]
            )
            spot_gaps = [
                f"{label} missing at {len(hours_)} of {len(steps)} forecast hours "
                + ", ".join(f"f{h:03d}" for h in hours_[:12])
                + (", ..." if len(hours_) > 12 else "")
                for label, hours_ in (("waves", no_wave), ("wind/pressure", no_wind))
                if hours_
            ]
            if spot_gaps:
                result.errors[spot.spot_id] = "incomplete: " + "; ".join(spot_gaps)

        gaps = [f"f{s.fhour:03d} {failure}" for s in steps for failure in s.failed]
        incomplete = [f"f{s.fhour:03d}" for s in steps if s.failed]
        usage = ", ".join(
            f"{g.name}={sum(1 for v in reader.grid_of.values() if v is g)}"
            + ("" if g.native else " (interpolated)")
            for g in reader.grids_used
        )
        mb = total_bytes / 1e6
        label = f"{len(hours)} forecast hours f000-f{hours[-1]:03d}"
        if field_steps:
            label += f" + {len(field_steps)} hours for map fields only"
        # First, because ingestion keeps only the first 50 warnings.
        result.warnings.append(
            f"downloaded {mb:.1f} MB in {result.request_count} requests for {label} "
            f"({mb / len(all_steps):.1f} MB per hour)"
        )
        result.warnings.append(f"wave grids used (spots): {usage}")
        if sink is not None:
            result.fields, result.field_warnings = sink.fields, sink.warnings
            result.warnings.append(_field_summary_line(sink, result.fields))
        if incomplete:
            result.warnings.append(
                f"{len(incomplete)} of {len(hours)} forecast hours incomplete: "
                + ", ".join(incomplete)
            )
        if not any(s.wind_ok for s in steps):
            result.warnings.append("GFS atmosphere could not be read for any step; no wind")
        result.warnings.extend(_unique([w for s in steps for w in s.warnings]))
        result.warnings.extend(spot_warnings)
        result.warnings.extend(gaps)
        logger.info(
            "NOAA GFS %s: %d spots, %d errors, %d hours (%d incomplete), %d requests, %.1f MB",
            run.run_key,
            len(result.records),
            len(result.errors),
            len(hours),
            len(incomplete),
            result.request_count,
            mb,
        )
        return result


class NoaaGfsFieldsProvider(NoaaGfsProvider):
    """Only the global map fields of one GFS cycle, as a source of their own.

    The standalone map-fields mode (:func:`app.services.map_fields.map_fields_mode`:
    "noaa_gfs" is not a forecast provider, and MAP_FIELDS_ENABLED is true, or unset with
    live spot providers). Its runs belong to the source ``noaa_gfs_fields`` (kind
    ``fields``), hold map_fields rows and no spot records, and are never selected for spot
    forecasts, blending, comparisons, verification or the status page: those only read the
    configured providers' sources (``registry.configured_source_codes``) or ensembles.

    ``current_run`` follows the spot provider's availability rules (nominal cycle with
    NOAA_AVAILABILITY_LAG_HOURS, then up to OLDER_CYCLES_TO_TRY older ones), considers only
    the cycles in MAP_FIELD_CYCLES and needs only the last map hour of the map's atmosphere
    file (or its pgrb2.0p25 fallback) and ``global.0p25``. ``run_for_cycle`` takes any
    cycle. ``max_lead_hours`` overrides MAP_FIELD_MAX_LEAD_HOURS (e.g. for a short live
    check). A run that MAP_FIELD_MAX_BYTES_PER_RUN stops keeps the hours read before.
    """

    code = FIELDS_SOURCE_CODE
    RUN_KEY_PREFIX = "noaa-gfs-fields"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
        *,
        max_lead_hours: int | None = None,
    ) -> None:
        super().__init__(settings, client, backoff_seconds, map_fields=True)
        self.max_lead_hours = max_lead_hours

    def source_info(self) -> SourceInfo:
        spot = super().source_info()
        return SourceInfo(
            code=self.code,
            name="NOAA GFS-Wave + GFS global fields",
            provider=spot.provider,
            wave_model=f"GFS-Wave WW3 {FIELD_WAVE_GRID.name} (interpolated)",
            atmosphere_model=f"GFS pgrb2.{self.settings.map_field_atmos_grid}",
            is_demo=False,
            attribution=spot.attribution,
            license_notes=(
                "US government work, not subject to copyright in the US. NODD terms: data "
                "are open to the public and can be used as desired; NOAA requests "
                "attribution and that no endorsement by or affiliation with NOAA be "
                "stated or implied. Fields here are regridded, quantised and converted "
                "(hPa), so they must not be presented as original, unaltered NOAA data."
            ),
            family=spot.family,
            kind="fields",
        )

    def map_hours(self) -> list[int]:
        return map_field_hours(self.settings, self.max_lead_hours)

    def run_for_cycle(self, cycle: datetime) -> RunInfo:
        """RunInfo for a given cycle (no availability check; missing files become gaps)."""
        c = _as_utc(cycle)
        if c.hour % CYCLE_HOURS or c != c.replace(minute=0, second=0, microsecond=0):
            raise PermanentProviderError(f"{c:%Y-%m-%d %H:%M}Z is not a GFS cycle time")
        return RunInfo(f"{self.RUN_KEY_PREFIX}:{c:%Y%m%d%H}", c, c, c, "noaa-s3-idx")

    def current_run(self, now: datetime) -> RunInfo:
        """Newest map cycle whose last map hour exists for the atmosphere and global.0p25."""
        last = self.map_hours()[-1]

        def needs(cycle: datetime) -> list[list[str]] | None:
            if not is_map_cycle(self.settings, cycle):
                return None
            atmos = self.paths.map_atmos(cycle, last)
            return [atmos, [self.paths.wave(cycle, FIELD_WAVE_GRID.name, last)]]

        return self._newest_cycle(now, self.RUN_KEY_PREFIX, needs, last)

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        """The global fields of ``run``'s cycle; ``spots`` are ignored (no records)."""
        result = FetchResult()
        cycle = run.wave_model_run_at or run.issued_at
        sink = self._new_sink(cycle)
        reader = GfsPointReader(
            self.client,
            cycle,
            [],
            settings=self.settings,
            paths=self.paths,
            backoff_seconds=self.backoff_seconds,
            field_sink=sink,
        )
        steps = reader.read_field_steps(sink.hours, workers=self.settings.grib_download_workers)
        result.request_count = sum(s.request_count for s in steps)
        total_bytes = sum(s.bytes_downloaded for s in steps)
        result.stats = FetchStats(bytes_downloaded=total_bytes, requests=result.request_count)
        self.last_hours, self.last_bytes_downloaded = sink.hours, total_bytes
        self.last_step_bytes = {s.fhour: s.bytes_downloaded for s in steps}
        self.last_field_steps = steps
        if not any(s.files_read for s in steps):
            if sink.stopped is not None:  # retrying cannot help: the budget is a setting
                raise PermanentProviderError(f"{run.run_key}: {sink.stopped}")
            first = next((f for s in steps for f in s.failed), "no file attempted")
            raise TransientProviderError(
                f"{run.run_key}: no NOAA GFS file could be read for any map hour ({first})"
            )
        result.fields, result.field_warnings = sink.fields, sink.warnings
        mb = total_bytes / 1e6
        result.warnings.append(
            f"downloaded {mb:.1f} MB in {result.request_count} requests for {len(steps)} map "
            f"hours f000-f{sink.hours[-1]:03d} ({mb / len(steps):.1f} MB per hour)"
        )
        result.warnings.append(_field_summary_line(sink, result.fields))
        logger.info(
            "NOAA GFS fields %s: %d fields, %d requests, %.1f MB",
            run.run_key,
            len(result.fields),
            result.request_count,
            mb,
        )
        return result
