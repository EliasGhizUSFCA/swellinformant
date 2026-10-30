"""Forecast provider interface and normalised record types.

Conventions of :class:`ForecastRecord` values: UTC valid times; directions in degrees FROM;
wind in km/h, heights in m, periods in s. A partition (secondary swell, wind sea, ...) that
the model does not have at a time is None, never 0. Optional per-record metadata goes in
``extra`` under the EXTRA_* keys below.
"""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING

from app.services.forecasts.fields import EncodedField
from app.services.surf_quality.models import SpotParams

if TYPE_CHECKING:
    import ssl

    import httpx

# ForecastRecord.extra keys shared by providers, the blend and the timeline.
EXTRA_INTERPOLATED = "interpolated"  # bool: the source interpolated this time step
EXTRA_NATIVE_STEP_S = "native_step_seconds"  # int: the model's own output spacing here
EXTRA_WIND_CELL = "wind_cell"  # {"lat", "lon", "distance_km"}: grid cell used for wind
EXTRA_WAVE_CELL = "wave_cell"  # {"lat", "lon", "distance_km"}: grid cell used for waves
EXTRA_WAVE_MODEL = "wave_model"  # str: the wave model that produced the values
# str: how the gust is defined, one of schemas.spots.GustKind ("gfs_instantaneous",
# "ecmwf_max_3h", "ecmwf_max_since_pp", "unknown").
EXTRA_GUST_KIND = "gust_kind"

FORECAST_FIELDS = (
    "sig_wave_height_m",
    "wave_period_s",
    "wave_direction_deg",
    "primary_swell_height_m",
    "primary_swell_period_s",
    "primary_swell_direction_deg",
    "secondary_swell_height_m",
    "secondary_swell_period_s",
    "secondary_swell_direction_deg",
    "tertiary_swell_height_m",
    "tertiary_swell_period_s",
    "tertiary_swell_direction_deg",
    "wind_wave_height_m",
    "wind_wave_period_s",
    "wind_wave_direction_deg",
    "wind_speed_kmh",
    "wind_direction_deg",
    "wind_gust_kmh",
    "pressure_msl_hpa",
    "sea_level_m",
)

# The two independent components of a spot forecast source (design D6): each has its own
# model run, availability time and detection state, and a refresh fetches only the due ones.
ATMOSPHERE = "atmosphere"
WAVE = "wave"
COMPONENTS: frozenset[str] = frozenset({ATMOSPHERE, WAVE})
_ATMOSPHERE_FIELDS = ("wind_speed_kmh", "wind_direction_deg", "wind_gust_kmh", "pressure_msl_hpa")
# Which FORECAST_FIELDS each component provides; together they partition FORECAST_FIELDS
# (a unit test checks it). The wave component holds the sea state, the swell partitions,
# the wind sea and the sea level when a provider has one.
COMPONENT_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        ATMOSPHERE: _ATMOSPHERE_FIELDS,
        WAVE: tuple(f for f in FORECAST_FIELDS if f not in _ATMOSPHERE_FIELDS),
    }
)


def component_of(field_name: str) -> str:
    """The component (``"atmosphere"`` or ``"wave"``) a FORECAST_FIELDS name belongs to."""
    for component, names in COMPONENT_FIELDS.items():
        if field_name in names:
            return component
    raise KeyError(f"{field_name!r} is not a forecast field")


@dataclass(frozen=True)
class SpotPoint:
    spot_id: int
    slug: str
    latitude: float  # the break (used for wind)
    longitude: float
    forecast_latitude: float  # offshore wave-model grid point
    forecast_longitude: float
    params: SpotParams | None = None  # full physical parameters (used by the demo provider)


@dataclass
class ForecastRecord:
    valid_time: datetime
    values: dict[str, float | None] = field(default_factory=dict)
    # Meaning of values["wave_period_s"]: "tp", "tm01", "tm02" or "tm10" (None = unknown).
    period_kind: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRunRef:
    """One model run a provider's data came from (e.g. HRRR and GFS for gfs_seamless)."""

    model: str
    init_time: datetime
    # When the provider published the run, if it says (e.g. Open-Meteo meta.json).
    available_at: datetime | None = None
    # The component this model run feeds ("wave" | "atmosphere"); None = not said (older
    # providers, or a model that feeds both).
    component: str | None = None


@dataclass(frozen=True)
class RunInfo:
    run_key: str
    issued_at: datetime
    wave_model_run_at: datetime | None = None
    atmosphere_model_run_at: datetime | None = None
    metadata_source: str = "provider"
    # Every model run behind the data, stored as run.details["model_runs"].
    model_runs: tuple[ModelRunRef, ...] = ()
    # The run key of each component ("wave", "atmosphere") built from its own model domains;
    # empty = the provider has the whole-source run_key only.
    component_keys: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    # The run time is an estimate, not a provider field (Windy: no model-run field; a changed
    # data fingerprint marks a new run, labelled "estimated" wherever it is shown).
    estimated: bool = False
    # The data come from a TESTING key (Windy's trial API): never stored or published.
    testing: bool = False


