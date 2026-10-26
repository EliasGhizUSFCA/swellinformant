"""Forecast provider interface and normalised record types.

Conventions of :class:`ForecastRecord` values: UTC valid times; directions in degrees FROM;
wind in km/h, heights in m, periods in s. A partition (secondary swell, wind sea, ...) that
the model does not have at a time is None, never 0. Optional per-record metadata goes in
``extra`` under the EXTRA_* keys below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.services.forecasts.fields import EncodedField
from app.services.surf_quality.models import SpotParams

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


@dataclass(frozen=True)
class RunInfo:
    run_key: str
    issued_at: datetime
    wave_model_run_at: datetime | None = None
    atmosphere_model_run_at: datetime | None = None
    metadata_source: str = "provider"
    # Every model run behind the data, stored as run.details["model_runs"].
    model_runs: tuple[ModelRunRef, ...] = ()


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
    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult: ...


def nominal_gfs_cycle(now: datetime, availability_lag_hours: float = 5.0) -> datetime:
    """Most recent 00/06/12/18Z GFS cycle that should be published by ``now``.

    NOAA GFS/GFS-Wave output is typically complete ~4–5 h after the cycle time.
    """
    t = now.astimezone(UTC) - timedelta(hours=availability_lag_hours)
    return t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
