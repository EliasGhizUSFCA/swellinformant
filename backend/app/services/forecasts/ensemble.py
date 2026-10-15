"""GEFS-Wave ensemble mean and spread at each spot, for forecast uncertainty.

NOAA's GEFS-Wave runs WAVEWATCH III with 31 members (control ``c00`` and perturbed
``p01``..``p30``) forced by the GEFS atmospheric ensemble, on a 0.25° grid out to 384 h,
and NCEP publishes the ensemble mean and spread of a subset of fields for every step.
This module samples those at each spot and stores them in ``forecast_ensemble_stats``,
never in ``wave_forecasts``: they describe how uncertain the forecast is and are not a
forecast to score. The source (``noaa_gefs_wave``) is not in ``FORECAST_PROVIDERS`` /
``registry.SOURCE_CODES``, so ``queries.latest_runs_by_spot`` can never choose one of its
runs, and its runs are stored with ``predictions_computed_at`` set so the prediction job
never treats them as pending.

File layout (research notes, GEFS-Wave sections; re-checked live on the 2026-10-10 12Z
cycle; docs/FORECAST_SOURCES.md):

* ``{NOAA_GEFS_BASE_URL}/gefs.YYYYMMDD/HH/wave/gridded/gefs.wave.tHHz.{mean,spread}
  .global.0p25.fFFF.grib2`` plus ``.idx``. 105 steps: every 3 h to f240, then every 6 h to
  f384. Regular lat/lon 1440x721 from 90N/0E; land and ice are bitmap-masked and decode
  as ``missingValue`` 9999 (grib.decode_message turns them into NaN).
* Mean/spread records (``.idx`` 7th field "ens mean" / "ens spread"; GRIB2 product
  template 4.2 with ``derivedForecast`` 0 = unweighted mean / 4 = spread over
  ``numberOfForecastsInEnsemble`` = 31): HTSGW, PERPW, DIRPW, WVHGT, WVPER, WVDIR, WIND,
  WDIR, SWELL, SWELL, SWPER, SWPER.
* The two swell partitions are coded ``surface`` (level type 1, value 0) in the ``.idx``
  AND inside the GRIB message, so only message order tells them apart: the FIRST
  SWELL/SWPER record is partition 1 (:func:`select_stat_records`, checked physically on
  every run by :func:`check_partition_order`). Member files label them "1 in sequence".

What the statistics are (checked against the 31 member files at f024 and f168 of the
2026-10-10 12Z cycle over all ~0.56 M ocean cells, to within the 0.01 packing step):

* mean = unweighted mean of the 31 members; spread = their population standard
  deviation (ddof = 0).
* For SWELL/SWPER a member without a swell partition at a cell counts as 0 m / 0 s, so
  where only some members have partition 1 the published mean is biased low (the period
  mean most of all: real periods averaged with zeros) and the spread inflated.
  :func:`swell1_complete_certain` proves from the published numbers alone when every
  member had the partition; rows carry the result in ``extra``.
* Partition 1 is a height rank within each member (WW3 orders swells by height), so
  members can put different physical swells into it. HTSGW statistics are the robust ones.

Periods: PERPW is the peak period of the combined spectrum and SWPER the partition peak
period (research notes: ww3_grib writes FP to PERPW and PTP to SWPER), so both are
``period_kind`` "tp". WIND is the 10 m forcing wind speed in m/s, stored in km/h. No
direction statistic is read: a published "mean direction" cannot be used as an ordinary
number and the uncertainty model does not need one.

Records read: GEFS_STAT_FIELDS (default ``HTSGW,PERPW``: Hs and Tp mean and spread, the
only statistics the uncertainty model and forecast confidence use). ``WIND``, ``SWELL``
(swell partition 1 height, plus the partition-order check) and ``SWPER`` can be added.
Cycles: GEFS_CYCLES (default all four).

Bandwidth (record sizes of the 2026-10-13 12Z f024 files): 1.60 MB per step for the four
HTSGW/PERPW records (about 65 MB for the 41 steps of a 240 h run), against about 4.9 MB per
step for all ten statistics records; member mode adds 31 HTSGW records of ~0.4 MB
(~12 MB per step).
"""

from __future__ import annotations

import logging
import math
import threading
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import ForecastEnsembleStat, ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.services.forecasts.base import (
    FetchResult,
    ForecastProvider,
    RunInfo,
    SourceInfo,
    SpotPoint,
    nominal_gfs_cycle,
)
from app.services.forecasts.grib import (
    CellChoice,
    GribError,
    GribFetcher,
    GridField,
    IndexEntry,
    PointSampler,
    decode_message,
    load_eccodes,
    parse_ncep_idx,
)
from app.services.forecasts.ingestion import STUCK_RUN_AFTER, ensure_source, spot_points
from app.services.forecasts.registry import GEFS_WAVE_CODE
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)

HTTP_PROVIDER = "noaa_gefs_wave"
USER_AGENT = "SwellTravelAgent/1.0 (forecast ingestion; NOAA open data)"
CYCLE_HOURS = 6
OLDER_CYCLES_TO_TRY = 3
MAX_LEAD_HOURS = 384
# Steps NOAA publishes per cycle (research notes; 105 files on 2026-10-10 12Z).
PUBLISHED_STEPS = frozenset((*range(0, 241, 3), *range(246, MAX_LEAD_HOURS + 1, 6)))
MEMBERS = ("c00", *(f"p{i:02d}" for i in range(1, 31)))
# .idx description "ens <kind>" and GRIB2 code table 4.7 value (template 4.2) per file.
STAT_KINDS = {"mean": 0, "spread": 4}
MS_TO_KMH = 3.6
# Mean/spread files carry swell partitions 1-2 only, both coded "surface".
SWELL_PARTITIONS_IN_STATS = 2
# Partition 1 >= partition 2 (minus one packing step) held on 99.994% of ocean cells in
# the 2026-10-10 12Z f024 mean file (99.95% within single members); a swapped order would
# hold on few cells, so 0.99 separates the two cases with a wide margin.
PARTITION_ORDER_MIN_FRACTION = 0.99
PARTITION_ORDER_TOLERANCE_M = 0.011
# Quantiles from fewer than two thirds of the 31 members are not stored (heuristic).
MIN_MEMBERS_FOR_QUANTILES = 21
QUANTILES = (0.1, 0.5, 0.9)
# The first readable step fixes each spot's grid cell; give up after this many tries.
ANCHOR_ATTEMPTS = 3
INSERT_CHUNK = 2000
PERIOD_KIND = "tp"
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
# ensemble_for() scans at most this many recent runs.
MAX_RUNS_SCANNED = 60


