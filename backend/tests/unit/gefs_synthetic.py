"""SYNTHETIC GEFS-Wave files for tests: invented values, not NOAA output.

Messages are built from the ecCodes ``regular_ll_sfc_grib2`` sample with the parameter
codes, product template (4.2 for mean/spread, derivedForecast 0/4) and level coding of the
real files, so the provider's layout and content checks run unchanged. The record order
and ``.idx`` text copy the live layout of
https://noaa-gefs-pds.s3.amazonaws.com/gefs.20261010/12/wave/gridded/gefs.wave.t12z.mean.global.0p25.f168.grib2.idx
(NOAA open data, US public domain). :class:`FakeBucket` serves them through
``httpx.MockTransport`` with HEAD, ``.idx`` and HTTP Range support.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import numpy as np

from app.services.forecasts.ensemble import MEMBERS, GefsPaths
from app.services.forecasts.grib import load_eccodes

BASE_URL = "https://gefs.test"
CYCLE = datetime(2026, 10, 10, 12, tzinfo=UTC)

PARAMS = {
    "HTSGW": (10, 0, 3),
    "PERPW": (10, 0, 11),
    "DIRPW": (10, 0, 10),
    "WVHGT": (10, 0, 5),
    "WVPER": (10, 0, 6),
    "WVDIR": (10, 0, 4),
    "WIND": (0, 2, 1),
    "WDIR": (0, 2, 0),
    "SWELL": (10, 0, 8),
    "SWPER": (10, 0, 9),
}
# Record order of the live mean/spread files (12 records).
STAT_ORDER = (
    "HTSGW",
    "PERPW",
    "DIRPW",
    "WVHGT",
    "WVPER",
    "WVDIR",
    "WIND",
    "WDIR",
    "SWELL",
    "SWELL",
    "SWPER",
    "SWPER",
)
# Synthetic field values (constant fields unless a test overrides them).
DEFAULTS: dict[str, dict[str, list[float]]] = {
    "mean": {
        "HTSGW": [2.5],
        "PERPW": [12.0],
        "WIND": [10.0],
        "SWELL": [2.0, 1.0],
        "SWPER": [14.0, 8.0],
    },
    "spread": {
        "HTSGW": [0.4],
        "PERPW": [1.0],
        "WIND": [2.0],
        "SWELL": [0.3, 0.2],
        "SWPER": [0.5, 1.0],
    },
}

Values = float | np.ndarray


@dataclass(frozen=True)
class Grid:
    lat0: float  # first (northernmost) row
    lon0: float
    step: float
    nj: int
    ni: int

    def full(self, value: Values, land: np.ndarray | None = None) -> np.ndarray:
        arr = np.array(np.broadcast_to(np.asarray(value, dtype="float64"), (self.nj, self.ni)))
        if land is not None:
            arr[land] = np.nan
        return arr


# 1-degree global grid (synthetic) for integration tests with the seeded spots.
GLOBAL_1DEG = Grid(90.0, 0.0, 1.0, 181, 360)


@dataclass
class Record:
    variable: str
    level: str
    description: str
    values: np.ndarray
    param: tuple[int, int, int]
    derived: int | None = None  # GRIB2 table 4.7 (0 mean, 4 spread); None = member file
    partition: int | None = None  # coded as level type 241 ("N in sequence") when set
    message_step: int | None = None  # override the step written into the message
    members: int = 31


def grib_message(record: Record, grid: Grid, cycle: datetime, fhour: int) -> bytes:
    eccodes = load_eccodes()
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        disc, cat, num = record.param
        eccodes.codes_set(h, "discipline", disc)
        if record.derived is not None:
            eccodes.codes_set(h, "productDefinitionTemplateNumber", 2)
            eccodes.codes_set(h, "derivedForecast", record.derived)
            eccodes.codes_set(h, "numberOfForecastsInEnsemble", record.members)
        eccodes.codes_set(h, "parameterCategory", cat)
        eccodes.codes_set(h, "parameterNumber", num)
        if record.partition is not None:
            eccodes.codes_set(h, "typeOfFirstFixedSurface", 241)
            eccodes.codes_set(h, "scaledValueOfFirstFixedSurface", record.partition)
        eccodes.codes_set(h, "dataDate", int(f"{cycle:%Y%m%d}"))
        eccodes.codes_set(h, "dataTime", cycle.hour * 100)
        eccodes.codes_set(h, "step", fhour if record.message_step is None else record.message_step)
        eccodes.codes_set(h, "Ni", grid.ni)
        eccodes.codes_set(h, "Nj", grid.nj)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", grid.lat0)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", grid.lon0)
        eccodes.codes_set(
            h, "latitudeOfLastGridPointInDegrees", grid.lat0 - (grid.nj - 1) * grid.step
        )
        eccodes.codes_set(
            h, "longitudeOfLastGridPointInDegrees", grid.lon0 + (grid.ni - 1) * grid.step
        )
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", grid.step)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", grid.step)
        eccodes.codes_set(h, "jScansPositively", 0)
        eccodes.codes_set(h, "bitmapPresent", 1)
        eccodes.codes_set(h, "missingValue", 9999)
        eccodes.codes_set(h, "bitsPerValue", 24)
        vals = np.where(np.isnan(record.values), 9999.0, record.values).ravel()
        eccodes.codes_set_values(h, vals)
        return bytes(eccodes.codes_get_message(h))
    finally:
        eccodes.codes_release(h)


def stat_records(
    kind: str,
    grid: Grid,
    *,
    overrides: Mapping[str, Sequence[Values]] | None = None,
    order: Sequence[str] = STAT_ORDER,
    levels: Mapping[int, str] | None = None,
    land: np.ndarray | None = None,
) -> list[Record]:
    """Records of one synthetic mean/spread file in the live order (or ``order``)."""
    values = {**DEFAULTS[kind], **(overrides or {})}
    seen: dict[str, int] = {}
    out: list[Record] = []
    for n, var in enumerate(order):
        k = seen.get(var, 0)
        seen[var] = k + 1
        series = values.get(var, [1.0])
        out.append(
            Record(
                var,
                (levels or {}).get(n, "surface"),
                f"ens {kind}",
                grid.full(series[min(k, len(series) - 1)], land),
                PARAMS[var],
                derived=0 if kind == "mean" else 4,
            )
        )
    return out


def member_records(grid: Grid, hs: Values, land: np.ndarray | None = None) -> list[Record]:
    """A synthetic member file: WIND, WDIR, HTSGW, PERPW, SWELL 1/2 (subset of the 23)."""
    return [
        Record("WIND", "surface", "", grid.full(8.0, land), PARAMS["WIND"]),
        Record("WDIR", "surface", "", grid.full(270.0, land), PARAMS["WDIR"]),
        Record("HTSGW", "surface", "", grid.full(hs, land), PARAMS["HTSGW"]),
        Record("PERPW", "surface", "", grid.full(12.0, land), PARAMS["PERPW"]),
        Record("SWELL", "1 in sequence", "", grid.full(2.0, land), PARAMS["SWELL"], partition=1),
        Record("SWELL", "2 in sequence", "", grid.full(1.0, land), PARAMS["SWELL"], partition=2),
    ]


@dataclass
class FakeBucket:
    """In-memory NODD bucket: GET (with Range), HEAD, per-path status overrides."""

    base_url: str = BASE_URL
    objects: dict[str, bytes] = field(default_factory=dict)
    status: dict[str, int] = field(default_factory=dict)
    log: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def paths(self) -> GefsPaths:
        return GefsPaths(self.base_url)

    def key(self, cycle: datetime, kind: str, fhour: int) -> str:
        return self.paths.shorten(self.paths.file(cycle, kind, fhour))

    def put_file(
        self, cycle: datetime, kind: str, fhour: int, records: Sequence[Record], grid: Grid
    ) -> None:
        fcst = "anl" if fhour == 0 else f"{fhour} hour fcst"
        blob, lines = b"", []
        for n, rec in enumerate(records, start=1):
            msg = grib_message(rec, grid, cycle, fhour)
            lines.append(
                f"{n}:{len(blob)}:d={cycle:%Y%m%d%H}:{rec.variable}:{rec.level}:{fcst}:"
                f"{rec.description}"
            )
            blob += msg
        key = self.key(cycle, kind, fhour)
        self.objects[key] = blob
        self.objects[key + ".idx"] = ("\n".join(lines) + "\n").encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        rng = request.headers.get("Range", "")
        self.log.append((request.method, key, rng))
        if key in self.status:
            return httpx.Response(self.status[key])
        data = self.objects.get(key)
        if data is None:
            return httpx.Response(404)
        if request.method == "HEAD":
            return httpx.Response(200)
        if rng:
            start_s, end_s = rng.removeprefix("bytes=").split("-")
            end = int(end_s) if end_s else len(data) - 1
            return httpx.Response(206, content=data[int(start_s) : end + 1])
        return httpx.Response(200, content=data)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def gets(self) -> list[str]:
        return [k for m, k, _ in self.log if m == "GET"]


def publish_cycle(
    bucket: FakeBucket,
    grid: Grid,
    fhours: Sequence[int],
    *,
    cycle: datetime = CYCLE,
    stats: Callable[[str, int], list[Record]] | None = None,
    member_hs: Callable[[str, int], Values | None] | None = None,
    land: np.ndarray | None = None,
) -> None:
    """Publish mean/spread (and optionally member) files for ``fhours`` of one cycle."""
    for fhour in fhours:
        for kind in ("mean", "spread"):
            records = (
                stats(kind, fhour) if stats is not None else stat_records(kind, grid, land=land)
            )
            bucket.put_file(cycle, kind, fhour, records, grid)
        if member_hs is not None:
            for member in MEMBERS:
                hs = member_hs(member, fhour)
                if hs is not None:  # None = this member file is not published
                    bucket.put_file(cycle, member, fhour, member_records(grid, hs, land), grid)
