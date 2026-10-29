"""Spot point forecasts from the Open-Meteo API, one provider per model family.

Open-Meteo republishes NOAA and ECMWF model output as JSON for many coordinates per
request, so 50 spot forecasts cost kilobytes instead of the gigabytes of whole-globe GRIB
records the direct ingesters read (docs/FORECAST_SOURCES.md has the measured numbers).
The free tier is for non-commercial use; OPEN_METEO_API_KEY switches to the commercial
``customer-*`` hosts (docs/API_SETUP_REQUIRED.md). With COMMERCIAL_USE=true and no key the
providers refuse to be built or to send any request (:func:`require_commercial_licence`),
so a commercial deployment never calls the free hosts by mistake.

Families (``families.py``; the blend keeps one member per family):

* :class:`OpenMeteoProvider` (source ``open_meteo_noaa``, family ``ncep_gfs``, token
  ``open_meteo``): NOAA GFS-Wave (WAVEWATCH III) waves, NOAA GFS 10 m wind, gust and MSLP.
* ``open_meteo_ecmwf.OpenMeteoEcmwfProvider`` (source ``open_meteo_ecmwf``, family
  ``ecmwf_ifs``): ECMWF ecWAM waves and IFS atmosphere (open data, 0.25 deg).

What makes the API data equivalent to sampling the model grids ourselves:

* Grid cells. Every request sends ``cell_selection`` (OPEN_METEO_CELL_SELECTION, default
  ``sea``): Open-Meteo's default for weather picks a land cell of similar elevation, which
  gave 18.4 instead of 28.0 km/h mean wind at Pipeline (audit DA-5). Waves are requested
  at the spot's offshore forecast point, wind at the break. A wave cell without data in
  any hour (masked by the wave model although Open-Meteo calls it sea) is replaced by the
  nearest neighbouring cell with data, as the GRIB ingesters take the nearest valid cell.
  The cell used and its distance are recorded in the first record of each spot
  (``extra["wave_cell"]``, ``extra["wind_cell"]``).
* Native wave grids. ``ncep_gfswave016`` (native 0.16 deg) only covers about 52.5N..15S
  and answers HTTP 400 "No data is available for this location" outside; it is used where
  it exists and ``ncep_gfswave025`` elsewhere (OPEN_METEO_GFS_WAVE_MODELS, in that order).
  The domain comes from each model's metadata (``BBOX``). One request never mixes models;
  a spot refused by one model is retried alone with the next.
* All partitions. Primary, secondary and tertiary swell and wind sea. Open-Meteo returns
  0 m / 0 s / 0 deg for a partition the model does not have at a time: stored as None.
* Native time steps only. Open-Meteo interpolates every model to hourly ("For GFS, values
  are interpolated from 3-hourly to 1-hourly after 120 hours"; IFS 0.25 is "3-Hourly,
  6-hourly after 144 hours"). Only the model's own output steps (:data:`NATIVE_STEPS`) are
  stored; with OPEN_METEO_NATIVE_STEPS_ONLY=false the other hours are kept and flagged
  ``extra["interpolated"] = True``. Every record carries ``extra["native_step_seconds"]``.
* Run identity. The run is read from each model's ``meta.json``
  (``last_run_initialisation_time``). A new run is used only
  OPEN_METEO_AVAILABILITY_WAIT_MINUTES after ``last_run_availability_time`` (Open-Meteo:
  "please wait 10 minutes after the availability time"); until then the previous run is
  the current one. The metadata is read again before and after the data requests, and a
  run that changed meanwhile is never stored under the old key. Hours past the run's
  ``data_end_time`` (which Open-Meteo fills from an older, longer run) and hours before its
  initialisation are dropped. Every model run behind the data is recorded
  (``RunInfo.model_runs``; ``gfs_seamless`` = HRRR plus GFS).
* Components (design D6). The wave models (the marine API) and the atmosphere model (the
  forecast API) are published independently, minutes to hours apart. Each model domain is
  tagged with its component (``ModelRunRef.component``) and each component has a run key of
  its own (``RunInfo.component_keys``: ``wave`` = the wave domains, ``atmosphere`` = the
  atmosphere domains; the run key is the two joined). ``fetch(components=...)`` requests
  only those components (the marine request for ``wave``, the forecast request for
  ``atmosphere``; still one batched request per model for all spots), and
  ``ensure_servable(components=...)`` checks only their domains.
* Gusts. ``extra["gust_kind"]`` says what the gust is (``gfs_instantaneous``: GFS's GUST is
  an instantaneous surface diagnostic, often below the 10 m mean wind; ``ecmwf_max_3h``).
* Sea level. ``sea_level_height_msl`` is not requested: it is a modelled sea surface height
  above global mean sea level, "not suitable for coastal navigation", not a tide
  prediction. Tides come from app.services.tides.

Calls are weighted (``quota.call_weight``) and charged to the shared Open-Meteo budget
before they are sent; responses are kept in the bounded Redis cache (``provider_cache``)
for the model's update interval, so a retried run does not download them again.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, ClassVar, Literal

import httpx

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.services.forecasts import provider_cache
from app.services.forecasts.base import (
    ATMOSPHERE,
    COMPONENTS,
    EXTRA_GUST_KIND,
    EXTRA_INTERPOLATED,
    EXTRA_NATIVE_STEP_S,
    EXTRA_WAVE_CELL,
    EXTRA_WAVE_MODEL,
    EXTRA_WIND_CELL,
    FORECAST_FIELDS,
    FetchResult,
    FetchStats,
    ForecastProvider,
    ForecastRecord,
    ModelRunRef,
    PartlyReadRun,
    RunInfo,
    SourceInfo,
    SpotPoint,
    http_client,
)
from app.services.forecasts.base import WAVE as WAVE_COMPONENT
from app.services.forecasts.grib import haversine_km
from app.services.forecasts.provider_cache import ProviderCacheKey, coords_hash
from app.services.forecasts.quota import (
    QuotaWindowExhaustedError,
    call_weight,
    consume_open_meteo_budget,
    seconds_until_next_utc_day,
    seconds_until_next_utc_month,
)
from app.services.http import (
    RETRYABLE_STATUS,
    HttpTally,
    PermanentProviderError,
    ProviderError,
    RunNotServedError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)

HTTP_PROVIDER = "open_meteo"
# Raised (PermanentProviderError) with COMMERCIAL_USE=true and no OPEN_METEO_API_KEY.
FREE_API_NON_COMMERCIAL = "free Open-Meteo API is non-commercial; set OPEN_METEO_API_KEY"
USER_AGENT = (
    "SwellTravelAgent/1.0 (forecast ingestion; +https://github.com/eliasghizusfca/swellinformant)"
)
HostKind = Literal["marine", "api"]
# The order the two components of a run are read and listed in (wave domains first).
COMPONENT_ORDER: tuple[str, ...] = (WAVE_COMPONENT, ATMOSPHERE)


DEFAULT_UPDATE_INTERVAL_S = 6 * 3600
# HTTP timeout of one meta.json request (seconds; FORECAST_HTTP_TIMEOUT_SECONDS when lower):
# the file is under 1 kB, so a host that has not answered by then is down. It bounds what a
# hanging host costs a refresh that carries that host's component (3 attempts).
META_TIMEOUT_SECONDS = 10.0

# Wave variables of the GFS family (GFS-Wave's wave_period is its peak period PERPW).
GFS_WAVE_CORE = {
    "wave_height": "sig_wave_height_m",
    "wave_direction": "wave_direction_deg",
    "wave_period": "wave_period_s",
    "wind_wave_height": "wind_wave_height_m",
    "wind_wave_direction": "wind_wave_direction_deg",
    "wind_wave_period": "wind_wave_period_s",
    "swell_wave_height": "primary_swell_height_m",
    "swell_wave_direction": "primary_swell_direction_deg",
    "swell_wave_period": "primary_swell_period_s",
}
# Dropped automatically when a model rejects them (HTTP 400 naming the variable).
GFS_WAVE_OPTIONAL = {
    "secondary_swell_wave_height": "secondary_swell_height_m",
    "secondary_swell_wave_direction": "secondary_swell_direction_deg",
    "secondary_swell_wave_period": "secondary_swell_period_s",
    "tertiary_swell_wave_height": "tertiary_swell_height_m",
    "tertiary_swell_wave_direction": "tertiary_swell_direction_deg",
    "tertiary_swell_wave_period": "tertiary_swell_period_s",
}
ATMOS = {
    "wind_speed_10m": "wind_speed_kmh",
    "wind_direction_10m": "wind_direction_deg",
    "wind_gusts_10m": "wind_gust_kmh",
    "pressure_msl": "pressure_msl_hpa",
}
ATMOS_FIELDS = tuple(ATMOS.values())
PARTITIONS = ("primary_swell", "secondary_swell", "tertiary_swell", "wind_wave")

# The models' own output steps as (first lead h, last lead h, step h) segments, from the
# Open-Meteo model pages (checked 2026-10-14) and NOAA's GFS / GFS-Wave file lists:
# GFS "Hourly, 3-hourly after 120 hours"; GFS-Wave files are hourly to f120, then 3-hourly
# to f384; IFS 0.25 open data "3-Hourly, 6-hourly after 144 hours" (ecWAM 0.25 likewise);
# IFS HRES 9 km "1-Hourly, 3-hourly after 90 hours, 6-hourly after 144 hours".
GFS_STEPS = ((0, 120, 1), (120, 384, 3))
ECMWF_OPEN_STEPS = ((0, 144, 3), (144, 360, 6))
ECMWF_HRES_STEPS = ((0, 90, 1), (90, 144, 3), (144, 360, 6))
NATIVE_STEPS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "ncep_gfswave016": GFS_STEPS,
    "ncep_gfswave025": GFS_STEPS,
    "gfs_global": GFS_STEPS,
    "gfs_seamless": GFS_STEPS,
    "gfs025": GFS_STEPS,
    "gfs013": GFS_STEPS,
    "ecmwf_wam025": ECMWF_OPEN_STEPS,
    "ecmwf_ifs025": ECMWF_OPEN_STEPS,
    "ecmwf_wam": ECMWF_HRES_STEPS,
    "ecmwf_ifs": ECMWF_HRES_STEPS,
}

# Metadata domains behind each API model name (``/data/{domain}/static/meta.json``).
# gfs_global reads GFS 0.11/0.13 deg and falls back to 0.25 deg for some variables, so
# both runs are recorded; gfs_seamless adds HRRR (hourly runs, CONUS only).
META_DOMAINS: dict[str, tuple[tuple[HostKind, str], ...]] = {
    "ncep_gfswave016": (("marine", "ncep_gfswave016"),),
    "ncep_gfswave025": (("marine", "ncep_gfswave025"),),
    "gfs_global": (("api", "ncep_gfs013"), ("api", "ncep_gfs025")),
    "gfs_seamless": (("api", "ncep_hrrr_conus"), ("api", "ncep_gfs013"), ("api", "ncep_gfs025")),
    "gfs025": (("api", "ncep_gfs025"),),
    "gfs013": (("api", "ncep_gfs013"),),
    "ecmwf_wam025": (("marine", "ecmwf_wam025"),),
    "ecmwf_ifs025": (("api", "ecmwf_ifs025"),),
}
# Latitude domains used when a model's metadata has no BBOX (Open-Meteo meta.json,
# 2026-10-14: ncep_gfswave016 BBOX[-15.0,-180.0,52.5,179.83334]).
KNOWN_LATITUDE_DOMAINS: dict[str, tuple[float, float]] = {"ncep_gfswave016": (-15.0, 52.5)}
# Grid spacing of the wave models (degrees). cell_selection=sea can still pick a cell the
# wave model masks (no value in any hour: ecmwf_wam025 at Keramas and Puerto Escondido on
# 2026-10-14); the neighbouring cells are then read (``_neighbour_cells``).
WAVE_GRID_STEP_DEG: dict[str, float] = {
    "ncep_gfswave016": 1 / 6,
    "ncep_gfswave025": 0.25,
    "ecmwf_wam025": 0.25,
}

LICENCE_URL = "https://open-meteo.com/en/licence"
ATTRIBUTION_LINK = "https://open-meteo.com/"
_BBOX = re.compile(r"BBOX\[\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\]")


# --------------------------------------------------------------------------- metadata
@dataclass(frozen=True)
class ModelMeta:
    """One Open-Meteo model domain's ``meta.json`` (times UTC)."""

    domain: str
    init_time: datetime
    available_at: datetime | None
    data_end: datetime | None
    update_interval_s: int
    temporal_resolution_s: int | None
    bbox: tuple[float, float, float, float] | None  # lat_min, lon_min, lat_max, lon_max

    @classmethod
    def parse(cls, domain: str, data: dict[str, Any]) -> ModelMeta:
        def stamp(name: str) -> datetime | None:
            value = data.get(name)
            return None if value in (None, "") else datetime.fromtimestamp(int(value), tz=UTC)

        init = stamp("last_run_initialisation_time")
        if init is None:
            raise ValueError("no last_run_initialisation_time")
        match = _BBOX.search(str(data.get("crs_wkt") or ""))
        bbox = None
        if match:
            lat0, lon0, lat1, lon1 = (float(g) for g in match.groups())
            bbox = (lat0, lon0, lat1, lon1)
        resolution = data.get("temporal_resolution_seconds")
        return cls(
            domain=domain,
            init_time=init,
            available_at=stamp("last_run_availability_time"),
            data_end=stamp("data_end_time"),
            update_interval_s=int(data.get("update_interval_seconds") or DEFAULT_UPDATE_INTERVAL_S),
            temporal_resolution_s=int(resolution) if resolution else None,
            bbox=bbox,
        )

    def ready(self, now: datetime, wait: timedelta) -> bool:
        """True once the newest run has been available for ``wait``."""
        return self.available_at is None or now >= self.available_at + wait

    def contains(self, lat: float, lon: float) -> bool:
        if self.bbox is None:
            bounds = KNOWN_LATITUDE_DOMAINS.get(self.domain)
            return bounds is None or bounds[0] <= lat <= bounds[1]
        lat0, lon0, lat1, lon1 = self.bbox
        if not lat0 <= lat <= lat1:
            return False
        if lon1 - lon0 >= 359.0:  # global in longitude (the last column wraps)
            return True
        lon = ((lon + 180.0) % 360.0) - 180.0
        return lon0 <= lon <= lon1


