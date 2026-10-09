"""Plain data containers for the scoring engine (decoupled from the ORM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpotParams:
    slug: str
    latitude: float
    longitude: float
    swell_window_min: float
    swell_window_max: float
    optimal_swell_direction_min: float
    optimal_swell_direction_max: float
    offshore_wind_direction: float
    min_swell_period_s: float
    ideal_swell_period_s: float
    wave_height_min_ft: float
    wave_height_max_ft: float
    height_factor: float = 1.0
    tide_preference: str | None = None
    calibration: dict[str, Any] | None = None

    @classmethod
    def from_orm(cls, spot: Any) -> SpotParams:
        return cls(
            slug=spot.slug,
            latitude=spot.latitude,
            longitude=spot.longitude,
            swell_window_min=spot.swell_window_min,
            swell_window_max=spot.swell_window_max,
            optimal_swell_direction_min=spot.optimal_swell_direction_min,
            optimal_swell_direction_max=spot.optimal_swell_direction_max,
            offshore_wind_direction=spot.offshore_wind_direction,
            min_swell_period_s=spot.min_swell_period_s,
            ideal_swell_period_s=spot.ideal_swell_period_s,
            wave_height_min_ft=spot.wave_height_min_ft,
            wave_height_max_ft=spot.wave_height_max_ft,
            height_factor=spot.height_factor,
            tide_preference=str(spot.tide_preference) if spot.tide_preference else None,
            calibration=spot.calibration,
        )


@dataclass(frozen=True)
class SeaState:
    """Offshore conditions for one valid time (metres, seconds, degrees FROM, km/h)."""

    sig_wave_height_m: float | None = None
    wave_period_s: float | None = None
    wave_direction_deg: float | None = None
    primary_swell_height_m: float | None = None
    primary_swell_period_s: float | None = None
    primary_swell_direction_deg: float | None = None
    secondary_swell_height_m: float | None = None
    secondary_swell_period_s: float | None = None
    secondary_swell_direction_deg: float | None = None
    wind_wave_height_m: float | None = None
    wind_wave_period_s: float | None = None
    wind_wave_direction_deg: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    wind_gust_kmh: float | None = None
    # 0 = local low tide, 1 = local high tide (derived from the sea-level series)
    tide_stage: float | None = None

    @classmethod
    def from_orm(cls, row: Any, tide_stage: float | None = None) -> SeaState:
        return cls(
            sig_wave_height_m=row.sig_wave_height_m,
            wave_period_s=row.wave_period_s,
            wave_direction_deg=row.wave_direction_deg,
            primary_swell_height_m=row.primary_swell_height_m,
            primary_swell_period_s=row.primary_swell_period_s,
            primary_swell_direction_deg=row.primary_swell_direction_deg,
            secondary_swell_height_m=row.secondary_swell_height_m,
            secondary_swell_period_s=row.secondary_swell_period_s,
            secondary_swell_direction_deg=row.secondary_swell_direction_deg,
            wind_wave_height_m=row.wind_wave_height_m,
            wind_wave_period_s=row.wind_wave_period_s,
            wind_wave_direction_deg=row.wind_wave_direction_deg,
            wind_speed_kmh=row.wind_speed_kmh,
            wind_direction_deg=row.wind_direction_deg,
            wind_gust_kmh=row.wind_gust_kmh,
            tide_stage=tide_stage,
        )


@dataclass
class BreakingEstimate:
    min_ft: float
    max_ft: float
    mid_ft: float
    method: str
    calibrated: bool
    partitions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QualityResult:
    score: int
    label: str
    components: dict[str, float]
    weights_used: dict[str, float]
    breaking: BreakingEstimate
    wind_relation: str
    explanation: str
