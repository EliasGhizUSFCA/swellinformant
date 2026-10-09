"""Surf spot, forecast and swell event schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class AirportOut(ORMModel):
    iata: str
    name: str
    city: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    timezone: str


class SpotAirportOut(BaseModel):
    airport: AirportOut
    is_primary: bool
    transfer_minutes: int
    transfer_mode: str
    notes: str


class Conditions(BaseModel):
    valid_time: datetime
    score: int
    label: str
    breaking_height_min_ft: float
    breaking_height_max_ft: float
    swell_height_m: float | None
    swell_period_s: float | None
    swell_direction_deg: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_relation: str
    confidence: int
    confidence_label: str
    is_daylight: bool


class SpotMini(BaseModel):
    id: int
    slug: str
    name: str
    country: str
    country_code: str
    region: str
    region_group: str
    latitude: float
    longitude: float
    timezone: str
    break_type: str


class EventOut(BaseModel):
    id: uuid.UUID
    spot: SpotMini
    status: str
    start_time: datetime
    end_time: datetime
    peak_time: datetime
    peak_score: int
    avg_score: float
    peak_label: str
    peak_breaking_height_min_ft: float
    peak_breaking_height_max_ft: float
    peak_swell_height_m: float | None
    peak_swell_period_s: float | None
    peak_swell_direction_deg: float | None
    peak_wind_speed_kmh: float | None
    peak_wind_direction_deg: float | None
    peak_wind_relation: str
    qualifying_hours: int
    confidence: int
    confidence_label: str
    lead_days: float
    version: int
    first_detected_at: datetime
    last_updated_at: datetime
    is_demo: bool
    source_code: str
    history: list[dict[str, Any]] = []


class SpotSummary(SpotMini):
    wave_direction: str
    is_big_wave: bool
    skill_level: str
    wave_height_min_ft: float
    wave_height_max_ft: float
    best_months: list[int]
    primary_airport: str | None
    current: Conditions | None = None
    best_upcoming: Conditions | None = None
    next_event: EventOut | None = None
    upcoming_event_count: int = 0


class SpotDetail(SpotSummary):
    forecast_latitude: float
    forecast_longitude: float
    swell_window_min: float
    swell_window_max: float
    optimal_swell_direction_min: float
    optimal_swell_direction_max: float
    offshore_wind_direction: float
    min_swell_period_s: float
    ideal_swell_period_s: float
    tide_preference: str | None
    height_factor: float
    calibrated: bool
    seasonality: str
    accessibility_notes: str
    hazards: str
    description: str
    airports: list[SpotAirportOut]


class ForecastPoint(BaseModel):
    valid_time: datetime
    is_daylight: bool
    sig_wave_height_m: float | None
    primary_swell_height_m: float | None
    primary_swell_period_s: float | None
    primary_swell_direction_deg: float | None
    secondary_swell_height_m: float | None
    secondary_swell_period_s: float | None
    secondary_swell_direction_deg: float | None
    wind_wave_height_m: float | None
    wind_wave_period_s: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_gust_kmh: float | None
    pressure_msl_hpa: float | None
    sea_level_m: float | None
    wave_power_kw_m: float | None
    breaking_height_min_ft: float
    breaking_height_max_ft: float
    score: int
    label: str
    confidence: int
    confidence_label: str
    wind_relation: str
    explanation: str
    components: dict[str, Any]


class DailySummary(BaseModel):
    date: date
    best_score: int
    best_label: str
    best_time: datetime | None
    height_min_ft: float
    height_max_ft: float
    avg_wind_kmh: float | None
    good_hours: int
    confidence: int


class ForecastSourceOut(BaseModel):
    code: str
    name: str
    is_demo: bool
    attribution: str
    wave_model: str | None
    atmosphere_model: str | None


class ForecastRunOut(BaseModel):
    run_key: str
    issued_at: datetime
    completed_at: datetime | None
    wave_model_run_at: datetime | None
    atmosphere_model_run_at: datetime | None
    status: str


class SpotForecastOut(BaseModel):
    spot_slug: str
    timezone: str
    available: bool
    message: str | None = None
    source: ForecastSourceOut | None = None
    run: ForecastRunOut | None = None
    stale: bool = False
    breaking_height_method: str | None = None
    calibrated: bool = False
    points: list[ForecastPoint] = []
    daily: list[DailySummary] = []


class RegionOut(BaseModel):
    region_group: str
    spot_count: int
    countries: list[dict[str, str]]
