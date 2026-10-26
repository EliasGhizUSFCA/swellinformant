"""GRIB2 over HTTP: index parsing, byte-range fetching, decoding and point sampling.

NOAA (NODD on AWS) and ECMWF open data publish one GRIB2 file per forecast step with a
sidecar index. Fetching only the fields we need by HTTP ``Range`` keeps a global 0.25°
step at ~5 MB instead of ~12–550 MB.

* NCEP ``.idx`` lines look like ``5:2891445:d=2026101000:HTSGW:surface:24 hour fcst:``;
  a record's length is the next record's offset minus its own (the last one runs to EOF).
* ECMWF ``.index`` files are JSON lines with ``param``, ``levtype``, ``step``,
  ``_offset`` and ``_length``.

Decoding uses ecCodes (``eccodes`` + the ``eccodeslib`` binary wheel). Only regular
latitude/longitude grids are supported; anything else raises :class:`GribError`.

Point sampling never interpolates across land: each point takes the nearest grid cell
that holds a valid value (wave fields are missing over land), or, for atmospheric fields,
the nearest cell the land-sea mask marks as sea. The choice is made once per grid
geometry and reused for every step.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np

from app.services.http import PermanentProviderError, ProviderError, request_with_retries

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0
DEFAULT_MAX_DISTANCE_KM = 60.0


class GribError(ProviderError):
    """A GRIB message could not be decoded or is not in a supported layout."""


class NotPublishedError(PermanentProviderError):
    """The requested file does not exist (yet) on the server (HTTP 403/404)."""


class RangeNotHonouredError(GribError):
    """The server answered a byte-range request with the whole file (HTTP 200)."""


def _range_bounds(header: str) -> tuple[int, int | None] | None:
    """(start, inclusive end or None) of a single ``bytes=a-b`` Range header."""
    if not header.startswith("bytes=") or "," in header:
        return None
    start_s, _, end_s = header.removeprefix("bytes=").partition("-")
    try:
        return int(start_s), (int(end_s) if end_s else None)
    except ValueError:
        return None


def refuse_ignored_range(response: httpx.Response) -> None:
    """httpx response hook: refuse a 200 to a Range request before its body is read.

    A server that ignores ``Range`` sends the whole file (548 MB for a GFS ``pgrb2.0p25``
    file) where a few MB were asked for. Response hooks run before httpx reads the body,
    and an exception here closes the response unread, so nothing beyond the headers is
    downloaded. A 200 is accepted only for ``bytes=0-`` or when its Content-Length is no
    larger than the range asked for (a range covering the whole file).
    """
    if response.status_code != 200:
        return
    bounds = _range_bounds(response.request.headers.get("Range", ""))
    if bounds is None:
        return
    start, end = bounds
    length = response.headers.get("Content-Length")
    whole_file_asked = start == 0 and (
        end is None or (length is not None and length.isdigit() and int(length) <= end + 1)
    )
    if not whole_file_asked:
        raise RangeNotHonouredError(
            f"{response.request.url} answered the byte-range request "
            f"{response.request.headers['Range']} with the whole file (HTTP 200, "
            f"Content-Length {length or 'unknown'}): refused unread"
        )


# --------------------------------------------------------------------------- indexes
@dataclass(frozen=True)
class IndexEntry:
    number: int
    offset: int
    length: int | None  # None: runs to the end of the file
    variable: str  # NCEP abbreviation ("HTSGW") or ECMWF short name ("swh")
    level: str  # NCEP level ("surface", "1 in sequence") or ECMWF levtype ("sfc")
    step: str
    meta: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def key(self) -> str:
        return f"{self.variable}:{self.level}"


def parse_ncep_idx(text: str) -> list[IndexEntry]:
    """Records of an NCEP ``.idx``; lines that are not records are skipped.

    A record line (it starts with a record number) whose byte offset is not a whole number
    raises :class:`GribError`, so a damaged index is a failed file, not a crash.
    """
    rows: list[tuple[int, int, str, str, str]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        parts = line.strip().split(":")
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        if not parts[1].isdigit():
            raise GribError(f"invalid NCEP index line {n}: offset {parts[1][:20]!r}")
        rows.append((int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    entries: list[IndexEntry] = []
    for i, (num, off, var, level, step) in enumerate(rows):
        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
        entries.append(IndexEntry(num, off, None if nxt is None else nxt - off, var, level, step))
    return entries


def parse_ecmwf_index(text: str) -> list[IndexEntry]:
    entries: list[IndexEntry] = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GribError(f"invalid ECMWF index line {i + 1}: {exc}") from exc
        entries.append(
            IndexEntry(
                number=i + 1,
                offset=int(row["_offset"]),
                length=int(row["_length"]),
                variable=str(row.get("param", "")),
                level=str(row.get("levtype", "")),
                step=str(row.get("step", "")),
                meta={k: v for k, v in row.items() if not k.startswith("_")},
            )
        )
    return entries


def select_entries(entries: Iterable[IndexEntry], keys: Iterable[str]) -> dict[str, IndexEntry]:
    """Pick entries by ``"VAR:LEVEL"`` (exact) or ``"VAR"`` (first match)."""
    wanted = list(keys)
    found: dict[str, IndexEntry] = {}
    entries = list(entries)
    for key in wanted:
        for e in entries:
            if (e.key == key) if ":" in key else (e.variable == key):
                found[key] = e
                break
    return found


# --------------------------------------------------------------------------- fetching
class GribFetcher:
    """Fetches index files and byte ranges with retries, backoff and request counting."""

    def __init__(
        self,
        client: httpx.Client,
        provider: str,
        *,
        attempts: int = 4,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.provider = provider
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.request_count = 0
        self.bytes_downloaded = 0
        # First in line, so no other hook (e.g. a byte tally) reads a refused body.
        hooks = client.event_hooks
        if refuse_ignored_range not in hooks.get("response", []):
            client.event_hooks = {
                "request": list(hooks.get("request", [])),
                "response": [refuse_ignored_range, *hooks.get("response", [])],
            }

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        self.request_count += 1
        response = request_with_retries(
            self.client,
            "GET",
            url,
            provider=self.provider,
            attempts=self.attempts,
            backoff_seconds=self.backoff_seconds,
            headers=headers or {},
        )
        if response.status_code in (403, 404):
            raise NotPublishedError(
                f"{self.provider}: {url} not published (HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise PermanentProviderError(f"{self.provider}: HTTP {response.status_code} for {url}")
        self.bytes_downloaded += len(response.content)
        return response

    def text(self, url: str) -> str:
        return self._get(url).text

    def ranges(self, url: str, entries: Sequence[IndexEntry]) -> list[bytes]:
        """Download each entry's bytes; adjacent records are merged into one request."""
        ordered = sorted(entries, key=lambda e: e.offset)
        groups: list[list[IndexEntry]] = []
        for e in ordered:
            last = groups[-1][-1] if groups else None
            if (
                last is not None
                and last.length is not None
                and last.offset + last.length == e.offset
            ):
                groups[-1].append(e)
            else:
                groups.append([e])
        blobs: dict[int, bytes] = {}
        for group in groups:
            first, last = group[0], group[-1]
            end = "" if last.length is None else str(last.offset + last.length - 1)
            response = self._get(url, {"Range": f"bytes={first.offset}-{end}"})
            data = response.content
            if response.status_code != 206 and first.offset > 0:
                # The hook refuses this unread; a client whose hooks were replaced after
                # this fetcher was built still never slices records out of a whole file.
                raise RangeNotHonouredError(
                    f"{self.provider}: {url} ignored the Range header (HTTP "
                    f"{response.status_code}, {len(data)} bytes)"
                )
            pos = 0
            for e in group:
                size = len(data) - pos if e.length is None else e.length
                chunk = data[pos : pos + size]
                if e.length is not None and len(chunk) != e.length:
                    raise GribError(
                        f"{self.provider}: short read for {e.key} ({len(chunk)}/{e.length} bytes)"
                    )
                blobs[e.offset] = chunk
                pos += size
        return [blobs[e.offset] for e in entries]


