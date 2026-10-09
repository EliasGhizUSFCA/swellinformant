"""Forecast provider interface and normalised record types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.services.surf_quality.models import SpotParams

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


@dataclass(frozen=True)
class RunInfo:
    run_key: str
    issued_at: datetime
    wave_model_run_at: datetime | None = None
    atmosphere_model_run_at: datetime | None = None
    metadata_source: str = "provider"


@dataclass
class FetchResult:
    records: dict[int, list[ForecastRecord]] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    request_count: int = 0
    warnings: list[str] = field(default_factory=list)


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
