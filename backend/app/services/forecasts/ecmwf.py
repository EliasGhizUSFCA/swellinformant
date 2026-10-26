"""ECMWF IFS open data (HRES, 0.25°): ecWAM waves plus IFS 10 m wind, gust and MSL pressure.

This is the independent second model family (``ecmwf_ifs``): different wave model
(ecWAM), different atmosphere (IFS) and altimeter assimilation, so its agreement with the
NOAA chain is real evidence (see :mod:`app.services.forecasts.families`).

Files (one per forecast step, each with a JSON-lines ``.index`` holding ``_offset`` and
``_length`` per GRIB2 message; forecast-engine research notes, ECMWF section, re-checked
against the live bucket on 2026-10-10)::

    {ECMWF_BASE_URL}/{YYYYMMDD}/{HH}z/ifs/0p25/wave/{YYYYMMDD}{HH}0000-{step}h-wave-fc.grib2
    {ECMWF_BASE_URL}/{YYYYMMDD}/{HH}z/ifs/0p25/oper/{YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2

Only the 00Z and 12Z cycles are used: they run to 360 h, 3-hourly to 144 h and 6-hourly
after (85 steps; the 06Z/18Z cycles stop at 144 h). A wave file is ~10.9 MB and an
atmosphere file ~145 MB, so only the needed messages are fetched by HTTP ``Range``:

* wave: ECMWF_WAVE_PARAMS, by default ``swh``, ``mwd``, ``pp1d`` and ``mwp``: one
  contiguous 3.69 MB range per step in the live layout (indexes of 2026-10-13), instead of
  the 10.06 MB of all 12 wave parameters. ``wmb`` (model depth, a static field) is read on
  the first wave step of a run only. The period-band heights ``h1012`` … ``h2530`` and
  ``mp2`` are read only when listed in ECMWF_WAVE_PARAMS (nothing in the app uses them; they
  go to ``extra``). This provider is an optional, key-free fallback: the recommended setup
  reads ECMWF through Open-Meteo (``open_meteo_ecmwf``).
* atmosphere: ``10u``, ``10v``, ``10fg``, ``msl`` (four ~0.5–1.4 MB requests per step) and
  ``lsm`` once per run (0.19 MB).

Conventions (checked by decoding live messages on 2026-10-10):

* ``swh`` "Significant height of combined wind waves and swell" (m) is the OFFSHORE
  significant wave height of the whole sea state at the wave-model cell. It is not a surf
  (breaking) height; the nearshore model derives that later.
* ``pp1d`` "Peak wave period" (s) → ``wave_period_s`` with ``period_kind="tp"``.
* ``mwd`` "Mean wave direction" (degree true) is the direction waves come FROM (ECMWF
  convention; the research notes' 46026 example has ECMWF mwd 285° next to GFS-Wave
  SWELL1 288°, both FROM), so it is stored unchanged modulo 360 (packing gives 360.002).
* ``mwp`` is the mean period Tm-1,0 (energy period) and ``mp2`` the mean zero-crossing
  period Tm02; both go to ``extra``, never to ``wave_period_s``.
* ``h1012`` … ``h2530`` are "Significant wave height of all waves with periods within the
  inclusive range from 10 to 12 seconds" (etc.), in metres. Energy adds, so the height of
  several bands together is the root of the sum of squares; ``hs_ge_12s_m`` (derived, not
  an ECMWF field) is that value for the five bands from 12 to 30 s. The bands carry no
  direction.
* ECMWF open data has NO swell / wind-sea partitions: every partition field is left None.
* ``10u``/``10v`` (m/s, the direction the air moves TO) → speed in km/h (×3.6) and the
  nautical direction it blows FROM; ``10fg`` is the "Maximum 10 metre wind gust since
  previous post-processing" (m/s): its window is read from each message (1 h at step 24,
  0 h at step 0, where the field is a constant 0 and is therefore dropped);
  ``msl`` Pa → hPa. ``lsm`` is the land fraction (0–1); wind is sampled at the nearest
  cell with ``lsm < 0.5`` to the break, waves at the nearest valid wave cell to the
  offshore forecast point. The grids start at longitude -180 (GRIB2 encodes it as 180).
* Not used: ``zos`` (ocean-model sea-surface height) has no tides, so ``sea_level_m``
  stays None rather than pretending to be a tide.

ECMWF's S3 bucket answers request bursts with HTTP 503 ``SlowDown`` (seen on most first
attempts from the shared egress used during development). The provider therefore keeps
at most four requests in flight, spaces request starts, retries with exponential backoff
and gives transient failures a second, sequential pass before reporting them.

Licence: CC BY 4.0 (the bucket's LICENCE.txt points to
https://apps.ecmwf.int/datasets/licences/general); see :data:`ATTRIBUTION`.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import numpy as np

from app.core.config import Settings, get_settings
from app.services.forecasts.base import (
    EXTRA_GUST_KIND,
    FORECAST_FIELDS,
    FetchResult,
    FetchStats,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
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
    load_eccodes,
    parse_ecmwf_index,
    select_entries,
)
from app.services.forecasts.registry import ECMWF_IFS_CODE
from app.services.geo import wind_from_uv
from app.services.http import (
    HttpTally,
    PermanentProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)

PROVIDER = "ecmwf_open_data"
RUN_KEY_PREFIX = "ecmwf-ifs"
CYCLE_HOURS = (0, 12)  # the long (360 h) cycles; 06Z/18Z stop at 144 h
LAST_3H_STEP = 144
LAST_PUBLISHED_STEP = 360
# How many past 00/12Z cycles current_run() probes before giving up.
CYCLE_CANDIDATES = 3

WAVE_STREAM = "wave"
ATMOS_STREAM = "oper"
LEVTYPE = "sfc"
BAND_PARAMS = {
    "h1012": "10-12s",
    "h1214": "12-14s",
    "h1417": "14-17s",
    "h1721": "17-21s",
    "h2125": "21-25s",
    "h2530": "25-30s",
}
LONG_PERIOD_BANDS = ("h1214", "h1417", "h1721", "h2125", "h2530")  # periods >= 12 s
# Every wave parameter this module can read (ECMWF_WAVE_PARAMS picks from these).
WAVE_PARAMS = ("swh", "pp1d", "mwd", "mwp", "mp2", *BAND_PARAMS, "wmb")
STATIC_WAVE_PARAMS = ("wmb",)  # fetched and decoded on the first wave step of a run only
REQUIRED_WAVE_PARAMS = ("swh",)
# ECMWF's 10fg is the "Maximum 10 metre wind gust since previous post-processing".
GUST_KIND = "ecmwf_max_since_pp"
ATMOS_PARAMS = ("10u", "10v", "10fg", "msl")
LSM_PARAM = "lsm"
SEA_MAX_LAND_FRACTION = 0.5  # lsm < 0.5 = sea
PERIOD_KIND = "tp"  # wave_period_s holds pp1d

# ECMWF S3 throttles bursts (HTTP 503 SlowDown): cap concurrency, space request starts
# and back off generously (sleeps ~2, 4, 8, 16, 30, 30 s between seven attempts).
MAX_WORKERS = 4
REQUEST_INTERVAL_SECONDS = 0.25
RETRY_ATTEMPTS = 7
BACKOFF_SECONDS = 2.0
# Steps processed one by one before going parallel, so the wave cells and the sea mask
# are chosen once (from the first step that has them) and reused by every worker.
WARMUP_STEPS = 3

LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = (
    "Contains modified ECMWF open data: Copyright European Centre for Medium-Range Weather "
    "Forecasts (ECMWF), IFS HRES and ecWAM real-time forecasts, licensed under CC BY 4.0 "
    f"({LICENSE_URL}). Modified by Swell Travel Agent: sampled to the nearest grid cell of "
    "each spot and converted to other units (wind m/s to km/h, pressure Pa to hPa); "
    "derived values such as hs_ge_12s_m are ours, not ECMWF's."
)
LICENSE_NOTES = (
    "ECMWF real-time open data (0.25 deg), Creative Commons Attribution 4.0 International "
    f"({LICENSE_URL}); terms referenced by the bucket's LICENCE.txt: "
    "https://apps.ecmwf.int/datasets/licences/general . Commercial use is allowed with "
    "attribution; indicate that the data were modified. ECMWF accepts no liability for "
    "the data or their use. No API key is needed."
)


# --------------------------------------------------------------------------- helpers
def cycle_steps(cycle_hour: int, max_lead_hours: int) -> list[int]:
    """Published forecast steps (hours) of a 00Z/12Z cycle up to ``max_lead_hours``.

    0–144 h every 3 h, then 150–360 h every 6 h (bucket listing of 2026-10-10, 00Z and
    12Z: 85 steps each).
    """
    if cycle_hour not in CYCLE_HOURS:
        raise ValueError(f"ECMWF provider only uses the 00Z/12Z cycles, not {cycle_hour:02d}Z")
    top = min(max_lead_hours, LAST_PUBLISHED_STEP)
    steps = list(range(0, min(top, LAST_3H_STEP) + 1, 3))
    steps += list(range(LAST_3H_STEP + 6, top + 1, 6))
    return steps


def candidate_cycles(
    now: datetime, lag_hours: float, count: int = CYCLE_CANDIDATES
) -> list[datetime]:
    """Newest-first 00/12Z cycles whose nominal availability (cycle + lag) has passed."""
    t = now.astimezone(UTC) - timedelta(hours=lag_hours)
    newest = t.replace(hour=(t.hour // 12) * 12, minute=0, second=0, microsecond=0)
    return [newest - timedelta(hours=12 * k) for k in range(count)]


def file_url(base_url: str, cycle: datetime, step: int, stream: str, ext: str = "grib2") -> str:
    day, hour = f"{cycle:%Y%m%d}", f"{cycle:%H}"
    return f"{base_url}/{day}/{hour}z/ifs/0p25/{stream}/{day}{hour}0000-{step}h-{stream}-fc.{ext}"


def combined_band_height(heights: Iterable[float | None]) -> float | None:
    """Significant height of several period bands together: sqrt(sum of Hs_band²)."""
    values = list(heights)
    if not values or any(h is None for h in values):
        return None
    return math.sqrt(sum(h * h for h in values if h is not None))


def step_window_hours(message: bytes) -> float | None:
    """Length of a statistically processed field's window (e.g. gust ``23-24`` → 1 h)."""
    eccodes = load_eccodes()

    try:
        handle = eccodes.codes_new_from_message(message)
    except Exception as exc:  # eccodes raises many internal error types
        raise GribError(f"cannot read GRIB step range: {exc}") from exc
    try:
        return float(eccodes.codes_get(handle, "endStep")) - float(
            eccodes.codes_get(handle, "startStep")
        )
    except eccodes.KeyValueNotFoundError:
        return None
    finally:
        eccodes.codes_release(handle)


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _bearing(value: float | None) -> float | None:
    """Degrees in [0, 360) rounded to 0.1 (packing gives e.g. 360.002 for north)."""
    return None if value is None else round(value % 360.0, 1) % 360.0