# --------------------------------------------------------------------------- decoding
@dataclass
class GridField:
    """One decoded field on a regular lat/lon grid (rows = latitude, NaN = missing)."""

    short_name: str
    values: np.ndarray  # shape (nj, ni)
    lat0: float  # latitude of row 0
    lon0: float  # longitude of column 0, in [0, 360)
    dlat: float  # signed latitude step between rows
    dlon: float  # longitude step between columns (> 0)
    units: str = ""
    step_hours: float | None = None
    data_date: int | None = None
    data_time: int | None = None

    @property
    def shape(self) -> tuple[int, int]:
        nj, ni = self.values.shape
        return int(nj), int(ni)

    @property
    def is_global_in_longitude(self) -> bool:
        return abs(self.shape[1] * self.dlon - 360.0) < self.dlon * 0.5

    def geometry_key(self) -> tuple[float, float, float, float, int, int]:
        return (
            round(self.lat0, 4),
            round(self.lon0, 4),
            round(self.dlat, 6),
            round(self.dlon, 6),
            *self.shape,
        )

    def lat_of(self, j: int) -> float:
        return self.lat0 + j * self.dlat

    def lon_of(self, i: int) -> float:
        return (self.lon0 + i * self.dlon) % 360.0

    def fractional_index(self, lat: float, lon: float) -> tuple[float, float]:
        fj = (lat - self.lat0) / self.dlat
        fi = ((lon % 360.0) - self.lon0) / self.dlon
        if self.is_global_in_longitude:
            fi %= self.shape[1]
        return fj, fi

    def contains(self, lat: float, lon: float) -> bool:
        """True if (lat, lon) lies within half a cell of the grid.

        A grid that is global in longitude covers every longitude: a point in the last half
        column (e.g. 0.1°W on a grid starting at 0°E) belongs to column 0.
        """
        fj, fi = self.fractional_index(lat, lon)
        nj, ni = self.shape
        if not -0.5 <= fj <= nj - 0.5:
            return False
        return self.is_global_in_longitude or -0.5 <= fi <= ni - 0.5

    def value(self, j: int, i: int) -> float | None:
        v = float(self.values[j, i])
        return None if math.isnan(v) else v