class GefsLayoutError(PermanentProviderError):
    """The files no longer look like the layout this module was verified against.

    Raised instead of guessing (e.g. which record is swell partition 1); it aborts the
    whole run, because every step of a cycle shares the layout.
    """


# --------------------------------------------------------------------------- layout
@dataclass(frozen=True)
class FieldSpec:
    """One record read from the mean/spread files and what it must decode to."""

    name: str
    variable: str  # NCEP abbreviation in the .idx
    param: tuple[int, int, int]  # GRIB2 discipline, parameterCategory, parameterNumber
    units: str  # ecCodes units of the decoded field
    partition: int | None = None  # swell partition (SWELL/SWPER only)


HS = FieldSpec("hs", "HTSGW", (10, 0, 3), "m")
TP = FieldSpec("tp", "PERPW", (10, 0, 11), "s")
WIND = FieldSpec("wind", "WIND", (0, 2, 1), "m s**-1")
SWELL1 = FieldSpec("swell1_height", "SWELL", (10, 0, 8), "m", partition=1)
SWPER1 = FieldSpec("swell1_period", "SWPER", (10, 0, 9), "s", partition=1)
# Read from the mean file of the first step only, for check_partition_order().
SWELL2 = FieldSpec("swell2_height", "SWELL", (10, 0, 8), "m", partition=2)
STAT_FIELDS = (HS, TP, WIND, SWELL1, SWPER1)
SPECS = {s.name: s for s in (*STAT_FIELDS, SWELL2)}
# GEFS_STAT_FIELDS tokens (.idx abbreviations) -> records; HTSGW is required (its mean
# fixes each spot's grid cell).
STAT_FIELD_TOKENS: dict[str, FieldSpec] = {
    "HTSGW": HS,
    "PERPW": TP,
    "WIND": WIND,
    "SWELL": SWELL1,
    "SWPER": SWPER1,
}


def stat_fields_for(tokens: Sequence[str]) -> tuple[FieldSpec, ...]:
    """The records GEFS_STAT_FIELDS asks for, in file order (HTSGW first)."""
    unknown = sorted(set(tokens) - set(STAT_FIELD_TOKENS))
    if unknown or "HTSGW" not in tokens:
        raise PermanentProviderError(
            f"GEFS_STAT_FIELDS={','.join(tokens)!r}: must include HTSGW and only name "
            f"{', '.join(STAT_FIELD_TOKENS)}"
            + (f" (unknown: {', '.join(unknown)})" if unknown else "")
        )
    return tuple(spec for spec in STAT_FIELDS if spec.variable in tokens)


COLUMNS = {
    ("hs", "mean"): "hs_mean_m",
    ("hs", "spread"): "hs_spread_m",
    ("tp", "mean"): "tp_mean_s",
    ("tp", "spread"): "tp_spread_s",
    ("wind", "mean"): "wind_speed_mean_kmh",
    ("wind", "spread"): "wind_speed_spread_kmh",
    ("swell1_height", "mean"): "swell1_height_mean_m",
    ("swell1_height", "spread"): "swell1_height_spread_m",
    ("swell1_period", "mean"): "swell1_period_mean_s",
    ("swell1_period", "spread"): "swell1_period_spread_s",
}
STAT_COLUMNS = tuple(COLUMNS.values())


def parse_stat_index(text: str) -> tuple[list[IndexEntry], dict[int, str]]:
    """NCEP ``.idx`` entries plus each record's 7th field ("ens mean", "ens spread").

    grib.parse_ncep_idx keeps the first six fields only; the statistic is in the 7th.
    """
    entries = sorted(parse_ncep_idx(text), key=lambda e: e.number)
    descriptions: dict[int, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 6 and parts[0].isdigit():
            descriptions[int(parts[0])] = parts[6].strip() if len(parts) > 6 else ""
    return entries, descriptions


def _pick(entries: Sequence[IndexEntry], spec: FieldSpec, where: str) -> IndexEntry:
    found = [e for e in entries if e.variable == spec.variable]
    levels = [e.level for e in found]
    if spec.partition is None:
        if len(found) != 1 or found[0].level != "surface":
            raise GefsLayoutError(
                f"{where}: expected one {spec.variable}:surface record, found levels {levels}"
            )
        return found[0]
    if found and all(level.endswith(" in sequence") for level in levels):
        # NOAA labelled the partitions explicitly (as in the member files): use the label.
        labelled = [e for e in found if e.level == f"{spec.partition} in sequence"]
        if len(labelled) != 1:
            raise GefsLayoutError(
                f"{where}: no unique {spec.variable} partition {spec.partition} in {levels}"
            )
        return labelled[0]
    if len(found) == SWELL_PARTITIONS_IN_STATS and all(level == "surface" for level in levels):
        # The verified layout: partitions coded "surface", told apart only by order.
        return found[spec.partition - 1]
    raise GefsLayoutError(
        f"{where}: {spec.variable} records have levels {levels}; expected "
        f"{SWELL_PARTITIONS_IN_STATS} records coded 'surface' (partition = message order) "
        "or explicit 'N in sequence' levels. Re-verify the partition order before use."
    )


def select_stat_records(
    text: str,
    kind: str,
    *,
    with_swell2: bool = False,
    where: str = "GEFS-Wave",
    fields: Sequence[FieldSpec] = STAT_FIELDS,
) -> dict[str, IndexEntry]:
    """Pick the records of one mean/spread ``.idx``, asserting the layout we depend on.

    * every record is described "ens <kind>" (a mean file never holds spread records),
    * HTSGW, PERPW and WIND appear exactly once, at ``surface``,
    * SWELL and SWPER appear exactly twice, both coded ``surface``; partition 1 is the
      first occurrence in file order (research notes: "distinguished by message order";
      confirmed numerically against the member files). Explicit ``"1 in sequence"``
      levels are accepted too. Anything else raises :class:`GefsLayoutError`.
    """
    entries, descriptions = parse_stat_index(text)
    if not entries:
        raise GefsLayoutError(f"{where}: empty or unreadable {kind} index")
    expected = f"ens {kind}"
    other = sorted({descriptions.get(e.number, "") for e in entries} - {expected})
    if other:
        raise GefsLayoutError(f"{where}: {kind} index has records described {other}")
    specs = [*fields, SWELL2] if with_swell2 else list(fields)
    return {spec.name: _pick(entries, spec, where) for spec in specs}


_HEADER_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "productDefinitionTemplateNumber",
    "derivedForecast",
    "numberOfForecastsInEnsemble",
    "decimalScaleFactor",
    "binaryScaleFactor",
    "dataDate",
    "dataTime",
    "step",
)