def native_step_hours(model: str, lead_hours: float, fallback_s: int | None = None) -> int | None:
    """The model's output spacing at ``lead_hours`` (forward), or None past its horizon."""
    segments = NATIVE_STEPS.get(model)
    if segments is None:
        return None if not fallback_s else max(1, round(fallback_s / 3600))
    for start, end, step in segments:
        if start <= lead_hours < end:
            return step
    last_start, last_end, last_step = segments[-1]
    return last_step if lead_hours == last_end else None


def is_native_step(model: str, lead_hours: float, fallback_s: int | None = None) -> bool:
    """True when the model itself outputs a value at this lead time."""
    if lead_hours < 0 or lead_hours != math.floor(lead_hours):
        return False
    segments = NATIVE_STEPS.get(model)
    if segments is None:
        step = max(1, round((fallback_s or 3600) / 3600))
        return int(lead_hours) % step == 0
    return any(
        start <= lead_hours <= end and (int(lead_hours) - start) % step == 0
        for start, end, step in segments
    )


# --------------------------------------------------------------------------- errors
class OpenMeteoError(PermanentProviderError):
    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict) and body.get("reason"):
        return str(body["reason"])
    return f"HTTP {resp.status_code}"


def _next_hour(now: datetime) -> float:
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(60.0, (top - now).total_seconds())