class _RequestPacer:
    """httpx request hook: spaces request starts at least ``interval`` s apart (all threads)."""

    def __init__(self, interval: float, sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = interval
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def __call__(self, request: httpx.Request) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            self._sleep(wait)


class _ResponseCounter:
    """httpx response hook: counts every HTTP attempt and the S3 throttling answers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.responses = 0
        self.http_503 = 0
        self.slowdown = 0

    def __call__(self, response: httpx.Response) -> None:
        slowdown = False
        if response.status_code == 503:
            response.read()
            slowdown = b"SlowDown" in response.content
        with self._lock:
            self.responses += 1
            if response.status_code == 503:
                self.http_503 += 1
                self.slowdown += int(slowdown)


@dataclass
class _RunState:
    """Per-run choices shared by the step workers (guarded by ``lock``)."""

    sampler: PointSampler
    lock: threading.Lock = field(default_factory=threading.Lock)
    wave_cells: dict[int, CellChoice | None] = field(default_factory=dict)
    wave_geometry: tuple[Any, ...] | None = None
    model_depth: dict[int, float | None] = field(default_factory=dict)
    sea_geometry: tuple[Any, ...] | None = None
    wind_cells: dict[int, CellChoice | None] = field(default_factory=dict)
    land_fraction: dict[int, float | None] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.wave_geometry is not None and self.sea_geometry is not None


@dataclass
class _StepSample:
    step: int
    wave: dict[int, dict[str, float | None]] | None = None
    atmos: dict[int, dict[str, float | None]] | None = None
    gust_window_h: float | None = None
    errors: dict[str, str] = field(default_factory=dict)  # part ("wave"/"oper") -> reason
    transient: set[str] = field(default_factory=set)  # parts that failed transiently
    missing_params: list[str] = field(default_factory=list)
    requests: int = 0
    bytes: int = 0

    def merge(self, retry: _StepSample, parts: Iterable[str]) -> None:
        """Adopt a second attempt of ``parts``; what still fails keeps its (new) reason."""
        for part in parts:
            if part in retry.errors:
                self.errors[part] = f"{retry.errors[part]} (after a second attempt)"
            else:
                self.errors.pop(part, None)
                if part == WAVE_STREAM:
                    self.wave = retry.wave
                else:
                    self.atmos, self.gust_window_h = retry.atmos, retry.gust_window_h
        self.transient = set(retry.transient)
        self.missing_params = sorted({*self.missing_params, *retry.missing_params})
        self.requests += retry.requests
        self.bytes += retry.bytes


# --------------------------------------------------------------------------- provider
class EcmwfIfsProvider(ForecastProvider):
    """ECMWF IFS HRES + ecWAM 0.25° open data, read by byte range from the S3 bucket."""

    code = ECMWF_IFS_CODE

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        max_lead_hours: int | None = None,
        steps: Iterable[int] | None = None,
        attempts: int = RETRY_ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
        request_interval_seconds: float = REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.ecmwf_base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (forecast ingestion)"},
        )
        self.pacer = _RequestPacer(request_interval_seconds)
        self.counter = _ResponseCounter()
        self.tally = HttpTally()
        hooks = self.client.event_hooks
        self.client.event_hooks = {
            "request": [*hooks.get("request", []), self.pacer],
            "response": [*hooks.get("response", []), self.counter, self.tally],
        }
        wanted = self.settings.ecmwf_wave_param_list
        unknown = sorted(set(wanted) - set(WAVE_PARAMS))
        if unknown or not set(REQUIRED_WAVE_PARAMS) <= set(wanted):
            raise PermanentProviderError(
                f"ECMWF_WAVE_PARAMS={','.join(wanted)!r}: must include "
                f"{', '.join(REQUIRED_WAVE_PARAMS)} and only name {', '.join(WAVE_PARAMS)}"
                + (f" (unknown: {', '.join(unknown)})" if unknown else "")
            )
        self.wave_params = tuple(p for p in wanted if p not in STATIC_WAVE_PARAMS)
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.workers = max(1, min(self.settings.grib_download_workers, MAX_WORKERS))
        self.max_lead_hours = min(
            max_lead_hours or self.settings.ecmwf_max_lead_hours,
            self.settings.forecast_days * 24,
        )
        # Explicit step list (live checks, backfills); default: every published step.
        self.steps = sorted(set(steps)) if steps is not None else None
        self.probe_request_count = 0
        self.last_fetch_stats: dict[str, Any] = {}

    # ------------------------------------------------------------------ metadata
    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="ECMWF IFS HRES + ecWAM (open data, 0.25°)",
            provider=PROVIDER,
            wave_model="ecmwf_ifs_ecwam_0p25",
            atmosphere_model="ecmwf_ifs_hres_0p25",
            is_demo=False,
            attribution=ATTRIBUTION,
            license_notes=LICENSE_NOTES,
            family="ecmwf_ifs",
            kind="deterministic",
        )

    def _steps_for(self, cycle: datetime) -> list[int]:
        return self.steps or cycle_steps(cycle.hour, self.max_lead_hours)

    def _published(self, url: str) -> bool:
        """HEAD probe: True = 200, False = not published (403/404); throttling raises."""
        self.probe_request_count += 1
        response = request_with_retries(
            self.client,
            "HEAD",
            url,
            provider=PROVIDER,
            attempts=self.attempts,
            backoff_seconds=self.backoff_seconds,
        )
        if response.status_code == 200:
            return True
        if response.status_code in (403, 404):
            return False
        raise PermanentProviderError(f"{PROVIDER}: HTTP {response.status_code} for HEAD {url}")

    def current_run(self, now: datetime) -> RunInfo:
        """Newest 00/12Z cycle whose last needed step is published for waves and atmosphere.

        Wave files appeared all at once ~7.6 h after the cycle and atmosphere files from
        ~6.6 to ~7.6 h (12Z of 2026-10-10), so probing the last step's ``.index`` of both
        streams is enough. Candidates start at ``now - ECMWF_AVAILABILITY_LAG_HOURS``.
        """
        tried: list[str] = []
        for cycle in candidate_cycles(now, self.settings.ecmwf_availability_lag_hours):
            last = self._steps_for(cycle)[-1]
            complete = all(
                self._published(file_url(self.base_url, cycle, last, stream, "index"))
                for stream in (WAVE_STREAM, ATMOS_STREAM)
            )
            if complete:
                return RunInfo(
                    run_key=f"{RUN_KEY_PREFIX}:{cycle:%Y%m%d%H}",
                    issued_at=cycle,
                    wave_model_run_at=cycle,
                    atmosphere_model_run_at=cycle,
                    metadata_source="ecmwf_index_probe",
                )
            tried.append(f"{cycle:%Y-%m-%d %H}Z (+{last}h)")
            logger.info("ECMWF cycle %s not complete to +%dh yet", f"{cycle:%Y%m%d%H}", last)
        raise TransientProviderError(
            f"{PROVIDER}: no complete 00/12Z cycle published; tried {', '.join(tried)}"
        )

    # ------------------------------------------------------------------ data
    @staticmethod
    def _cycle_of(run: RunInfo) -> datetime:
        when = run.wave_model_run_at
        if when is not None:
            # A naive time is UTC already (astimezone would take it as the server's local time).
            return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
        try:
            stamp = run.run_key.split(":", 1)[1]
            return datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except (IndexError, ValueError) as exc:
            raise PermanentProviderError(f"unrecognised ECMWF run key {run.run_key!r}") from exc

    def _fetcher(self) -> GribFetcher:
        return GribFetcher(
            self.client,
            PROVIDER,
            attempts=self.attempts,
            backoff_seconds=self.backoff_seconds,
        )

    def _download(
        self, fetcher: GribFetcher, cycle: datetime, step: int, stream: str, params: Iterable[str]
    ) -> tuple[dict[str, bytes], list[str]]:
        """Messages of ``params`` for one step (by param name) and the params not indexed."""
        index = parse_ecmwf_index(
            fetcher.text(file_url(self.base_url, cycle, step, stream, "index"))
        )
        wanted = [f"{p}:{LEVTYPE}" for p in params]
        picked = select_entries(index, wanted)
        entries: list[IndexEntry] = [picked[k] for k in wanted if k in picked]
        missing = [k.split(":")[0] for k in wanted if k not in picked]
        if not entries:
            return {}, missing
        blobs = fetcher.ranges(file_url(self.base_url, cycle, step, stream), entries)
        return {e.variable: blob for e, blob in zip(entries, blobs, strict=True)}, missing

    def _sample_wave(
        self, blobs: dict[str, bytes], spots: list[SpotPoint], state: _RunState
    ) -> dict[int, dict[str, float | None]]:
        if "swh" not in blobs:
            raise GribError("swh missing from the wave index")
        swh = decode_message(blobs["swh"])
        geometry = swh.geometry_key()
        with state.lock:
            if state.wave_geometry is None:
                state.wave_geometry = geometry
                for p in spots:
                    # Nearest cell with a valid swh (wave fields are masked over land).
                    # The ECMWF grids start at -180 deg: a point in the last half column
                    # wraps to column 0, as GridField.contains treats a grid that is
                    # global in longitude as covering every longitude.
                    state.wave_cells[p.spot_id] = state.sampler.cell(
                        swh,
                        ("wave", p.spot_id),
                        p.forecast_latitude,
                        p.forecast_longitude,
                        mask_name="swh",
                    )
        if geometry != state.wave_geometry:
            raise GribError(f"wave grid geometry changed: {geometry} != {state.wave_geometry}")
        cells = {p.spot_id: state.wave_cells.get(p.spot_id) for p in spots}
        out: dict[int, dict[str, float | None]] = {
            spot_id: {"swh": state.sampler.sample(swh, cell)}
            for spot_id, cell in cells.items()
            if cell is not None
        }
        # Decode and sample one field at a time: a decoded 0.25 deg field is 8.3 MB as
        # float64, so holding the 12 wave fields of a step at once costs ~100 MB per worker.
        for name, blob in blobs.items():
            if name == "swh" or (name in STATIC_WAVE_PARAMS and state.model_depth):
                continue
            grid = decode_message(blob)
            if grid.geometry_key() != geometry:
                raise GribError(f"{name} grid differs from the swh grid")
            if name in STATIC_WAVE_PARAMS:  # wmb, sampled on the first wave step of the run
                with state.lock:
                    for p in spots:
                        state.model_depth[p.spot_id] = state.sampler.sample(
                            grid, state.wave_cells.get(p.spot_id)
                        )
                continue
            for spot_id, values in out.items():
                values[name] = state.sampler.sample(grid, cells[spot_id])
        return out

    def _set_sea_mask(self, lsm: GridField, spots: list[SpotPoint], state: _RunState) -> None:
        with state.lock:
            if state.sea_geometry is not None:
                return
            sea = np.nan_to_num(lsm.values, nan=1.0) < SEA_MAX_LAND_FRACTION
            for p in spots:
                # Wind at the break: nearest cell the IFS land-sea mask calls sea.
                choice = state.sampler.cell(
                    lsm,
                    ("wind", p.spot_id),
                    p.latitude,
                    p.longitude,
                    valid=sea,
                    mask_name="lsm<0.5",
                )
                state.wind_cells[p.spot_id] = choice
                state.land_fraction[p.spot_id] = (
                    None if choice is None else lsm.value(choice.j, choice.i)
                )
            state.sea_geometry = lsm.geometry_key()

    def _sample_atmos(
        self, blobs: dict[str, bytes], spots: list[SpotPoint], state: _RunState
    ) -> tuple[dict[int, dict[str, float | None]], float | None]:
        if LSM_PARAM in blobs:
            self._set_sea_mask(decode_message(blobs[LSM_PARAM]), spots, state)
        if state.sea_geometry is None:
            raise GribError("land-sea mask unavailable, cannot choose sea cells for wind")
        cells = {
            p.spot_id: cell for p in spots if (cell := state.wind_cells.get(p.spot_id)) is not None
        }
        out: dict[int, dict[str, float | None]] = {spot_id: {} for spot_id in cells}
        window: float | None = None
        gust_read = False
        for name in ATMOS_PARAMS:  # decoded and sampled one at a time (8.3 MB each)
            if name not in blobs:
                continue
            grid = decode_message(blobs[name])
            if grid.geometry_key() != state.sea_geometry:
                raise GribError(f"{name} grid differs from the land-sea mask grid")
            if name == "10fg":
                window = step_window_hours(blobs[name])
                if not window or window <= 0:
                    continue  # step 0: no window yet, the field is a constant 0
                gust_read = True
            for spot_id, cell in cells.items():
                out[spot_id][name] = grid.value(cell.j, cell.i)
        return out, (window if gust_read else None)

    def _fetch_step(
        self,
        cycle: datetime,
        step: int,
        spots: list[SpotPoint],
        state: _RunState,
        parts: Iterable[str] = (WAVE_STREAM, ATMOS_STREAM),
    ) -> _StepSample:
        """Fetch and sample one step; failures are recorded per part, never raised."""
        sample = _StepSample(step)
        fetcher = self._fetcher()
        for part in parts:
            try:
                if part == WAVE_STREAM:
                    wave_params = list(self.wave_params)
                    if not state.model_depth:  # the static depth field, once per run
                        wave_params += STATIC_WAVE_PARAMS
                    blobs, missing = self._download(fetcher, cycle, step, part, wave_params)
                    sample.wave = self._sample_wave(blobs, spots, state)
                else:
                    params = list(ATMOS_PARAMS)
                    if state.sea_geometry is None:
                        params.append(LSM_PARAM)
                    blobs, missing = self._download(fetcher, cycle, step, part, params)
                    sample.atmos, sample.gust_window_h = self._sample_atmos(blobs, spots, state)
                sample.missing_params += [f"{part}:{m}" for m in missing]
            except NotPublishedError as exc:
                sample.errors[part] = f"not published ({exc})"
            except TransientProviderError as exc:
                sample.errors[part] = str(exc)
                sample.transient.add(part)
            except (GribError, PermanentProviderError, KeyError, ValueError) as exc:
                # KeyError/ValueError: malformed index line (no _offset/_length).
                sample.errors[part] = f"{type(exc).__name__}: {exc}"
        sample.requests = fetcher.request_count
        sample.bytes = fetcher.bytes_downloaded
        return sample

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        """Sample every step of the run's cycle at every spot.

        Failures are isolated per step and stream: a missing or throttled step leaves a
        gap that is reported (run warnings + an "incomplete" spot error, so the run is
        PARTIAL), never silently dropped. Only when no step yields wave data does the
        whole fetch raise (transient if every failure was transient).
        """
        if not spots:
            return FetchResult()
        started = time.monotonic()
        before = (self.counter.responses, self.counter.http_503, self.counter.slowdown)
        tally_before = self.tally.snapshot()
        cycle = self._cycle_of(run)
        steps = self._steps_for(cycle)
        state = _RunState(PointSampler(self.settings.grib_max_cell_distance_km))
        samples: dict[int, _StepSample] = {}
        pending = list(steps)
        while pending and not state.ready and len(samples) < WARMUP_STEPS:
            step = pending.pop(0)
            samples[step] = self._fetch_step(cycle, step, spots, state)
        if pending:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for sample in pool.map(lambda s: self._fetch_step(cycle, s, spots, state), pending):
                    samples[sample.step] = sample
        # Second chance for throttled / timed-out parts, one at a time (SlowDown clears).
        for step in steps:
            sample = samples[step]
            if sample.transient:
                parts = sorted(sample.transient)
                sample.merge(self._fetch_step(cycle, step, spots, state, parts=parts), parts)

        result = self._assemble(cycle, steps, samples, spots, state)
        result.request_count = sum(s.requests for s in samples.values())
        stats: dict[str, Any] = {
            "cycle": f"{cycle:%Y%m%d%H}",
            "steps": len(steps),
            "steps_with_waves": sum(1 for s in samples.values() if s.wave is not None),
            "steps_with_atmosphere": sum(1 for s in samples.values() if s.atmos is not None),
            "requests": result.request_count,
            "http_attempts": self.counter.responses - before[0],
            "http_503": self.counter.http_503 - before[1],
            "slowdown": self.counter.slowdown - before[2],
            "bytes": sum(s.bytes for s in samples.values()),
            "seconds": round(time.monotonic() - started, 1),
        }
        self.last_fetch_stats = stats
        responses, wire_bytes, statuses = self.tally.snapshot()
        result.stats = FetchStats(
            bytes_downloaded=wire_bytes - tally_before[1],
            requests=responses - tally_before[0],
            http_status_counts={
                k: v - tally_before[2].get(k, 0)
                for k, v in statuses.items()
                if v - tally_before[2].get(k, 0)
            },
        )
        mb = result.stats.bytes_downloaded / 1e6
        result.warnings.insert(
            0,
            f"downloaded {mb:.1f} MB in {result.request_count} requests for {len(steps)} steps "
            f"({mb / max(len(steps), 1):.2f} MB per step; wave parameters "
            f"{','.join(self.wave_params)})",
        )
        if stats["http_503"]:
            result.warnings.insert(
                0,
                f"ECMWF S3 answered {stats['http_503']} of {stats['http_attempts']} HTTP "
                f"attempts with 503 ({stats['slowdown']} SlowDown); retried with backoff",
            )
        logger.info("ECMWF fetch %s", stats)
        failures = [samples[s] for s in steps if WAVE_STREAM in samples[s].errors]
        if not result.records and failures:
            first = failures[0]
            message = (
                f"{PROVIDER}: no wave data for cycle {cycle:%Y%m%d%H} ({len(failures)} of "
                f"{len(steps)} steps failed; +{first.step}h: {first.errors[WAVE_STREAM]})"
            )
            if all(WAVE_STREAM in s.transient for s in failures):
                raise TransientProviderError(message)
            raise PermanentProviderError(message)
        return result

    def _assemble(
        self,
        cycle: datetime,
        steps: list[int],
        samples: dict[int, _StepSample],
        spots: list[SpotPoint],
        state: _RunState,
    ) -> FetchResult:
        result = FetchResult()
        for step in steps:
            s = samples[step]
            for part, reason in s.errors.items():
                result.warnings.append(f"+{step}h {part}: {reason}")
        missing_params = sorted({m for s in samples.values() for m in s.missing_params})
        if missing_params:
            result.warnings.append(f"parameters absent from some indexes: {missing_params}")
        wave_ok = any(s.wave is not None for s in samples.values())
        for p in spots:
            if wave_ok and state.wave_cells.get(p.spot_id) is None:
                result.errors[p.spot_id] = (
                    "no valid ECMWF wave cell within "
                    f"{self.settings.grib_max_cell_distance_km:g} km of the forecast point "
                    f"({p.forecast_latitude}, {p.forecast_longitude})"
                )
                continue
            if state.sea_geometry is not None and state.wind_cells.get(p.spot_id) is None:
                result.warnings.append(
                    f"{p.slug}: no ECMWF sea cell (lsm < {SEA_MAX_LAND_FRACTION}) within "
                    f"{self.settings.grib_max_cell_distance_km:g} km of the break; no wind"
                )
            records: list[ForecastRecord] = []
            no_wave: list[int] = []
            no_wind: list[int] = []
            for step in steps:
                s = samples[step]
                wave = (s.wave or {}).get(p.spot_id)
                if not wave or wave.get("swh") is None:
                    no_wave.append(step)
                    continue
                atmos = (s.atmos or {}).get(p.spot_id)
                if atmos is None:
                    no_wind.append(step)
                records.append(
                    self._record(cycle, step, wave, atmos or {}, s.gust_window_h, p, state)
                )
            if not records:
                result.errors[p.spot_id] = "no ECMWF wave data at any step (see run warnings)"
                continue
            result.records[p.spot_id] = records
            gaps = []
            if no_wave:
                gaps.append(f"waves missing at {len(no_wave)} of {len(steps)} steps {no_wave}")
            if no_wind:
                gaps.append(f"wind/pressure missing at {len(no_wind)} steps {no_wind}")
            if gaps:
                result.errors[p.spot_id] = "incomplete: " + "; ".join(gaps)
        return result

    @staticmethod
    def _record(
        cycle: datetime,
        step: int,
        wave: dict[str, float | None],
        atmos: dict[str, float | None],
        gust_window_h: float | None,
        spot: SpotPoint,
        state: _RunState,
    ) -> ForecastRecord:
        values: dict[str, float | None] = dict.fromkeys(FORECAST_FIELDS)  # no partitions
        values["sig_wave_height_m"] = _round(wave.get("swh"), 3)
        values["wave_period_s"] = _round(wave.get("pp1d"), 2)
        values["wave_direction_deg"] = _bearing(wave.get("mwd"))  # already FROM
        u, v = atmos.get("10u"), atmos.get("10v")
        if u is not None and v is not None:
            speed_ms, direction = wind_from_uv(u, v)  # m/s; direction None when calm
            values["wind_speed_kmh"] = round(speed_ms * 3.6, 2)
            values["wind_direction_deg"] = _bearing(direction)
        gust = atmos.get("10fg")
        values["wind_gust_kmh"] = None if gust is None else round(gust * 3.6, 2)
        msl = atmos.get("msl")
        values["pressure_msl_hpa"] = None if msl is None else round(msl / 100.0, 2)

        # Only the parameters that were read (ECMWF_WAVE_PARAMS) appear in extra.
        extra: dict[str, object] = {}
        if "mwp" in wave:  # ECMWF mwp: mean period Tm-1,0 (s)
            extra["mean_period_tm10_s"] = _round(wave.get("mwp"), 2)
        if "mp2" in wave:  # ECMWF mp2: mean zero-crossing period Tm02 (s)
            extra["mean_period_tm02_s"] = _round(wave.get("mp2"), 2)
        if any(name in wave for name in BAND_PARAMS):
            # ECMWF h1012...h2530: significant height (m) of the waves in each period band.
            extra["hs_band_m"] = {
                label: _round(wave.get(name), 3) for name, label in BAND_PARAMS.items()
            }
            # Derived here (not an ECMWF field): sqrt of the sum of squares, bands >= 12 s.
            extra["hs_ge_12s_m"] = _round(
                combined_band_height(wave.get(n) for n in LONG_PERIOD_BANDS), 3
            )
        cell = state.wave_cells.get(spot.spot_id)
        if cell is not None:
            extra["wave_cell"] = {
                **cell.as_dict(),
                # ecWAM model depth (m); the open data cap it at 999 m (= deep water).
                "model_depth_m": _round(state.model_depth.get(spot.spot_id), 1),
            }
        wind_cell = state.wind_cells.get(spot.spot_id)
        if atmos and wind_cell is not None:
            extra["wind_cell"] = {
                **wind_cell.as_dict(),
                "land_fraction": _round(state.land_fraction.get(spot.spot_id), 3),
            }
        if values["wind_gust_kmh"] is not None:
            extra["wind_gust_window_h"] = gust_window_h
            extra[EXTRA_GUST_KIND] = GUST_KIND
        return ForecastRecord(
            valid_time=cycle + timedelta(hours=step),
            values=values,
            period_kind=PERIOD_KIND,
            extra=extra,
        )