@dataclass(frozen=True)
class PartlyReadRun(RunInfo):
    """A run of which some components could not be read (``current_run(..., partial=True)``
    of Open-Meteo, whose meta.json host timed out, or Windy, whose sentinel request failed):
    ``unread`` holds why, by component; ``component_keys`` and ``model_runs`` name the
    readable components only (design D6: one unreadable component does not hold a new run
    of the other back)."""

    unread: Mapping[str, Exception] = field(default_factory=lambda: MappingProxyType({}))


@dataclass
class FetchStats:
    """What one fetch cost upstream, stored as run.details["stats"] (dataclasses.asdict)."""

    bytes_downloaded: int = 0
    requests: int = 0
    # Response count per HTTP status ("200", "400", ...).
    http_status_counts: dict[str, int] = field(default_factory=dict)
    # Provider-weighted call count where the provider bills that way (Open-Meteo).
    weighted_calls: float | None = None

    @classmethod
    def between(
        cls,
        before: tuple[int, int, dict[str, int]],
        after: tuple[int, int, dict[str, int]],
        weighted_calls: float | None = None,
    ) -> FetchStats:
        """What an ``HttpTally`` counted between two of its snapshots (responses, bytes,
        per-status counts; a status whose count did not change is left out)."""
        counts = {k: v - before[2].get(k, 0) for k, v in after[2].items()}
        return cls(
            bytes_downloaded=after[1] - before[1],
            requests=after[0] - before[0],
            http_status_counts={k: n for k, n in counts.items() if n},
            weighted_calls=weighted_calls,
        )


@dataclass
class FetchResult:
    records: dict[int, list[ForecastRecord]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    request_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # Global map fields read along with the spots (see forecasts.fields); stored as
    # map_fields rows of the run. Their problems are warnings, never spot errors.
    fields: list[EncodedField] = field(default_factory=list)
    field_warnings: list[str] = field(default_factory=list)
    stats: FetchStats | None = None
    # The components the records hold (a component-aware provider that was asked for one
    # reports just that one; the others always fetch both).
    components: frozenset[str] = COMPONENTS


@dataclass(frozen=True)
class SourceInfo:
    code: str
    name: str
    provider: str
    wave_model: str | None
    atmosphere_model: str | None
    is_demo: bool
    attribution: str
    license_notes: str
    # Dependency family (sources sharing model physics or forcing) and kind; see
    # app.services.forecasts.families.
    family: str = "unknown"
    kind: str = "deterministic"


class ForecastProvider(ABC):
    code: str

    @abstractmethod
    def source_info(self) -> SourceInfo: ...

    @abstractmethod
    def current_run(self, now: datetime) -> RunInfo:
        """Identify the newest upstream model run available (cheap metadata call)."""

    @abstractmethod
    def fetch(
        self,
        spots: list[SpotPoint],
        run: RunInfo,
        components: frozenset[str] | None = None,
    ) -> FetchResult:
        """Fetch ``run`` for ``spots``. ``components``: the components wanted ("wave",
        "atmosphere"); None = all. A provider that cannot fetch them separately ignores it
        and reports what it fetched in :attr:`FetchResult.components`."""

    def horizon_end(self, component: str, init: datetime, now: datetime) -> datetime | None:
        """The last valid time a fetch made at ``now`` of ``component``'s run initialised at
        ``init`` reaches (an upper bound; None = unknown). Default: FORECAST_DAYS after the
        run, as the Windy and NOAA adapters cut it. A refresh fetches a carried component
        again, from the same run, when the published forecast would otherwise end well
        before this (ingestion.refresh_source): a component-only refresh never shortens the
        rolling horizon."""
        from app.core.config import get_settings

        return init + timedelta(days=get_settings().forecast_days)


@functools.cache
def _ssl_context() -> ssl.SSLContext:
    import httpx

    return httpx.create_ssl_context()  # what httpx.Client() builds by default


def http_client(timeout: float, user_agent: str | None = None) -> httpx.Client:
    """A provider's HTTP client. All of them share one SSL context: loading the CA bundle
    into a new one costs about 50 ms CPU, and the sync daemon builds the providers for
    every check."""
    import httpx

    headers = {"User-Agent": user_agent} if user_agent else None
    return httpx.Client(timeout=timeout, headers=headers, verify=_ssl_context())


def rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def bearing(value: float | None) -> float | None:
    """Degrees in [0, 360) rounded to 0.1. 359.96 rounds to 360.0, which is 0 (GRIB SWDIR
    holds 360.0 values, and packing gives e.g. 360.002 for north)."""
    return None if value is None else round(value % 360.0, 1) % 360.0


def nominal_gfs_cycle(now: datetime, availability_lag_hours: float = 5.0) -> datetime:
    """Most recent 00/06/12/18Z GFS cycle that should be published by ``now``.

    NOAA GFS/GFS-Wave output is typically complete ~4–5 h after the cycle time.
    """
    t = now.astimezone(UTC) - timedelta(hours=availability_lag_hours)
    return t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