# --------------------------------------------------------------------------- records
def _clean(values: dict[str, float | None]) -> dict[str, float | None]:
    """Absent partitions to None, directions to [0, 360), calm wind without a direction."""
    for prefix in PARTITIONS:
        h = values.get(f"{prefix}_height_m")
        if h is None or h <= 0:
            for suffix in ("height_m", "period_s", "direction_deg"):
                if f"{prefix}_{suffix}" in values:
                    values[f"{prefix}_{suffix}"] = None
            continue
        period = values.get(f"{prefix}_period_s")
        if period is not None and period <= 0:
            values[f"{prefix}_period_s"] = None
    hs = values.get("sig_wave_height_m")
    if hs is not None and hs <= 0:
        values["sig_wave_height_m"] = None
    period = values.get("wave_period_s")
    if period is not None and period <= 0:
        values["wave_period_s"] = None
    for name in [k for k in values if k.endswith("direction_deg")]:
        d = values[name]
        if d is not None:
            values[name] = round(d % 360.0, 1) % 360.0
    speed = values.get("wind_speed_kmh")
    if speed is not None and speed <= 0:
        values["wind_direction_deg"] = None  # calm: no direction (geo.wind_from_uv)
    return values


def _cell(item: dict[str, Any], lat: float, lon: float) -> dict[str, float] | None:
    clat, clon = item.get("latitude"), item.get("longitude")
    if clat is None or clon is None:
        return None
    return {
        "lat": round(float(clat), 4),
        "lon": round(float(clon), 4),
        "distance_km": round(haversine_km(lat, lon, float(clat), float(clon)), 1),
    }


@dataclass
class Series:
    """One location's hourly arrays from one response."""

    model: str
    item: dict[str, Any]
    mapping: dict[str, str]

    def by_time(self) -> dict[int, dict[str, float | None]]:
        hourly = self.item.get("hourly") or {}
        times = hourly.get("time") or []
        out: dict[int, dict[str, float | None]] = {}
        for idx, ts in enumerate(times):
            row: dict[str, float | None] = {}
            for api_name, name in self.mapping.items():
                values = hourly.get(api_name)
                value = values[idx] if values is not None and idx < len(values) else None
                row[name] = None if value is None else float(value)
            out[int(ts)] = row
        return out

    def has_wave_height(self) -> bool:
        """Whether any hour has a significant wave height (False: a masked cell)."""
        names = [api for api, name in self.mapping.items() if name == "sig_wave_height_m"]
        hourly = self.item.get("hourly") or {}
        return any(v is not None for api in names for v in hourly.get(api) or [])


@dataclass
class _Fetch:
    """State of one fetch() call."""

    labelled: dict[str, datetime]
    metas: dict[str, ModelMeta]
    cache_writes: list[tuple[ProviderCacheKey, bytes, int]] = field(default_factory=list)
    data_requests: int = 0
    cache_hits: int = 0
    weighted_calls: float = 0.0
    warnings: list[str] = field(default_factory=list)


def free_api_refused(settings: Settings) -> bool:
    """True when the site is in commercial use (COMMERCIAL_USE) without OPEN_METEO_API_KEY:
    Open-Meteo's free API "may only" be used "for non-commercial purposes", so its free
    hosts must not be called."""
    return bool(settings.commercial_use and not settings.open_meteo_api_key)


def require_commercial_licence(settings: Settings) -> None:
    """Raise PermanentProviderError when :func:`free_api_refused` (nothing is retried)."""
    if free_api_refused(settings):
        raise PermanentProviderError(FREE_API_NON_COMMERCIAL)