def load_eccodes() -> Any:
    """Import ecCodes, always after psycopg's native libraries.

    The ``eccodeslib`` and ``psycopg[binary]`` wheels each bundle their own OpenLDAP build.
    When ecCodes is loaded first, the process segfaults at exit inside libldap's destructor
    (``python -c "import eccodes, psycopg"`` exits with SIGSEGV; the reverse order exits
    cleanly), which failed test runs and Celery worker shutdowns after all work had
    succeeded. Every ecCodes import in the app and the tests goes through this function;
    ``importlib`` keeps the order explicit (an import sorter would reorder plain imports).
    """
    importlib.import_module("psycopg")
    return importlib.import_module("eccodes")


def _key(handle: Any, name: str, default: Any = None) -> Any:
    eccodes = load_eccodes()

    try:
        return eccodes.codes_get(handle, name)
    except eccodes.KeyValueNotFoundError:
        return default


def decode_message(message: bytes) -> GridField:
    """Decode one GRIB message; every ecCodes failure is raised as :class:`GribError`.

    A message whose header parses but whose data section is damaged makes ecCodes raise its
    own errors (e.g. ``KeyValueNotFoundError`` from ``codes_get_values``); callers isolate
    failures per file by catching ``GribError``, so those are converted here.
    """
    eccodes = load_eccodes()

    try:
        handle = eccodes.codes_new_from_message(message)
    except Exception as exc:  # eccodes raises many internal error types
        raise GribError(f"cannot decode GRIB message: {exc}") from exc
    try:
        grid_type = _key(handle, "gridType")
        if grid_type != "regular_ll":
            raise GribError(f"unsupported GRIB grid type {grid_type!r}")
        ni, nj = int(_key(handle, "Ni")), int(_key(handle, "Nj"))
        lat0 = float(_key(handle, "latitudeOfFirstGridPointInDegrees"))
        lon0 = float(_key(handle, "longitudeOfFirstGridPointInDegrees")) % 360.0
        di = float(_key(handle, "iDirectionIncrementInDegrees"))
        dj = float(_key(handle, "jDirectionIncrementInDegrees"))
        if int(_key(handle, "iScansNegatively", 0)):
            raise GribError("GRIB grids scanning west are not supported")
        dlat = dj if int(_key(handle, "jScansPositively", 0)) else -dj
        if int(_key(handle, "jPointsAreConsecutive", 0)):
            raise GribError("column-major GRIB grids are not supported")
        values = np.asarray(eccodes.codes_get_values(handle), dtype="float64")
        if values.size != ni * nj:
            raise GribError(f"GRIB value count {values.size} != {ni}x{nj}")
        # ecCodes returns bitmap-masked points (land, for wave fields) as missingValue.
        values[values == float(_key(handle, "missingValue", 9999))] = np.nan
        step_raw = _key(handle, "step", None)
        return GridField(
            short_name=str(_key(handle, "shortName", "")),
            values=values.reshape(nj, ni),
            lat0=lat0,
            lon0=lon0,
            dlat=dlat,
            dlon=di,
            units=str(_key(handle, "units", "")),
            step_hours=float(step_raw) if step_raw is not None else None,
            data_date=_key(handle, "dataDate"),
            data_time=_key(handle, "dataTime"),
        )
    except GribError:
        raise
    except Exception as exc:  # eccodes raises many internal error types
        raise GribError(f"cannot decode GRIB message: {type(exc).__name__}: {exc}") from exc
    finally:
        eccodes.codes_release(handle)