def grib_header(message: bytes) -> dict[str, Any]:
    """Header keys that grib.decode_message does not expose (missing keys are None)."""
    eccodes = load_eccodes()

    try:
        handle = eccodes.codes_new_from_message(message)
    except Exception as exc:  # eccodes raises many internal error types
        raise GribError(f"cannot decode GRIB message: {exc}") from exc
    try:
        out: dict[str, Any] = {}
        for key in _HEADER_KEYS:
            try:
                out[key] = eccodes.codes_get(handle, key)
            except eccodes.KeyValueNotFoundError:
                out[key] = None
        return out
    finally:
        eccodes.codes_release(handle)


def packing_half_step(header: dict[str, Any]) -> float:
    """Half the quantisation step of a packed field: 0.5 * 2^E / 10^D (0.005 for GEFS)."""
    e = int(header.get("binaryScaleFactor") or 0)
    d = int(header.get("decimalScaleFactor") or 0)
    return 0.5 * 2.0**e / 10.0**d


def check_message(
    header: dict[str, Any],
    spec: FieldSpec,
    *,
    kind: str | None,
    cycle: datetime,
    fhour: int,
    where: str,
) -> None:
    """Fail loudly if a record is not the field, statistic, cycle and step we asked for."""
    triple = (header["discipline"], header["parameterCategory"], header["parameterNumber"])
    if triple != spec.param:
        raise GefsLayoutError(
            f"{where}: {spec.variable} decodes to GRIB2 parameter {triple}, expected {spec.param}"
        )
    if kind is not None:
        derived = STAT_KINDS[kind]
        if (
            header["productDefinitionTemplateNumber"] != 2
            or header["derivedForecast"] != derived
            or not header["numberOfForecastsInEnsemble"]
        ):
            raise GefsLayoutError(
                f"{where}: {kind} record has template {header['productDefinitionTemplateNumber']}"
                f", derivedForecast {header['derivedForecast']}, expected 2/{derived}"
            )
    if header["step"] is None or int(header["step"]) != fhour:
        raise GribError(f"{where}: message is for step {header['step']}, expected {fhour}")
    if header["dataDate"] != int(f"{cycle:%Y%m%d}") or header["dataTime"] != cycle.hour * 100:
        raise GribError(
            f"{where}: message is for cycle {header['dataDate']} {header['dataTime']}, "
            f"expected {cycle:%Y%m%d %H%M}"
        )


def check_partition_order(first: np.ndarray, second: np.ndarray) -> dict[str, Any]:
    """Physical check that the first SWELL record really is partition 1.

    WW3 ranks swell partitions by height in every member, so the mean of partition 1 is at
    least the mean of partition 2 almost everywhere (see PARTITION_ORDER_MIN_FRACTION). A
    swapped or reshuffled layout fails this check even if the ``.idx`` text looks right.
    """
    ok = ~np.isnan(first) & ~np.isnan(second)
    cells = int(ok.sum())
    if cells == 0:
        return {"verified": False, "cells": 0, "fraction": None}
    fraction = float(np.mean(first[ok] >= second[ok] - PARTITION_ORDER_TOLERANCE_M))
    if fraction < PARTITION_ORDER_MIN_FRACTION:
        raise GefsLayoutError(
            f"first SWELL record >= second on only {fraction:.1%} of {cells} cells; the "
            "mean-file partition order is not the verified one"
        )
    return {"verified": True, "cells": cells, "fraction": round(fraction, 5)}


def cv_rules_out_zero_fill(
    mean: float | None,
    spread: float | None,
    *,
    members: int,
    mean_half_step: float,
    spread_half_step: float,
) -> bool | None:
    """True when one zero-filled mean/spread pair proves no member was missing.

    Members without a partition enter the published statistics as 0. If only a fraction
    f < 1 of the n members have the partition, with valid-member mean m and variance s^2,
    the zero-filled statistics satisfy (spread/mean)^2 = s^2/(f m^2) + (1-f)/f >= (1-f)/f
    >= 1/(n-1). So an upper bound of the published ratio below sqrt(1/(n-1)) (0.183 for
    31 members) means f = 1; the bound widens both values by their packing half step.
    False means "not provable"; None means there is no value.
    """
    if mean is None or spread is None or members < 2:
        return None
    if mean <= mean_half_step:
        return False
    upper = (spread + spread_half_step + 1e-4) / (mean - mean_half_step)
    return upper < math.sqrt(1.0 / (members - 1))


def swell1_complete_certain(
    values: Mapping[str, float | None], half_steps: Mapping[str, float], *, members: int
) -> bool | None:
    """True when the published statistics prove every member had swell partition 1.

    A member lacks SWELL and SWPER partition 1 together (identical missing cells in the
    member and statistics files), so either the height or the period pair can prove it
    with :func:`cv_rules_out_zero_fill`. False means "not provable": members may be
    missing (zero-filled), or the members genuinely disagree, typically when two swells
    of similar height swap rank and partition 1 alternates between them. On 2026-10-10 12Z
    f024 this certified 94.5% of the cells where all 31 members had the partition and
    none of the 59,877 cells where some lacked it.
    """
    results = [
        cv_rules_out_zero_fill(
            values.get(f"{column}_mean_{unit}"),
            values.get(f"{column}_spread_{unit}"),
            members=members,
            mean_half_step=half_steps.get(f"{column}:mean", 0.0),
            spread_half_step=half_steps.get(f"{column}:spread", 0.0),
        )
        for column, unit in (("swell1_height", "m"), ("swell1_period", "s"))
    ]
    if any(r is True for r in results):
        return True
    if all(r is None for r in results):
        return None
    return False


