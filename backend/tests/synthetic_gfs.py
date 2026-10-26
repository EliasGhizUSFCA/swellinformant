"""SYNTHETIC NOAA GFS / GFS-Wave files for tests, served by ``httpx.MockTransport``.

Every value here is made up for testing (constant fields with distinct, recognisable
numbers); nothing is a real forecast. The files mimic the real layout checked live on the
2026-10-10 18Z cycle: GRIB2 messages built from the ecCodes ``regular_ll_sfc_grib2``
sample, one file per forecast hour with an NCEP-style ``.idx`` sidecar, records in the
real order (wave files start with WIND/WDIR/UGRD/VGRD fillers; the atmosphere file has
PRMSL first and LAND near the end) and land as missing values in the wave bitmap. The
same files feed the map-field tests (``map_settings``, ``add_field_hours``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import numpy as np

from app.core.config import Settings
from app.services.forecasts.grib import load_eccodes
from app.services.forecasts.noaa import GfsPaths

BASE_URL = "https://noaa.test"
PATHS = GfsPaths(BASE_URL)


def noaa_settings(lead: int = 24, step: int = 12, **kw: object) -> Settings:
    """Settings for the synthetic bucket: by default f000, f012 and f024.

    ``lead`` and ``step`` are applied after validation so tests can use fewer steps than
    the production limits (NOAA_MAX_LEAD_HOURS >= 24, NOAA_STEP_HOURS <= 6) allow.
    """
    values: dict[str, object] = {
        "noaa_gfs_base_url": BASE_URL,
        "forecast_days": 3,
        "grib_download_workers": 3,
        **kw,
    }
    base = Settings(**values)  # type: ignore[arg-type]
    return base.model_copy(update={"noaa_max_lead_hours": lead, "noaa_step_hours": step})


def map_settings(
    lead: int = 24,
    step: int = 12,
    *,
    map_lead: int = 24,
    providers: str = "noaa_gfs",
    **kw: object,
) -> Settings:
    """noaa_settings with map fields: providers "noaa_gfs" = the "with_noaa" mode.

    MAP_FIELD_MAX_LEAD_HOURS is ``map_lead`` (applied after validation, as in
    noaa_settings, because it must be >= 24 in production).
    """
    base = noaa_settings(lead, step, forecast_providers=providers, **kw)
    return base.model_copy(update={"map_field_max_lead_hours": map_lead})


# Synthetic constant values per wave record (``fhour / 100`` is added to HTSGW so each
# step is distinguishable). SWDIR 3 = 359.96 checks the 360 -> 0 wrap after rounding.
WAVE_VALUES: dict[str, float] = {
    "HTSGW:surface": 2.0,
    "PERPW:surface": 14.0,
    "DIRPW:surface": 280.0,
    "WVHGT:surface": 0.6,
    "SWELL:1 in sequence": 1.5,
    "SWELL:2 in sequence": 0.8,
    "SWELL:3 in sequence": 0.3,
    "WVPER:surface": 6.0,
    "SWPER:1 in sequence": 15.0,
    "SWPER:2 in sequence": 11.0,
    "SWPER:3 in sequence": 18.0,
    "WVDIR:surface": 45.0,
    "SWDIR:1 in sequence": 290.0,
    "SWDIR:2 in sequence": 200.0,
    "SWDIR:3 in sequence": 359.96,
}
WAVE_FILLERS = ("WIND:surface", "WDIR:surface", "UGRD:surface", "VGRD:surface")
# Atmosphere over sea: u=3, v=4 m/s (5 m/s = 18 km/h FROM 216.87 deg), gust 10 m/s.
# Over land: u=-20, v=0 and gust 30 m/s, so a land cell is easy to spot in the output.
SEA_U, SEA_V, SEA_GUST = 3.0, 4.0, 10.0
LAND_U, LAND_V, LAND_GUST = -20.0, 0.0, 30.0
PRMSL_PA = 101325.0


@dataclass(frozen=True)
class Grid:
    """Geometry of a synthetic regular lat/lon grid (north to south rows)."""

    lat0: float
    lon0: float  # [0, 360)
    d: float
    nj: int
    ni: int

    def mesh(self) -> tuple[np.ndarray, np.ndarray]:
        lats = self.lat0 - self.d * np.arange(self.nj)
        lons = (self.lon0 + self.d * np.arange(self.ni)) % 360.0
        lon2, lat2 = np.meshgrid(lons, lats)
        return lat2, np.where(lon2 >= 180.0, lon2 - 360.0, lon2)


# Wave grids with the real latitude coverage but only a narrow band of longitudes.
WAVE_GRID_GEOMETRY: dict[str, Grid] = {
    "global.0p16": Grid(52.5, 199.0, 1 / 6, 406, 24),  # 199E..202.8E (Oahu)
    "gsouth.0p25": Grid(-10.5, 23.0, 0.25, 277, 13),  # 23E..26E (Eastern Cape)
    "global.0p25": Grid(90.0, 355.0, 0.25, 721, 13),  # 5W..2W (north Scotland)
}
ATMOS_GRID = Grid(90.0, 0.0, 0.5, 361, 720)  # global 0.5 deg

Mask = Callable[[np.ndarray, np.ndarray], np.ndarray]  # (lat, lon) -> bool array


def grib2_message(values: np.ndarray, grid: Grid, cycle: datetime, fhour: int) -> bytes:
    eccodes = load_eccodes()
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "Ni", grid.ni)
        eccodes.codes_set(h, "Nj", grid.nj)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", grid.lat0)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", grid.lon0)
        eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", grid.lat0 - (grid.nj - 1) * grid.d)
        eccodes.codes_set(
            h, "longitudeOfLastGridPointInDegrees", (grid.lon0 + (grid.ni - 1) * grid.d) % 360
        )
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", grid.d)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", grid.d)
        eccodes.codes_set(h, "jScansPositively", 0)
        eccodes.codes_set(h, "dataDate", int(f"{cycle:%Y%m%d}"))
        eccodes.codes_set(h, "dataTime", cycle.hour * 100)
        eccodes.codes_set(h, "step", fhour)
        eccodes.codes_set(h, "bitmapPresent", 1)
        eccodes.codes_set(h, "missingValue", 9999)
        eccodes.codes_set_values(h, np.where(np.isnan(values), 9999, values).ravel())
        return bytes(eccodes.codes_get_message(h))
    finally:
        eccodes.codes_release(h)


def grib_file(records: list[tuple[str, bytes]], cycle: datetime, fhour: int) -> tuple[bytes, str]:
    """Concatenate messages and write the matching NCEP ``.idx`` text."""
    lines: list[str] = []
    offset = 0
    fcst = "anl" if fhour == 0 else f"{fhour} hour fcst"
    for n, (key, message) in enumerate(records, start=1):
        var, level = key.split(":", 1)
        lines.append(f"{n}:{offset}:d={cycle:%Y%m%d%H}:{var}:{level}:{fcst}:")
        offset += len(message)
    return b"".join(m for _, m in records), "\n".join(lines) + "\n"


@dataclass
class SyntheticGfs:
    """In-memory bucket: URL path -> bytes, plus per-path failure injection."""

    files: dict[str, bytes] = field(default_factory=dict)
    # path -> HTTP status to answer instead (e.g. 404 not published, 503 transient).
    fail: dict[str, int] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(self, url: str, content: bytes, idx: str) -> None:
        path = httpx.URL(url).path
        self.files[path] = content
        self.files[path + ".idx"] = idx.encode()

    def fail_url(self, url: str, status: int = 404) -> None:
        self.fail[httpx.URL(url).path] = status

    def add_wave(
        self,
        cycle: datetime,
        grid_name: str,
        fhour: int,
        *,
        grid: Grid | None = None,
        valid: Mask | None = None,
        missing_partition3: bool = False,
        values: dict[str, float] | None = None,
        arrays: dict[str, np.ndarray] | None = None,
    ) -> None:
        """``arrays`` replaces whole records (shape of the grid; NaN = masked cell)."""
        g = grid or WAVE_GRID_GEOMETRY[grid_name]
        lat, lon = g.mesh()
        ok = np.ones(lat.shape, dtype=bool) if valid is None else valid(lat, lon)
        filler = np.zeros((2, 2))
        small = Grid(0.0, 0.0, 1.0, 2, 2)
        records = [(k, grib2_message(filler, small, cycle, fhour)) for k in WAVE_FILLERS]
        for key, base in (values or WAVE_VALUES).items():
            value = base + (fhour / 100 if key == "HTSGW:surface" else 0.0)
            arr = np.where(ok, value, np.nan)
            if arrays and key in arrays:
                arr = np.where(ok, arrays[key], np.nan)
            if missing_partition3 and key.endswith("3 in sequence"):
                arr = np.full(lat.shape, np.nan)  # no third swell partition anywhere
            records.append((key, grib2_message(arr, g, cycle, fhour)))
        content, idx = grib_file(records, cycle, fhour)
        self.add(PATHS.wave(cycle, grid_name, fhour), content, idx)

    def add_atmos(
        self,
        cycle: datetime,
        fhour: int,
        *,
        land: Mask | None = None,
        uv: tuple[float, float] = (SEA_U, SEA_V),
    ) -> None:
        g = ATMOS_GRID
        lat, lon = g.mesh()
        is_land = np.zeros(lat.shape, dtype=bool) if land is None else land(lat, lon)
        small = Grid(0.0, 0.0, 1.0, 2, 2)
        filler = grib2_message(np.zeros((2, 2)), small, cycle, fhour)
        fields = {
            "PRMSL:mean sea level": np.full(lat.shape, PRMSL_PA),
            "GUST:surface": np.where(is_land, LAND_GUST, SEA_GUST),
            "UGRD:10 m above ground": np.where(is_land, LAND_U, uv[0]),
            "VGRD:10 m above ground": np.where(is_land, LAND_V, uv[1]),
            "LAND:surface": is_land.astype("float64"),
        }
        order = [
            "PRMSL:mean sea level",
            "CLMR:1 hybrid level",
            "GUST:surface",
            "HGT:0.01 mb",
            "UGRD:10 m above ground",
            "VGRD:10 m above ground",
            "ICEG:10 m above mean sea level",
            "LAND:surface",
            "ICEC:surface",
        ]
        records = [
            (k, grib2_message(fields[k], g, cycle, fhour) if k in fields else filler) for k in order
        ]
        content, idx = grib_file(records, cycle, fhour)
        self.add(PATHS.atmos(cycle, fhour), content, idx)

    def add_cycle(
        self,
        cycle: datetime,
        hours: list[int],
        *,
        grids: tuple[str, ...] = ("global.0p16", "gsouth.0p25", "global.0p25"),
        land: Mask | None = None,
        wave_valid: dict[str, Mask] | None = None,
    ) -> None:
        for h in hours:
            self.add_atmos(cycle, h, land=land)
            for name in grids:
                self.add_wave(cycle, name, h, valid=(wave_valid or {}).get(name))

    def add_field_hours(self, cycle: datetime, hours: list[int]) -> None:
        """What the map fields need: the atmosphere file and global.0p25 for each hour."""
        for h in hours:
            self.add_atmos(cycle, h)
            self.add_wave(cycle, "global.0p25", h)

    # ------------------------------------------------------------------ serving
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.fail:
            return httpx.Response(self.fail[path])
        if path not in self.files:
            return httpx.Response(404)
        content = self.files[path]
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(content))})
        rng = request.headers.get("Range")
        if rng is None:
            return httpx.Response(200, content=content)
        start_s, end_s = rng.removeprefix("bytes=").split("-")
        start, end = int(start_s), int(end_s) if end_s else len(content) - 1
        return httpx.Response(206, content=content[start : end + 1])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def paths_requested(self, method: str = "GET") -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]


def nearest_cell_mask(points: list[tuple[float, float]], grid: Grid) -> Mask:
    """Mask that is True only at the grid cell nearest to each (lat, lon) point."""

    def mask(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        out = np.zeros(lat.shape, dtype=bool)
        for plat, plon in points:
            j = int(round((grid.lat0 - plat) / grid.d))
            i = int(round(((plon % 360.0) - grid.lon0) / grid.d)) % grid.ni
            out[j, i] = True
        return out

    return mask