# --------------------------------------------------------------------------- provider
class OpenMeteoFamilyProvider(ForecastProvider):
    """Shared Open-Meteo logic; subclasses choose the models, variables and labels."""

    family: ClassVar[str]
    atmos_path: ClassVar[str]
    gust_kind: ClassVar[str] = "unknown"
    # Only runs initialised at these UTC hours are read (None = every run).
    cycle_hours: ClassVar[tuple[int, ...] | None] = None
    max_forecast_days: ClassVar[int] = 16
    period_kind: ClassVar[str | None] = "tp"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        require_commercial_licence(self.settings)
        self.client = client or http_client(self.settings.forecast_http_timeout_seconds, USER_AGENT)
        self.tally = HttpTally()
        self.tally.install(self.client)
        self.backoff_seconds = backoff_seconds
        self.clock = clock or utcnow
        key = self.settings.open_meteo_api_key
        prefix = "customer-" if key else ""
        self.hosts: dict[HostKind, str] = {
            "marine": f"https://{prefix}marine-api.open-meteo.com",
            "api": f"https://{prefix}api.open-meteo.com",
        }
        self.wait = timedelta(minutes=self.settings.open_meteo_availability_wait_minutes)
        self.days = min(self.settings.forecast_days, self.max_forecast_days)
        # Weighted calls charged by this instance, failed fetches included (ingestion
        # records the share of a fetch that raised).
        self.weighted_calls_total = 0.0
        # The newest meta.json read of each domain (update_interval_seconds).
        self.last_metas: dict[str, ModelMeta] = {}

    # ---------------------------------------------------------------- subclass hooks
    def wave_models(self) -> list[str]:
        raise NotImplementedError

    def atmos_model(self) -> str:
        raise NotImplementedError

    def wave_variables(self, optional: bool) -> dict[str, str]:
        raise NotImplementedError

    def wave_extra(self, row: dict[str, float | None]) -> dict[str, object]:
        """Values of a wave row that go to ``extra`` instead of ``values``."""
        return {}

    def gust_kind_at(self) -> str:
        return self.gust_kind

    def attribution(self) -> str:
        raise NotImplementedError

    def display_name(self) -> str:
        raise NotImplementedError

    # ---------------------------------------------------------------- metadata
    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name=self.display_name(),
            provider="open_meteo",
            wave_model=",".join(self.wave_models()),
            atmosphere_model=self.atmos_model(),
            is_demo=False,
            attribution=self.attribution(),
            license_notes=(
                f"Open-Meteo data are CC BY 4.0 ({LICENCE_URL}): credit Open-Meteo with a link "
                f"({ATTRIBUTION_LINK}) next to the data and the model providers. The free API "
                "is for non-commercial use only (600 calls/min, 5,000/h, 10,000/day, "
                "300,000/month); commercial use needs an Open-Meteo API subscription "
                "(OPEN_METEO_API_KEY). Values are sampled at Open-Meteo's chosen grid cell "
                "and only the models' own output steps are kept."
            ),
            family=self.family,
            kind="deterministic",
        )

    def domains(self) -> list[tuple[str, HostKind, str]]:
        """(model, host kind, meta domain) for every model this provider requests."""
        requested: list[tuple[str, HostKind]] = [(m, "marine") for m in self.wave_models()]
        requested.append((self.atmos_model(), "api"))
        out: list[tuple[str, HostKind, str]] = []
        for model, kind in requested:
            for host, domain in META_DOMAINS.get(model, ((kind, model),)):
                out.append((model, host, domain))
        return out

    def _params(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.settings.open_meteo_api_key:
            return {**params, "apikey": self.settings.open_meteo_api_key}
        return params

    def read_metas(self, components: Collection[str] | None = None) -> dict[str, ModelMeta]:
        """``meta.json`` of every domain (free calls: not charged to the budget); with
        ``components``, of the domains of those components only."""
        require_commercial_licence(self.settings)
        metas: dict[str, ModelMeta] = {}
        for model, host, domain in self.domains():
            if domain in metas or (
                components is not None and self.component_of_model(model) not in components
            ):
                continue
            url = f"{self.hosts[host]}/data/{domain}/static/meta.json"
            resp = request_with_retries(
                self.client,
                "GET",
                url,
                provider=HTTP_PROVIDER,
                backoff_seconds=self.backoff_seconds,
                params=self._params({}),
                timeout=min(self.settings.forecast_http_timeout_seconds, META_TIMEOUT_SECONDS),
            )
            if resp.status_code != 200:
                raise TransientProviderError(
                    f"Open-Meteo metadata for {domain} returned HTTP {resp.status_code}: the "
                    "model run cannot be identified, so nothing is fetched"
                )
            try:
                metas[domain] = ModelMeta.parse(domain, resp.json())
            except (ValueError, TypeError) as exc:
                raise TransientProviderError(
                    f"Open-Meteo metadata for {domain} is unreadable ({exc}): the model run "
                    "cannot be identified, so nothing is fetched"
                ) from exc
        self.last_metas.update(metas)
        return metas

    def _metas_of(self, components: Collection[str]) -> dict[str, ModelMeta]:
        """``meta.json`` of the domains of these components, read one component at a time
        (the reads a check shares, ``detect.servable_run``); raises when one fails."""
        metas: dict[str, ModelMeta] = {}
        for component in COMPONENT_ORDER:
            if component in components:
                metas.update(self.read_metas(frozenset({component})))
        return metas

    def _component_metas(self) -> tuple[dict[str, ModelMeta], dict[str, TransientProviderError]]:
        """Every component's ``meta.json``, and why a component's could not be read (its
        host timed out or answered an error: design D6, the other component is still
        planned). A refused licence (PermanentProviderError) raises."""
        metas: dict[str, ModelMeta] = {}
        unread: dict[str, TransientProviderError] = {}
        for component in COMPONENT_ORDER:
            try:
                metas.update(self.read_metas(frozenset({component})))
            except TransientProviderError as exc:
                unread[component] = exc
        return metas, unread

    def update_interval_seconds(self, component: str) -> int | None:
        """The update interval of a component's model (meta.json ``update_interval_seconds``,
        the longest of its domains) as last read; None before its metadata was read."""
        values = [
            self.last_metas[d].update_interval_s
            for m, _, d in self.domains()
            if self.component_of_model(m) == component and d in self.last_metas
        ]
        return max(values) if values else None

    def _allowed(self, init: datetime) -> bool:
        return self.cycle_hours is None or init.hour in self.cycle_hours

    def labelled_init(self, meta: ModelMeta, now: datetime) -> datetime:
        """The run this provider treats as current for one domain (see module docstring)."""
        if meta.ready(now, self.wait) and self._allowed(meta.init_time):
            return meta.init_time
        step = timedelta(seconds=meta.update_interval_s)
        t = meta.init_time - step
        for _ in range(48):
            if self._allowed(t):
                return t
            t -= step
        return meta.init_time - step

    def component_of_model(self, model: str) -> str:
        """The component an API model feeds: ``wave`` for the marine models."""
        return WAVE_COMPONENT if model in self.wave_models() else ATMOSPHERE

    def current_run(self, now: datetime, *, partial: bool = False) -> RunInfo:
        """The run of every domain this provider reads (metadata only).

        ``partial``: when one component's metadata cannot be read (its host timed out or
        answered an error), return the other component's run as a :class:`PartlyReadRun`
        that names why (``unread``) instead of raising, so a refresh plans and fetches the
        readable component alone (design D6). Raises when nothing can be read, and without
        ``partial`` when anything cannot.
        """
        require_commercial_licence(self.settings)
        metas, unread = self._component_metas()
        if unread and (not partial or len(unread) == len(COMPONENT_ORDER)):
            raise next(iter(unread.values()))
        refs: list[ModelRunRef] = []
        seen: set[str] = set()
        for model, _, domain in self.domains():
            if domain in seen or self.component_of_model(model) in unread:
                continue
            seen.add(domain)
            meta = metas[domain]
            init = self.labelled_init(meta, now)
            available = meta.available_at if init == meta.init_time else None
            refs.append(
                ModelRunRef(
                    model=domain,
                    init_time=init,
                    available_at=available,
                    component=self.component_of_model(model),
                )
            )
        by_domain = {r.model: r.init_time for r in refs}
        wave_domains = {d for m, _, d in self.domains() if m in self.wave_models()}
        atmos_domains = {d for m, _, d in self.domains() if m == self.atmos_model()}

        def part(r: ModelRunRef) -> str:
            return f"{r.model.removeprefix('ncep_')}:{r.init_time:%Y%m%d%H}"

        keys = {
            c: "|".join(part(r) for r in refs if r.component == c)
            for c in COMPONENT_ORDER
            if c not in unread
        }
        run = RunInfo(
            run_key="|".join(part(r) for r in refs),
            issued_at=min(by_domain.values()),
            wave_model_run_at=min(
                (by_domain[d] for d in wave_domains if d in by_domain), default=None
            ),
            atmosphere_model_run_at=min(
                (by_domain[d] for d in atmos_domains if d in by_domain), default=None
            ),
            metadata_source="open_meteo_meta",
            model_runs=tuple(refs),
            component_keys=MappingProxyType(keys),
        )
        if not unread:
            return run
        values = {f.name: getattr(run, f.name) for f in fields(RunInfo)}
        return PartlyReadRun(**values, unread=MappingProxyType(dict(unread)))

    def ensure_servable(
        self,
        run: RunInfo,
        now: datetime | None = None,
        components: frozenset[str] | None = None,
    ) -> dict[str, ModelMeta]:
        """Raise :class:`RunNotServedError` unless Open-Meteo serves exactly ``run`` now.

        ``components``: check only the domains of these components (a refresh that fetches
        the atmosphere does not wait for a wave run being published, nor reads the wave
        metadata); None = every component the run names.
        Called by ingestion before a run is recorded, and again by :meth:`fetch`.
        """
        now = now or self.clock()
        if components is None:
            # Every component the run names (a partly read run names one).
            named = {ref.component for ref in run.model_runs}
            components = (
                COMPONENTS
                if None in named or not named
                else frozenset(c for c in named if c is not None)
            )
        metas = self._metas_of(components)
        labelled = {
            ref.model: ref.init_time
            for ref in run.model_runs
            if ref.component is None or ref.component in components
        }
        for domain, init in labelled.items():
            meta = metas.get(domain)
            if meta is None:
                raise RunNotServedError(f"Open-Meteo has no metadata for {domain}")
            if meta.init_time == init and meta.ready(now, self.wait):
                continue
            if meta.init_time > init and not meta.ready(now, self.wait):
                assert meta.available_at is not None  # ready() is True without it
                wait_s = (meta.available_at + self.wait - now).total_seconds()
                raise RunNotServedError(
                    f"Open-Meteo is publishing {domain} run {meta.init_time:%Y-%m-%d %H}Z "
                    f"(available {meta.available_at:%H:%M} UTC); it is read "
                    f"{self.wait.total_seconds() / 60:g} min after that",
                    retry_after_s=max(30.0, wait_s + 30.0),
                )
            if meta.init_time > init and self._allowed(meta.init_time):
                raise RunNotServedError(
                    f"Open-Meteo published {domain} run {meta.init_time:%Y-%m-%d %H}Z while run "
                    f"{init:%Y-%m-%d %H}Z was being acquired; the newer run is read on the retry",
                    retry_after_s=60.0,
                )
            if meta.init_time > init:
                raise RunNotServedError(
                    f"Open-Meteo now serves {domain} run {meta.init_time:%Y-%m-%d %H}Z, which "
                    f"this source does not read (only {self._cycles_text()} runs), and fills "
                    f"the hours after its horizon from older runs: run {init:%Y-%m-%d %H}Z can "
                    "no longer be read unmixed; the next one is read once it is published"
                )
            raise RunNotServedError(
                f"Open-Meteo serves {domain} run {meta.init_time:%Y-%m-%d %H}Z, older than the "
                f"run being acquired ({init:%Y-%m-%d %H}Z)",
                retry_after_s=300.0,
            )
        return metas

    def horizon_end(self, component: str, init: datetime, now: datetime) -> datetime | None:
        """A request's ``forecast_days`` window runs from today 00:00 GMT for ``days``
        days, and :meth:`_records` keeps the hours up to ``days`` after the run: a run read
        on a later UTC day reaches further (the horizon rolls 24 h a day)."""
        today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return min(
            init + timedelta(hours=self.days * 24),
            today + timedelta(hours=self.days * 24 - 1),
        )

    def _cycles_text(self) -> str:
        hours = self.cycle_hours or ()
        return "/".join(f"{h:02d}" for h in hours) + "Z"

    # ---------------------------------------------------------------- requests
    def _charge(self, state: _Fetch, weight: float) -> Callable[[], None]:
        def charge() -> None:
            consume_open_meteo_budget(weight, settings=self.settings)
            state.weighted_calls += weight
            self.weighted_calls_total += weight

        return charge

    def _request(
        self,
        state: _Fetch,
        host: HostKind,
        path: str,
        model: str,
        lats: list[float],
        lons: list[float],
        variables: list[str],
        extra: dict[str, Any],
    ) -> list[dict[str, Any]]:
        require_commercial_licence(self.settings)
        cell_selection = self.settings.open_meteo_cell_selection
        params = {
            "latitude": ",".join(f"{x:.4f}" for x in lats),
            "longitude": ",".join(f"{x:.4f}" for x in lons),
            "hourly": ",".join(variables),
            "models": model,
            "forecast_days": self.days,
            "timezone": "GMT",
            "timeformat": "unixtime",
            "cell_selection": cell_selection,
            **extra,
        }
        runs = {
            d: state.labelled[d] for m, _, d in self.domains() if m == model and d in state.labelled
        }
        inits = list(runs.values())
        # Every domain's run is part of the key: gfs_global reads GFS 0.13 and 0.25 deg,
        # which Open-Meteo publishes minutes apart, so a body fetched while only one of them
        # was new must not be served for the run in which both are.
        run_ids = ",".join(f"{d}:{t:%Y%m%d%H%M}" for d, t in sorted(runs.items()))
        key = ProviderCacheKey(
            provider=self.code,
            model=model,
            run_init_time=max(inits) if inits else None,
            coords_hash=coords_hash(
                lats, lons, cell_selection, str(self.days), json.dumps(extra), run_ids
            ),
            variables=",".join(variables),
        )
        body = provider_cache.get(key, self.settings) if inits else None
        if body is not None:
            state.cache_hits += 1
        else:
            weight = call_weight(len(lats), len(variables), self.days)
            state.data_requests += 1
            resp = request_with_retries(
                self.client,
                "GET",
                f"{self.hosts[host]}{path}",
                provider=HTTP_PROVIDER,
                backoff_seconds=self.backoff_seconds,
                retry_statuses=RETRYABLE_STATUS - {429},
                before_attempt=self._charge(state, weight),
                params=self._params(params),
            )
            self._raise_for_status(resp)
            body = resp.content
            ttl = max(
                (state.metas[d].update_interval_s for m, _, d in self.domains() if m == model),
                default=DEFAULT_UPDATE_INTERVAL_S,
            )
            if inits:
                state.cache_writes.append((key, body, ttl))
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise TransientProviderError(f"Open-Meteo returned unreadable JSON: {exc}") from exc
        items = data if isinstance(data, list) else [data]
        if len(items) != len(lats):
            raise PermanentProviderError(
                f"Open-Meteo returned {len(items)} locations for {len(lats)} requested"
            )
        return [i for i in items if isinstance(i, dict)]

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code == 200:
            return
        reason = _reason(resp)
        if resp.status_code == 429:
            low = reason.lower()
            if "daily" in low or "monthly" in low:
                # Nothing succeeds before the limit resets: the sync waits until then
                # (assumed 00:00 UTC for the daily limit, the 1st for the monthly one).
                now = self.clock()
                wait = (
                    seconds_until_next_utc_month(now)
                    if "monthly" in low
                    else seconds_until_next_utc_day(now)
                )
                raise QuotaWindowExhaustedError(f"Open-Meteo: {reason}", retry_after_s=wait)
            retry = _next_hour(self.clock()) if "hourly" in low else 60.0
            raise TransientProviderError(f"Open-Meteo: {reason}", retry_after_s=retry)
        if resp.status_code >= 500:
            raise TransientProviderError(f"Open-Meteo HTTP {resp.status_code}: {reason}")
        raise OpenMeteoError(reason, resp.status_code)

    def _fetch_waves(
        self, state: _Fetch, points: list[SpotPoint]
    ) -> tuple[dict[int, Series], dict[int, str]]:
        """Each spot's wave series from the first model whose domain holds its point."""
        models = self.wave_models()
        route: dict[int, int] = {}
        for p in points:
            idx = next(
                (
                    i
                    for i, m in enumerate(models)
                    if all(
                        state.metas[d].contains(p.forecast_latitude, p.forecast_longitude)
                        for mm, _, d in self.domains()
                        if mm == m
                    )
                ),
                len(models) - 1,
            )
            route[p.spot_id] = idx
        series: dict[int, Series] = {}
        errors: dict[int, str] = {}
        for idx, model in enumerate(models):
            group = [p for p in points if route.get(p.spot_id) == idx]
            for chunk in _chunks(group, self.settings.forecast_batch_size):
                self._wave_chunk(state, model, idx, chunk, route, series, errors)
        self._neighbour_cells(state, points, series)
        return series, errors

    def _neighbour_cells(
        self, state: _Fetch, points: list[SpotPoint], series: dict[int, Series]
    ) -> None:
        """Replace a wave cell without data in any hour by the nearest neighbouring cell
        that has data.

        The direct GRIB ingesters sample the nearest valid wave cell; Open-Meteo's
        cell_selection=sea can still answer from a cell the wave model masks. The eight
        cells one grid step around it (inside the model's domain) are read in one request
        per model, and the one nearest the forecast point with data, at most
        GRIB_MAX_CELL_DISTANCE_KM away, is used (recorded in the run's warnings and the
        spot's extra.wave_cell). Without one, the spot stays without wave data.
        """
        groups: dict[tuple[str, tuple[str, ...]], list[SpotPoint]] = {}
        for p in points:
            s = series.get(p.spot_id)
            if s is not None and not s.has_wave_height() and s.model in WAVE_GRID_STEP_DEG:
                groups.setdefault((s.model, tuple(s.mapping)), []).append(p)
        limit = self.settings.grib_max_cell_distance_km
        for (model, _), group in groups.items():
            step = WAVE_GRID_STEP_DEG[model]
            mapping = series[group[0].spot_id].mapping
            metas = [state.metas[d] for m, _, d in self.domains() if m == model]
            owners: list[tuple[SpotPoint, float, float]] = []
            for p in group:
                item = series[p.spot_id].item
                clat = float(item.get("latitude", p.forecast_latitude))
                clon = float(item.get("longitude", p.forecast_longitude))
                for dj, di in itertools.product((-1, 0, 1), repeat=2):
                    lat, lon = clat + dj * step, (clon + di * step + 180.0) % 360.0 - 180.0
                    if (dj or di) and -90 <= lat <= 90 and all(m.contains(lat, lon) for m in metas):
                        owners.append((p, lat, lon))
            found: dict[int, tuple[float, Series]] = {}
            size = max(1, self.settings.forecast_batch_size)
            for start in range(0, len(owners), size):
                chunk = owners[start : start + size]
                try:
                    items = self._request(
                        state,
                        "marine",
                        "/v1/marine",
                        model,
                        [o[1] for o in chunk],
                        [o[2] for o in chunk],
                        list(mapping),
                        {},
                    )
                except OpenMeteoError as exc:
                    state.warnings.append(f"{model}: neighbouring cells not read ({exc.reason})")
                    break
                for (p, _, _), item in zip(chunk, items, strict=True):
                    candidate = Series(model, item, mapping)
                    cell = _cell(item, p.forecast_latitude, p.forecast_longitude)
                    if cell is None or cell["distance_km"] > limit:
                        continue
                    best = found.get(p.spot_id)
                    if candidate.has_wave_height() and (
                        best is None or cell["distance_km"] < best[0]
                    ):
                        found[p.spot_id] = (cell["distance_km"], candidate)
            for p in group:
                old = series[p.spot_id].item
                where = f"cell ({old.get('latitude')}, {old.get('longitude')})"
                if p.spot_id not in found:
                    state.warnings.append(
                        f"{p.slug}: {model} {where} has no wave data, nor has any neighbouring "
                        f"cell within {limit:g} km"
                    )
                    continue
                distance, replacement = found[p.spot_id]
                series[p.spot_id] = replacement
                new = replacement.item
                state.warnings.append(
                    f"{p.slug}: {model} {where} has no wave data; the neighbouring cell "
                    f"({new.get('latitude')}, {new.get('longitude')}), {distance:g} km from "
                    "the forecast point, is used"
                )

    def _wave_chunk(
        self,
        state: _Fetch,
        model: str,
        idx: int,
        chunk: list[SpotPoint],
        route: dict[int, int],
        series: dict[int, Series],
        errors: dict[int, str],
        optional: bool = True,
    ) -> None:
        mapping = self.wave_variables(optional)
        lats = [p.forecast_latitude for p in chunk]
        lons = [p.forecast_longitude for p in chunk]
        try:
            items = self._request(
                state, "marine", "/v1/marine", model, lats, lons, list(mapping), {}
            )
        except OpenMeteoError as exc:
            optional_names = set(self.wave_variables(True)) - set(self.wave_variables(False))
            if optional and exc.status == 400 and any(n in exc.reason for n in optional_names):
                logger.info("Open-Meteo %s rejected optional variables (%s)", model, exc.reason)
                state.warnings.append(f"{model}: optional variables dropped ({exc.reason})")
                self._wave_chunk(state, model, idx, chunk, route, series, errors, optional=False)
                return
            if exc.status == 400 and len(chunk) > 1:
                # One point outside the model's domain fails the whole request.
                for p in chunk:
                    self._wave_chunk(state, model, idx, [p], route, series, errors, optional)
                return
            if exc.status == 400 and idx + 1 < len(self.wave_models()):
                nxt = self.wave_models()[idx + 1]
                state.warnings.append(f"{chunk[0].slug}: {model} refused ({exc.reason}); {nxt}")
                route[chunk[0].spot_id] = idx + 1  # read with the next model's group
                return
            for p in chunk:
                errors[p.spot_id] = f"wave data ({model}): {exc.reason}"
            return
        for p, item in zip(chunk, items, strict=True):
            series[p.spot_id] = Series(model, item, mapping)
            errors.pop(p.spot_id, None)

    def _fetch_atmos(
        self, state: _Fetch, points: list[SpotPoint]
    ) -> tuple[dict[int, Series], dict[int, str]]:
        model = self.atmos_model()
        series: dict[int, Series] = {}
        errors: dict[int, str] = {}
        pending = [list(c) for c in _chunks(points, self.settings.forecast_batch_size)]
        while pending:
            chunk = pending.pop(0)
            try:
                items = self._request(
                    state,
                    "api",
                    self.atmos_path,
                    model,
                    [p.latitude for p in chunk],
                    [p.longitude for p in chunk],
                    list(ATMOS),
                    {"wind_speed_unit": "kmh"},
                )
            except OpenMeteoError as exc:
                if exc.status == 400 and len(chunk) > 1:
                    pending = [[p] for p in chunk] + pending
                    continue
                for p in chunk:
                    errors[p.spot_id] = f"{model}: {exc.reason}"
                continue
            for p, item in zip(chunk, items, strict=True):
                series[p.spot_id] = Series(model, item, ATMOS)
        return series, errors

    # ---------------------------------------------------------------- fetch
    def fetch(
        self,
        spots: list[SpotPoint],
        run: RunInfo,
        components: frozenset[str] | None = None,
    ) -> FetchResult:
        """Waves and wind for every spot, for exactly the run ``run`` names.

        ``components``: only the wave (marine requests) or the atmosphere (forecast
        requests) part; None = both. The run's domains of the other component are neither
        checked nor requested (a refresh carries that part from a stored run)."""
        require_commercial_licence(self.settings)
        wanted = COMPONENTS if components is None else frozenset(components) & COMPONENTS
        if not wanted:
            raise ValueError(f"components must name {sorted(COMPONENTS)}, not {components!r}")
        result = FetchResult(components=wanted)
        before = self.tally.snapshot()
        if not run.model_runs:
            raise PermanentProviderError(
                f"run {run.run_key!r} lists no model runs: an Open-Meteo run is identified by "
                "current_run()"
            )
        metas = self.ensure_servable(run, components=None if wanted == COMPONENTS else wanted)
        labelled = {
            r.model: r.init_time
            for r in run.model_runs
            if r.component is None or r.component in wanted
        }
        unnamed = sorted(
            c for c in wanted if not any(r.component in (None, c) for r in run.model_runs)
        )
        if unnamed:
            raise TransientProviderError(
                f"run {run.run_key!r} names no {unnamed[0]} model run (its metadata was not "
                "read): that component cannot be fetched"
            )
        state = _Fetch(labelled=labelled, metas=metas)
        want_waves, want_atmos = WAVE_COMPONENT in wanted, ATMOSPHERE in wanted
        try:
            waves, wave_errors = self._fetch_waves(state, spots) if want_waves else ({}, {})
            atmos, atmos_errors = (
                self._fetch_atmos(state, spots)
                if want_atmos and (waves or not want_waves)
                else ({}, {})
            )
        except ProviderError:
            # A later request failed (a call budget, a timeout): keep the responses that
            # arrived for this run, so the task's retry does not download and pay for them
            # again (only if the run is still the one Open-Meteo serves).
            with contextlib.suppress(ProviderError):
                if not self._changed_runs(state):
                    self._save_responses(state)
            raise
        if waves or atmos:
            changed = self._changed_runs(state)
            if changed:
                raise TransientProviderError(
                    "Open-Meteo published a new run during the fetch ("
                    + ", ".join(changed)
                    + "): the responses may mix runs, so nothing is stored",
                    retry_after_s=self.wait.total_seconds() + 60.0,
                )
        self._save_responses(state)

        for p in spots:
            if not want_waves:
                if p.spot_id not in atmos:
                    error = atmos_errors.get(p.spot_id, "missing")
                    result.errors[p.spot_id] = f"atmosphere data: {error}"
                    continue
                records = self._atmos_records(p, atmos[p.spot_id], state)
                if not records:
                    result.errors[p.spot_id] = "no wind data at the spot"
                    continue
                result.records[p.spot_id] = records
                continue
            if p.spot_id not in waves:
                result.errors[p.spot_id] = wave_errors.get(p.spot_id, "wave data: missing")
                continue
            records = self._records(p, waves[p.spot_id], atmos.get(p.spot_id), state)
            if not records:
                result.errors[p.spot_id] = (
                    "no wave data at the forecast point (land or outside model)"
                )
                continue
            result.records[p.spot_id] = records
            if want_atmos and (p.spot_id in atmos_errors or p.spot_id not in atmos):
                reason = atmos_errors.get(p.spot_id, "missing")
                result.errors[p.spot_id] = f"incomplete: wind and pressure unavailable ({reason})"
        result.stats = FetchStats.between(
            before, self.tally.snapshot(), round(state.weighted_calls, 3)
        )
        result.request_count = result.stats.requests
        result.warnings.insert(
            0,
            f"Open-Meteo: {state.data_requests} data requests ({state.cache_hits} more served "
            f"from the cache), {state.weighted_calls:.1f} weighted calls, "
            f"{result.stats.bytes_downloaded / 1e3:.1f} kB in {result.request_count} HTTP "
            "responses",
        )
        result.warnings.extend(state.warnings)
        return result

    def _changed_runs(self, state: _Fetch) -> list[str]:
        """Domains whose current run differs from the one being fetched (meta.json, free;
        only the fetched components' domains are read)."""
        after = self._metas_of(
            {self.component_of_model(m) for m, _, d in self.domains() if d in state.labelled}
        )
        return [
            f"{d} {after[d].init_time:%Y-%m-%d %H}Z"
            for d, init in state.labelled.items()
            if d in after and after[d].init_time != init
        ]

    def _save_responses(self, state: _Fetch) -> None:
        for key, body, ttl in state.cache_writes:
            provider_cache.put(key, body, ttl, self.settings)
        state.cache_writes.clear()

    def _model_meta(self, state: _Fetch, model: str) -> tuple[datetime, datetime | None, int]:
        """(run init, data end, temporal resolution in s) of one requested model.

        A model read from several metadata domains (gfs_global: GFS 0.13 and 0.25 deg)
        takes the earliest init and end, so no hour of a newer or longer run slips in. A
        model of a component this fetch does not request has no metadata here (a wave-only
        fetch reads no atmosphere metadata): no end, hourly.
        """
        domains = [d for m, _, d in self.domains() if m == model and d in state.metas]
        inits = [state.labelled[d] for d in domains if d in state.labelled]
        ends = [e for d in domains if (e := state.metas[d].data_end) is not None]
        resolutions = [r for d in domains if (r := state.metas[d].temporal_resolution_s)]
        init = min(inits) if inits else min(state.labelled.values())
        return init, (min(ends) if ends else None), (min(resolutions) if resolutions else 3600)

    def _records(
        self, p: SpotPoint, wave: Series, atmos: Series | None, state: _Fetch
    ) -> list[ForecastRecord]:
        atmos_model = self.atmos_model()
        wave_init, wave_end, wave_res = self._model_meta(state, wave.model)
        atmos_init, atmos_end, atmos_res = self._model_meta(state, atmos_model)
        max_lead = self.days * 24
        native_only = self.settings.open_meteo_native_steps_only
        atmos_rows = atmos.by_time() if atmos is not None else {}
        records: list[ForecastRecord] = []
        for ts, row in sorted(wave.by_time().items()):
            t = datetime.fromtimestamp(ts, tz=UTC)
            lead = (t - wave_init).total_seconds() / 3600.0
            # Before the run's initialisation or after its last step: other runs' data.
            if lead < 0 or lead > max_lead or (wave_end is not None and t >= wave_end):
                continue
            native = is_native_step(wave.model, lead, wave_res)
            if native_only and not native:
                continue
            values: dict[str, float | None] = dict.fromkeys(FORECAST_FIELDS)
            values.update({k: v for k, v in row.items() if k in values})
            values = _clean(values)
            if all(values[k] is None for k in row if k in values):
                continue  # masked cell (land, ice) at this hour
            extra: dict[str, object] = {}
            interpolated = [] if native else [k for k in row if values.get(k) is not None]
            a_row = atmos_rows.get(ts)
            a_lead = (t - atmos_init).total_seconds() / 3600.0
            if a_row is not None and a_lead >= 0 and (atmos_end is None or t < atmos_end):
                values.update(a_row)
                values = _clean(values)
                if not is_native_step(atmos_model, a_lead, atmos_res):
                    interpolated += [k for k in ATMOS_FIELDS if values.get(k) is not None]
                if values.get("wind_gust_kmh") is not None:
                    extra[EXTRA_GUST_KIND] = self.gust_kind_at()
            step = native_step_hours(wave.model, lead, wave_res)
            if step is not None:
                extra[EXTRA_NATIVE_STEP_S] = step * 3600
            if interpolated:
                extra[EXTRA_INTERPOLATED] = True
                extra["interpolated_fields"] = sorted(interpolated)
            extra.update({k: v for k, v in self.wave_extra(row).items() if v is not None})
            records.append(
                ForecastRecord(
                    valid_time=t, values=values, period_kind=self.period_kind, extra=extra
                )
            )
        if records:
            first = records[0].extra
            first[EXTRA_WAVE_MODEL] = wave.model
            wave_cell = _cell(wave.item, p.forecast_latitude, p.forecast_longitude)
            if wave_cell is not None:
                first[EXTRA_WAVE_CELL] = wave_cell
            if atmos is not None:
                wind_cell = _cell(atmos.item, p.latitude, p.longitude)
                if wind_cell is not None:
                    first[EXTRA_WIND_CELL] = {**wind_cell, "model": atmos_model}
        return records

    def _atmos_records(self, p: SpotPoint, atmos: Series, state: _Fetch) -> list[ForecastRecord]:
        """The atmosphere-only records of an atmosphere refresh (the wave part is carried
        from a stored run): the atmosphere model's own steps of its run, at the break."""
        model = self.atmos_model()
        init, end, resolution = self._model_meta(state, model)
        max_lead = self.days * 24
        native_only = self.settings.open_meteo_native_steps_only
        records: list[ForecastRecord] = []
        for ts, row in sorted(atmos.by_time().items()):
            t = datetime.fromtimestamp(ts, tz=UTC)
            lead = (t - init).total_seconds() / 3600.0
            if lead < 0 or lead > max_lead or (end is not None and t >= end):
                continue
            native = is_native_step(model, lead, resolution)
            if native_only and not native:
                continue
            values: dict[str, float | None] = dict.fromkeys(FORECAST_FIELDS)
            values.update({k: v for k, v in row.items() if k in values})
            values = _clean(values)
            if all(values[k] is None for k in ATMOS_FIELDS):
                continue
            extra: dict[str, object] = {}
            if values.get("wind_gust_kmh") is not None:
                extra[EXTRA_GUST_KIND] = self.gust_kind_at()
            step = native_step_hours(model, lead, resolution)
            if step is not None:
                extra[EXTRA_NATIVE_STEP_S] = step * 3600
            if not native:
                extra[EXTRA_INTERPOLATED] = True
                extra["interpolated_fields"] = sorted(
                    k for k in ATMOS_FIELDS if values.get(k) is not None
                )
            records.append(ForecastRecord(valid_time=t, values=values, extra=extra))
        if records:
            wind_cell = _cell(atmos.item, p.latitude, p.longitude)
            if wind_cell is not None:
                records[0].extra[EXTRA_WIND_CELL] = {**wind_cell, "model": model}
        return records


def _chunks(items: Sequence[SpotPoint], size: int) -> Iterable[list[SpotPoint]]:
    for i in range(0, len(items), max(1, size)):
        yield list(items[i : i + size])


class OpenMeteoProvider(OpenMeteoFamilyProvider):
    """NOAA GFS + GFS-Wave (WAVEWATCH III) via Open-Meteo: the ``ncep_gfs`` family."""

    code = "open_meteo_noaa"
    family = "ncep_gfs"
    atmos_path = "/v1/gfs"
    max_forecast_days = 16
    period_kind = "tp"  # GFS-Wave wave_period is PERPW, the peak period

    def wave_models(self) -> list[str]:
        return self.settings.open_meteo_gfs_wave_model_list

    def atmos_model(self) -> str:
        return self.settings.open_meteo_gfs_atmos_model_name

    def wave_variables(self, optional: bool) -> dict[str, str]:
        return {**GFS_WAVE_CORE, **(GFS_WAVE_OPTIONAL if optional else {})}

    def gust_kind_at(self) -> str:
        # GFS GUST is an instantaneous surface diagnostic; HRRR (gfs_seamless) is not.
        model = self.atmos_model()
        return "gfs_instantaneous" if model in ("gfs_global", "gfs025", "gfs013") else "unknown"

    def display_name(self) -> str:
        return "NOAA GFS + GFS-Wave (WAVEWATCH III) via Open-Meteo"

    def attribution(self) -> str:
        return (
            f"Weather data by Open-Meteo.com ({ATTRIBUTION_LINK}, CC BY 4.0); model data NOAA "
            "NCEP GFS and GFS-Wave (WAVEWATCH III). Modified: sampled at Open-Meteo's nearest "
            "sea grid cell, hours Open-Meteo interpolated are not stored, absent swell "
            "partitions are left empty."
        )