def member_quantiles(values: Sequence[float]) -> dict[str, Any]:
    """Statistics of the members' HTSGW at one cell (quantiles: numpy "linear" method)."""
    out: dict[str, Any] = {"hs_members": len(values)}
    if not values:
        return out
    arr = np.asarray(values, dtype="float64")
    out["hs_members_mean_m"] = round(float(arr.mean()), 3)
    out["hs_members_std_m"] = round(float(arr.std(ddof=0)), 3)
    if len(values) >= MIN_MEMBERS_FOR_QUANTILES:
        p10, p50, p90 = (float(q) for q in np.quantile(arr, QUANTILES))
        out.update(
            hs_p10_m=round(p10, 3),
            hs_p50_m=round(p50, 3),
            hs_p90_m=round(p90, 3),
            hs_quantile_method="linear",
        )
    return out


def forecast_steps(max_lead_hours: int, step_hours: int) -> tuple[list[int], list[int]]:
    """Steps 0..max every ``step_hours``, split into (published, never published)."""
    wanted = range(0, min(max_lead_hours, MAX_LEAD_HOURS) + 1, step_hours)
    return [h for h in wanted if h in PUBLISHED_STEPS], [
        h for h in wanted if h not in PUBLISHED_STEPS
    ]


# --------------------------------------------------------------------------- sampling
@dataclass(frozen=True)
class GefsPaths:
    base_url: str

    def file(self, cycle: datetime, kind: str, fhour: int) -> str:
        hh = f"{cycle:%H}"
        return (
            f"{self.base_url.rstrip('/')}/gefs.{cycle:%Y%m%d}/{hh}/wave/gridded/"
            f"gefs.wave.t{hh}z.{kind}.global.0p25.f{fhour:03d}.grib2"
        )

    def shorten(self, url: str) -> str:
        return url.removeprefix(self.base_url.rstrip("/") + "/")


@dataclass(frozen=True)
class SpotCell:
    choice: CellChoice
    basis: str  # "forecast_point" or "break"

    def as_dict(self) -> dict[str, Any]:
        return {**self.choice.as_dict(), "basis": self.basis}


class CellPicker:
    """Chooses each spot's cell once, on the first decoded HTSGW-mean grid of the run.

    The nearest valid (not NaN) cell to the spot's forecast point, else to the break,
    within ``max_distance_km`` (grib.PointSampler). Every later grid must have the same
    geometry, so all steps and members are sampled at the same cell. Thread-safe.
    """

    def __init__(self, spots: Sequence[SpotPoint], max_distance_km: float) -> None:
        self.spots = list(spots)
        self.sampler = PointSampler(max_distance_km)
        self.geometry: tuple[float, ...] | None = None
        self.cells: dict[int, SpotCell | None] = {}
        self._lock = threading.Lock()

    def cells_for(self, grid: GridField) -> dict[int, SpotCell | None]:
        with self._lock:
            if self.geometry is None:
                self.geometry = grid.geometry_key()
                for s in self.spots:
                    basis = "forecast_point"
                    choice = self.sampler.cell(
                        grid, (s.spot_id, basis), s.forecast_latitude, s.forecast_longitude
                    )
                    if choice is None:
                        basis = "break"
                        choice = self.sampler.cell(
                            grid, (s.spot_id, basis), s.latitude, s.longitude
                        )
                    self.cells[s.spot_id] = None if choice is None else SpotCell(choice, basis)
            else:
                self.check_geometry(grid)
            return self.cells

    def check_geometry(self, grid: GridField) -> None:
        if self.geometry is not None and grid.geometry_key() != self.geometry:
            raise GefsLayoutError(
                f"grid geometry {grid.geometry_key()} differs from {self.geometry} within one run"
            )


@dataclass
class StepValues:
    """Everything read for one forecast step (values keyed by spot id)."""

    fhour: int
    valid_time: datetime
    values: dict[int, dict[str, float | None]] = field(default_factory=dict)
    extras: dict[int, dict[str, Any]] = field(default_factory=dict)
    members_in_stats: int | None = None
    member_files_read: int | None = None
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    bytes_downloaded: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class EnsembleFetchResult:
    rows: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    cells: dict[int, dict[str, Any]] = field(default_factory=dict)
    steps_requested: list[int] = field(default_factory=list)
    steps_unpublished: list[int] = field(default_factory=list)
    steps_read: list[int] = field(default_factory=list)
    steps_failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    bytes_downloaded: int = 0