# --------------------------------------------------------------------------- sampling
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class CellChoice:
    j: int
    i: int
    latitude: float
    longitude: float  # in [-180, 180)
    distance_km: float

    def as_dict(self) -> dict[str, float]:
        return {
            "lat": round(self.latitude, 4),
            "lon": round(self.longitude, 4),
            "distance_km": round(self.distance_km, 1),
        }


def nearest_valid_cell(
    grid: GridField,
    lat: float,
    lon: float,
    *,
    valid: np.ndarray | None = None,
    max_distance_km: float = DEFAULT_MAX_DISTANCE_KM,
) -> CellChoice | None:
    """Nearest cell whose ``valid`` flag is true (default: value is not NaN)."""
    if not grid.contains(lat, lon):
        return None
    ok = ~np.isnan(grid.values) if valid is None else valid
    nj, ni = grid.shape
    fj, fi = grid.fractional_index(lat, lon)
    cj, ci = int(round(fj)), int(round(fi))
    # Search a window just big enough to cover max_distance_km.
    km_per_row = abs(grid.dlat) * 111.2
    km_per_col = max(grid.dlon * 111.2 * math.cos(math.radians(lat)), 1e-3)
    rj = int(math.ceil(max_distance_km / km_per_row)) + 1
    ri = int(math.ceil(max_distance_km / km_per_col)) + 1
    best: CellChoice | None = None
    for j in range(max(0, cj - rj), min(nj, cj + rj + 1)):
        for di in range(-ri, ri + 1):
            i = ci + di
            if grid.is_global_in_longitude:
                i %= ni
            elif not 0 <= i < ni:
                continue
            if not ok[j, i]:
                continue
            clat, clon = grid.lat_of(j), grid.lon_of(i)
            d = haversine_km(lat, lon, clat, clon)
            if d <= max_distance_km and (best is None or d < best.distance_km):
                best = CellChoice(j, i, clat, ((clon + 180.0) % 360.0) - 180.0, d)
    return best


class PointSampler:
    """Caches each point's chosen grid cell per grid geometry (and validity mask)."""

    def __init__(self, max_distance_km: float = DEFAULT_MAX_DISTANCE_KM) -> None:
        self.max_distance_km = max_distance_km
        self._cache: dict[tuple[Any, ...], CellChoice | None] = {}

    def cell(
        self,
        grid: GridField,
        point_key: Any,
        lat: float,
        lon: float,
        *,
        valid: np.ndarray | None = None,
        mask_name: str = "values",
    ) -> CellChoice | None:
        key = (grid.geometry_key(), mask_name, point_key)
        if key not in self._cache:
            self._cache[key] = nearest_valid_cell(
                grid, lat, lon, valid=valid, max_distance_km=self.max_distance_km
            )
        return self._cache[key]

    def sample(self, grid: GridField, choice: CellChoice | None) -> float | None:
        if choice is None:
            return None
        return grid.value(choice.j, choice.i)