# --------------------------------------------------------------------------- provider
class GefsWaveEnsembleProvider(ForecastProvider):
    """GEFS-Wave mean/spread (and optionally member HTSGW) for every spot.

    Subclasses ForecastProvider for ``source_info``/``current_run`` (and ingestion's
    ``ensure_source``); its output is ensemble statistics, so :meth:`fetch` refuses and
    :meth:`fetch_stats` returns rows for ``forecast_ensemble_stats``.
    """

    code = GEFS_WAVE_CODE

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        members: bool = False,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        )
        self.read_members = members
        self.backoff_seconds = backoff_seconds
        self.paths = GefsPaths(self.settings.noaa_gefs_base_url)
        self.probe_requests = 0
        self.stat_fields = stat_fields_for(self.settings.gefs_stat_field_list)
        # The partition-order check needs swell partition 2, read only with SWELL.
        self.check_partitions = SWELL1 in self.stat_fields

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="NOAA GEFS-Wave ensemble mean and spread (direct from NOAA open data)",
            provider="noaa_nodd",
            wave_model="GEFS-Wave WW3 global.0p25 (31 members)",
            atmosphere_model="GEFS ensemble (31 members)",
            is_demo=False,
            attribution=(
                "NOAA/NCEP Global Ensemble Forecast System wave component (GEFS-Wave, "
                "WAVEWATCH III), via the NOAA Open Data Dissemination (NODD) program."
            ),
            license_notes=(
                "US government work. NODD terms: data are open to the public and can be used "
                "as desired; NOAA requests attribution; do not state or imply endorsement by "
                "or affiliation with NOAA. Values here are sampled, rounded and converted "
                "(km/h), so they must not be presented as original, unaltered NOAA data."
            ),
            family="ncep_gefs",
            kind="ensemble",
        )

    def steps(self) -> tuple[list[int], list[int]]:
        return forecast_steps(self.settings.gefs_max_lead_hours, self.settings.gefs_step_hours)

    def _published(self, url: str) -> bool:
        """True if ``url`` exists. Network failures raise instead of meaning "not yet"."""
        self.probe_requests += 1
        response = request_with_retries(
            self.client, "HEAD", url, provider=HTTP_PROVIDER, backoff_seconds=self.backoff_seconds
        )
        if response.status_code == 200:
            return True
        if response.status_code in (403, 404):  # S3 answers 403 for some missing keys
            return False
        raise PermanentProviderError(f"{HTTP_PROVIDER}: HTTP {response.status_code} for {url}")

    def current_run(self, now: datetime) -> RunInfo:
        """Newest 00/06/12/18Z cycle whose mean-file ``.idx`` exists for the last needed step.

        NCEP writes all mean/spread files of a cycle in one burst after the members
        (cycle + 6.8 to 7.2 h on 2026-10-09/10), so the last step's ``.idx`` marks a
        complete cycle; GEFS_AVAILABILITY_LAG_HOURS picks the first cycle to probe.
        """
        hours, _ = self.steps()
        last = hours[-1]
        nominal = nominal_gfs_cycle(now, self.settings.gefs_availability_lag_hours)
        cycles = self.settings.gefs_cycle_hours  # GEFS_CYCLES
        candidates = [
            c
            for back in range(4 * (OLDER_CYCLES_TO_TRY + 1))
            if (c := nominal - timedelta(hours=CYCLE_HOURS * back)).hour in cycles
        ][: OLDER_CYCLES_TO_TRY + 1]
        tried: list[str] = []
        for cycle in candidates:
            if self._published(self.paths.file(cycle, "mean", last) + ".idx"):
                return RunInfo(
                    run_key=f"gefs-wave:{cycle:%Y%m%d%H}",
                    issued_at=cycle,
                    wave_model_run_at=cycle,
                    atmosphere_model_run_at=cycle,
                    metadata_source="noaa-s3-idx",
                )
            tried.append(f"{cycle:%Y%m%d%H}Z")
        raise TransientProviderError(
            f"no GEFS-Wave cycle has its mean f{last:03d} published (tried {', '.join(tried)})"
        )

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        raise TypeError(
            "GEFS-Wave statistics are not wave forecasts; use fetch_stats()/acquire_ensemble()"
        )

    # ------------------------------------------------------------------ reading
    def _read_stats(
        self,
        fetcher: GribFetcher,
        cycle: datetime,
        step: StepValues,
        picker: CellPicker,
        check_order: bool,
    ) -> None:
        fhour = step.fhour
        cells: dict[int, SpotCell | None] = {}
        swell1_mean: np.ndarray | None = None
        half_steps: dict[str, float] = {}
        for kind in STAT_KINDS:
            url = self.paths.file(cycle, kind, fhour)
            where = self.paths.shorten(url)
            picked = select_stat_records(
                fetcher.text(url + ".idx"),
                kind,
                with_swell2=check_order and kind == "mean" and self.check_partitions,
                where=where,
                fields=self.stat_fields,
            )
            blobs = fetcher.ranges(url, list(picked.values()))
            for (name, entry), blob in zip(picked.items(), blobs, strict=True):
                spec = SPECS[name]
                rec = f"{where} record {entry.number} ({entry.key})"
                header = grib_header(blob)
                check_message(header, spec, kind=kind, cycle=cycle, fhour=fhour, where=rec)
                grid = decode_message(blob)
                if grid.units != spec.units:
                    raise GefsLayoutError(f"{rec}: units {grid.units!r}, expected {spec.units!r}")
                if spec is HS and kind == "mean":
                    cells = picker.cells_for(grid)  # HTSGW is the first record read
                    step.members_in_stats = int(header["numberOfForecastsInEnsemble"])
                else:
                    picker.check_geometry(grid)
                if spec is SWELL1 and kind == "mean" and check_order:
                    swell1_mean = grid.values
                if spec is SWELL2:
                    if swell1_mean is None:  # defensive: SWELL1 precedes SWELL2 in SPECS
                        raise GefsLayoutError(f"{rec}: partition 2 read before partition 1")
                    step.checks["partition_order"] = {
                        **check_partition_order(swell1_mean, grid.values),
                        "step": f"f{fhour:03d}",
                    }
                    continue
                if spec in (SWELL1, SWPER1):
                    half_steps[f"{name}:{kind}"] = packing_half_step(header)
                column = COLUMNS[(name, kind)]
                for spot_id, cell in cells.items():
                    if cell is None:
                        continue
                    v = grid.value(cell.choice.j, cell.choice.i)
                    if v is not None:
                        if kind == "spread" and v < 0:
                            raise GribError(f"{rec}: negative spread {v} at spot {spot_id}")
                        v = round(v * MS_TO_KMH, 2) if spec is WIND else round(v, 3)
                    step.values.setdefault(spot_id, {})[column] = v
        if SWELL1 not in self.stat_fields and SWPER1 not in self.stat_fields:
            return
        members = step.members_in_stats or len(MEMBERS)
        for spot_id, vals in step.values.items():
            step.extras.setdefault(spot_id, {})["swell1_complete_certain"] = (
                swell1_complete_certain(vals, half_steps, members=members)
            )

    def _read_members(
        self, fetcher: GribFetcher, cycle: datetime, step: StepValues, picker: CellPicker
    ) -> None:
        """HTSGW of each of the 31 member files at the spots' cells (one record per file)."""
        fhour = step.fhour
        cells = {k: v for k, v in picker.cells.items() if v is not None}
        values: dict[int, list[float]] = {spot_id: [] for spot_id in cells}
        unreadable: list[str] = []
        for member in MEMBERS:
            url = self.paths.file(cycle, member, fhour)
            where = self.paths.shorten(url)
            try:
                found = [
                    e for e in parse_ncep_idx(fetcher.text(url + ".idx")) if e.variable == "HTSGW"
                ]
                if len(found) != 1 or found[0].level != "surface":
                    keys = [e.key for e in found]
                    raise GefsLayoutError(f"{where}: expected one HTSGW:surface record, got {keys}")
                blob = fetcher.ranges(url, found)[0]
                header = grib_header(blob)
                check_message(header, HS, kind=None, cycle=cycle, fhour=fhour, where=where)
                grid = decode_message(blob)
                picker.check_geometry(grid)
            except GefsLayoutError:
                raise
            except ProviderError as exc:
                unreadable.append(f"{member}: {exc}")
                continue
            for spot_id, cell in cells.items():
                v = grid.value(cell.choice.j, cell.choice.i)
                if v is not None:
                    values[spot_id].append(v)
        step.member_files_read = len(MEMBERS) - len(unreadable)
        if unreadable:
            step.warnings.append(
                f"f{fhour:03d}: {len(unreadable)} of {len(MEMBERS)} member files unreadable "
                f"({unreadable[0]})"
            )
        for spot_id, vals in values.items():
            if spot_id in step.values:
                step.extras.setdefault(spot_id, {}).update(member_quantiles(vals))

    def read_step(
        self, cycle: datetime, fhour: int, picker: CellPicker, *, check_order: bool = False
    ) -> StepValues:
        """Read one step. Transport/decoding failures become ``failed`` (a reported gap);
        layout errors propagate and abort the run."""
        step = StepValues(fhour, cycle + timedelta(hours=fhour))
        fetcher = GribFetcher(self.client, HTTP_PROVIDER, backoff_seconds=self.backoff_seconds)
        try:
            self._read_stats(fetcher, cycle, step, picker, check_order)
            if self.read_members:
                self._read_members(fetcher, cycle, step, picker)
        except GefsLayoutError:
            raise
        except ProviderError as exc:
            step.failed.append(str(exc))
            step.values.clear()  # mean and spread of a step are stored together or not at all
            step.extras.clear()
        finally:
            step.request_count = fetcher.request_count
            step.bytes_downloaded = fetcher.bytes_downloaded
        return step

    def _read_parallel(
        self, cycle: datetime, hours: Sequence[int], picker: CellPicker
    ) -> list[StepValues]:
        if not hours:
            return []
        workers = max(1, self.settings.grib_download_workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gefs") as pool:
            futures = [pool.submit(self.read_step, cycle, h, picker) for h in hours]
            try:
                return [f.result() for f in futures]
            except BaseException:
                for f in futures:
                    f.cancel()
                raise

    def fetch_stats(self, spots: Sequence[SpotPoint], run: RunInfo) -> EnsembleFetchResult:
        result = EnsembleFetchResult()
        hours, unpublished = self.steps()
        result.steps_requested, result.steps_unpublished = hours, unpublished
        if not spots:
            return result
        cycle = run.wave_model_run_at or run.issued_at
        # File paths and the message checks use the cycle's UTC date and hour (a naive time
        # is taken to be UTC already).
        cycle = cycle.replace(tzinfo=UTC) if cycle.tzinfo is None else cycle.astimezone(UTC)
        picker = CellPicker(spots, self.settings.grib_max_cell_distance_km)
        # The first readable step fixes the cells and runs the partition-order check.
        steps: list[StepValues] = []
        for fhour in hours[:ANCHOR_ATTEMPTS]:
            steps.append(self.read_step(cycle, fhour, picker, check_order=True))
            if steps[-1].ok:
                break
        if not steps[-1].ok:
            raise TransientProviderError(
                f"{run.run_key}: none of the first {len(steps)} GEFS-Wave steps could be read "
                f"({steps[0].failed[0]})"
            )
        anchor = steps[-1]
        steps.extend(self._read_parallel(cycle, [h for h in hours if h > anchor.fhour], picker))
        return self._assemble(spots, run, steps, picker, result)

    def _assemble(
        self,
        spots: Sequence[SpotPoint],
        run: RunInfo,
        steps: list[StepValues],
        picker: CellPicker,
        result: EnsembleFetchResult,
    ) -> EnsembleFetchResult:
        result.request_count = sum(s.request_count for s in steps)
        result.bytes_downloaded = sum(s.bytes_downloaded for s in steps)
        result.steps_read = [s.fhour for s in steps if s.ok]
        result.steps_failed = {
            f"f{s.fhour:03d}": "; ".join(s.failed)[:500] for s in steps if not s.ok
        }
        good = [s for s in steps if s.ok]
        result.checks = {
            "members_in_stats": sorted({s.members_in_stats for s in good if s.members_in_stats}),
            **next((s.checks for s in good if "partition_order" in s.checks), {}),
        }
        km = self.settings.grib_max_cell_distance_km
        member_mean_diff = 0.0
        spot_warnings: list[str] = []
        for spot in spots:
            cell = picker.cells.get(spot.spot_id)
            if cell is None:
                result.errors[spot.spot_id] = (
                    f"no valid GEFS-Wave cell within {km:g} km of the forecast point "
                    f"({spot.forecast_latitude:.2f}, {spot.forecast_longitude:.2f}) or the break"
                )
                continue
            result.cells[spot.spot_id] = cell.as_dict()
            rows: list[dict[str, Any]] = []
            empty = 0
            for s in good:
                vals = s.values.get(spot.spot_id, {})
                if vals.get("hs_mean_m") is None and vals.get("hs_spread_m") is None:
                    empty += 1  # masked at this step (e.g. sea ice), not a fetch failure
                    continue
                extra: dict[str, Any] = {
                    "period_kind": PERIOD_KIND,
                    "cell_km": round(cell.choice.distance_km, 1),
                    **s.extras.get(spot.spot_id, {}),
                }
                if extra.get("hs_members") == s.members_in_stats and vals.get("hs_mean_m"):
                    diff = abs(extra["hs_members_mean_m"] - vals["hs_mean_m"])
                    member_mean_diff = max(member_mean_diff, diff)
                rows.append(
                    {
                        "spot_id": spot.spot_id,
                        "valid_time": s.valid_time,
                        "lead_hours": float(s.fhour),
                        "members": s.members_in_stats,
                        **{c: vals.get(c) for c in STAT_COLUMNS},
                        "extra": extra,
                    }
                )
            if empty:
                spot_warnings.append(
                    f"{spot.slug}: {empty} of {len(good)} steps masked at the chosen cell"
                )
            if rows:
                result.rows[spot.spot_id] = rows
            else:
                result.errors[spot.spot_id] = "no valid ensemble value at the chosen cell"
        if self.read_members:
            result.checks["member_mean_vs_published_max_diff_m"] = round(member_mean_diff, 3)
        mb = result.bytes_downloaded / 1e6
        result.warnings.append(
            f"downloaded {mb:.1f} MB in {result.request_count} requests for {len(steps)} steps "
            f"({mb / max(len(steps), 1):.2f} MB per step; records "
            f"{','.join(spec.variable for spec in self.stat_fields)}; members={self.read_members})"
        )
        if result.steps_unpublished:
            result.warnings.append(
                "not published by NOAA, skipped: "
                + ", ".join(f"f{h:03d}" for h in result.steps_unpublished)
            )
        if result.steps_failed:
            result.warnings.append(
                f"{len(result.steps_failed)} of {len(steps)} steps unreadable: "
                + ", ".join(result.steps_failed)
            )
        if self.check_partitions and not result.checks.get("partition_order", {}).get("verified"):
            result.warnings.append("partition order could not be checked (no overlapping cells)")
        result.warnings.extend(w for s in steps for w in s.warnings)
        result.warnings.extend(spot_warnings)
        result.warnings.extend(f"{k}: {v}" for k, v in result.steps_failed.items())
        logger.info(
            "GEFS-Wave %s: %d spots, %d errors, %d/%d steps, %d requests, %.1f MB",
            run.run_key,
            len(result.rows),
            len(result.errors),
            len(good),
            len(steps),
            result.request_count,
            mb,
        )
        return result


# --------------------------------------------------------------------------- job
def store_stats(db: Session, run: ForecastRun, rows: dict[int, list[dict[str, Any]]]) -> int:
    """Replace the run's rows (a retried or forced run never keeps rows of an old attempt)."""
    db.execute(delete(ForecastEnsembleStat).where(ForecastEnsembleStat.run_id == run.id))
    flat = [{"run_id": run.id, **row} for items in rows.values() for row in items]
    stmt = insert(ForecastEnsembleStat)
    for i in range(0, len(flat), INSERT_CHUNK):
        db.execute(stmt, flat[i : i + INSERT_CHUNK])  # executemany: compiled once, batched
    return len(flat)


def acquire_ensemble(
    db: Session,
    now: datetime | None = None,
    force: bool = False,
    *,
    members: bool = False,
    provider: GefsWaveEnsembleProvider | None = None,
) -> dict[str, Any]:
    """Job: GEFS-Wave mean/spread for the newest complete cycle (Celery
    ``forecasts.acquire_ensemble``).

    Run bookkeeping mirrors ingestion.acquire_from_provider: one run per ``run_key``; a
    SUCCESS/PARTIAL run is skipped, a RUNNING run younger than STUCK_RUN_AFTER is skipped,
    anything else (FAILED, stuck) is reset and fetched again; ``force`` re-fetches anyway
    and also overrides GEFS_ENABLED=false. Failures are recorded on the run and returned
    (status "failed"), not raised, like the forecast acquisition job.
    """
    settings = get_settings()
    if not settings.gefs_enabled and not force:
        return {"skipped": "GEFS_ENABLED is false"}
    now = now or utcnow()
    provider = provider or GefsWaveEnsembleProvider(settings, members=members)
    source = ensure_source(db, provider)
    db.commit()
    out: dict[str, Any] = {"source": source.code, "members_mode": provider.read_members}
    try:
        info = provider.current_run(now)
    except ProviderError as exc:
        logger.error("Could not determine current run for %s: %s", source.code, exc)
        return {
            **out,
            "run_key": None,
            "status": "failed",
            "message": str(exc),
            "retryable": exc.transient,
        }
    out["run_key"] = info.run_key

    run = db.scalar(
        select(ForecastRun).where(
            ForecastRun.source_id == source.id, ForecastRun.run_key == info.run_key
        )
    )
    if run is not None and not force:
        if run.status in USABLE:
            return {**out, "status": "skipped", "run_id": run.id, "message": "run already ingested"}
        if run.status == ForecastRunStatus.RUNNING and utcnow() - run.started_at < STUCK_RUN_AFTER:
            return {**out, "status": "skipped", "run_id": run.id, "message": "run in progress"}
    if run is None:
        run = ForecastRun(source_id=source.id, run_key=info.run_key, details={})
        db.add(run)
    run.issued_at = info.issued_at
    run.wave_model_run_at = info.wave_model_run_at
    run.atmosphere_model_run_at = info.atmosphere_model_run_at
    run.started_at = utcnow()
    run.completed_at = None
    run.status = ForecastRunStatus.RUNNING
    run.error_message = None
    run.spot_count = run.record_count = run.request_count = 0
    # No wave_forecasts to score: never "pending" for compute_pending_predictions.
    run.predictions_computed_at = now
    run.details = {
        "metadata_source": info.metadata_source,
        "model_runs": [
            {"model": "gefs_wave", "init_time": info.issued_at.isoformat(), "available_at": None}
        ],
        "kind": "ensemble",
        "members_mode": provider.read_members,
    }
    db.commit()
    out["run_id"] = run.id

    points = spot_points(db)
    try:
        fetched = provider.fetch_stats(points, info)
    except Exception as exc:  # provider errors and bugs alike end this run, never RUNNING
        db.rollback()
        transient = isinstance(exc, ProviderError) and exc.transient
        message = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
        failed = db.get(ForecastRun, out["run_id"])
        if failed is not None:
            failed.status = ForecastRunStatus.FAILED
            failed.error_message = message[:2000]
            failed.completed_at = utcnow()
            failed.details = {**(failed.details or {}), "retryable": transient}
            db.commit()
        logger.error("GEFS-Wave ensemble fetch failed for %s: %s", info.run_key, message)
        return {
            **out,
            "status": "failed",
            "message": message[:2000],
            "retryable": transient,
            "retry_after_s": getattr(exc, "retry_after_s", None) if transient else None,
        }

    count = store_stats(db, run, fetched.rows)
    slugs = {p.spot_id: p.slug for p in points}
    run.record_count = count
    run.spot_count = len(fetched.rows)
    run.request_count = fetched.request_count + provider.probe_requests
    run.details = {
        **run.details,
        "spot_ids": sorted(fetched.rows),
        "spot_errors": {slugs.get(k, str(k)): v for k, v in fetched.errors.items()},
        "cells": {slugs.get(k, str(k)): v for k, v in fetched.cells.items()},
        "steps_requested": len(fetched.steps_requested),
        "steps_read": len(fetched.steps_read),
        "steps_failed": fetched.steps_failed,
        "steps_unpublished": fetched.steps_unpublished,
        "bytes_downloaded": fetched.bytes_downloaded,
        "stats": {
            "bytes_downloaded": fetched.bytes_downloaded,
            "requests": fetched.request_count + provider.probe_requests,
            "http_status_counts": {},
            "weighted_calls": None,
        },
        "stat_fields": [spec.variable for spec in provider.stat_fields],
        "probe_requests": provider.probe_requests,
        "checks": fetched.checks,
        "period_kinds": {"tp_mean_s": "tp", "swell1_period_mean_s": "tp (partition peak)"},
        "swell_partition_rule": (
            "mean/spread files code both swell partitions as 'surface'; partition 1 = first "
            "SWELL/SWPER record; members without a partition count as 0 in those statistics"
        ),
        "warnings": fetched.warnings[:50],
    }
    if not fetched.rows:
        run.status = ForecastRunStatus.FAILED
        run.error_message = "no spot returned usable ensemble data"
    elif fetched.errors or fetched.steps_failed:
        run.status = ForecastRunStatus.PARTIAL
    else:
        run.status = ForecastRunStatus.SUCCESS
    run.completed_at = utcnow()
    run.predictions_computed_at = run.completed_at
    db.commit()
    logger.info(
        "Ingested %s run %s: %d ensemble rows for %d spots (%d errors, %d steps failed)",
        source.code,
        info.run_key,
        count,
        run.spot_count,
        len(fetched.errors),
        len(fetched.steps_failed),
    )
    return {
        **out,
        "status": run.status.value,
        "records": count,
        "spots": run.spot_count,
        "spot_errors": run.details["spot_errors"],
        "steps_read": len(fetched.steps_read),
        "steps_failed": sorted(fetched.steps_failed),
        "requests": run.request_count,
        "bytes_downloaded": fetched.bytes_downloaded,
        "megabytes": round(fetched.bytes_downloaded / 1e6, 1),
        "checks": fetched.checks,
    }


# --------------------------------------------------------------------------- reading
def _nearest(
    rows: Sequence[ForecastEnsembleStat], stamps: Sequence[datetime], t: datetime
) -> ForecastEnsembleStat:
    """Row nearest to ``t`` among time-sorted ``rows`` (ties: the earlier row)."""
    k = bisect_left(stamps, t)
    near = rows[max(k - 1, 0) : k + 1]  # the rows either side of t
    return min(near, key=lambda r: (abs(r.valid_time - t), r.valid_time))


def ensemble_for(
    db: Session,
    spot_id: int,
    valid_times: Sequence[datetime],
    max_gap_hours: float = 3.0,
) -> dict[datetime, ForecastEnsembleStat]:
    """Ensemble statistics for ``spot_id`` at each requested (timezone-aware) time.

    For each time, runs are tried newest first (``issued_at``, then id; SUCCESS or
    PARTIAL only). A run answers a time only if the time lies inside the span of that
    run's rows for the spot (no extrapolation past its first or last step) and its
    nearest row is at most ``max_gap_hours`` away (ties go to the earlier row); otherwise
    an older run is tried (e.g. for a step missing from a partial run). Times nobody
    covers are absent from the result. Values are not interpolated.
    """
    times = list(valid_times)
    if not times:
        return {}
    if any(t.tzinfo is None for t in times):
        raise ValueError("ensemble_for needs timezone-aware valid_times")
    gap = timedelta(hours=max_gap_hours)
    lo, hi = min(times) - gap, max(times) + gap
    run_ids = list(
        db.scalars(
            select(ForecastRun.id)
            .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
            .where(
                ForecastSource.code == GEFS_WAVE_CODE,
                ForecastRun.status.in_(USABLE),
                ForecastRun.issued_at <= hi,
                ForecastRun.issued_at >= lo - timedelta(hours=MAX_LEAD_HOURS),
            )
            .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
            .limit(MAX_RUNS_SCANNED)
        ).all()
    )
    if not run_ids:
        return {}
    spans = {
        rid: (first, last)
        for rid, first, last in db.execute(
            select(
                ForecastEnsembleStat.run_id,
                func.min(ForecastEnsembleStat.valid_time),
                func.max(ForecastEnsembleStat.valid_time),
            )
            .where(
                ForecastEnsembleStat.run_id.in_(run_ids), ForecastEnsembleStat.spot_id == spot_id
            )
            .group_by(ForecastEnsembleStat.run_id)
        ).all()
    }
    series: dict[int, list[ForecastEnsembleStat]] = {}
    for row in db.scalars(
        select(ForecastEnsembleStat)
        .where(
            ForecastEnsembleStat.run_id.in_(list(spans)),
            ForecastEnsembleStat.spot_id == spot_id,
            ForecastEnsembleStat.valid_time.between(lo, hi),
        )
        .order_by(ForecastEnsembleStat.run_id, ForecastEnsembleStat.valid_time)
    ):
        series.setdefault(row.run_id, []).append(row)
    stamps = {rid: [r.valid_time for r in rows] for rid, rows in series.items()}

    result: dict[datetime, ForecastEnsembleStat] = {}
    for t in times:
        for rid in run_ids:
            span = spans.get(rid)
            if span is None or not span[0] <= t <= span[1] or rid not in series:
                continue
            best = _nearest(series[rid], stamps[rid], t)
            if abs(best.valid_time - t) <= gap:
                result[t] = best
                break
    return result
