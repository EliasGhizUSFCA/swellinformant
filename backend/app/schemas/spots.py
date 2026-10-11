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
    """One forecast hour at a spot. Two kinds of numbers live here, kept apart on purpose:

    * raw model data from the forecast source (offshore ``sig_wave_height_m``, swell and
      wind fields), and
    * our predictions (``breaking_height_*``, ``score``/``label``, ``confidence``).
    """

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
    # Raw model: combined significant wave height of the sea state (offshore point).
    sig_wave_height_m: float | None = None
    # Provenance
    source_code: str | None = None
    source_name: str | None = None
    is_demo: bool = False
    run_issued_at: datetime | None = None
    method: str | None = None
    algorithm_version: str | None = None
    explanation: str | None = None
    # Our prediction's central estimate (significant breaking height) and its forecast
    # uncertainty. ``uncertainty_kind`` says how the range was obtained: "heuristic" and
    # "ensemble_heuristic" are stated assumptions, not calibrated probabilities.
    breaking_height_mid_ft: float | None = None
    breaking_p10_ft: float | None = None
    breaking_p90_ft: float | None = None
    uncertainty_kind: str | None = None
    ml_model_version: str | None = None
    # Forecast confidence (the offshore forecast) and local confidence (the breaking
    # prediction at this break, which also depends on how well the spot is researched):
    # separate classes "high" | "moderate" | "low", plus short plain-language reasons.
    # These are documented classifications, not calibrated probabilities.
    forecast_confidence_class: str | None = None
    local_confidence_class: str | None = None
    confidence_reasons: list[str] = []
    lead_hours: float | None = None


class MapSpotOut(BaseModel):
    slug: str
    conditions: Conditions | None
    event_at_time: bool  # an active swell event covers the selected time
    upcoming_event_count: int


class MapSnapshotOut(BaseModel):
    """Every monitored spot at one forecast time, from cached predictions only."""

    requested_at: datetime | None
    valid_time: datetime | None
    range_start: datetime | None
    range_end: datetime | None
    step_hours: int
    spots: list[MapSpotOut]


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
    # Overall research confidence of the spot ("high" | "moderate" | "low"); None when no
    # assessment exists yet.
    research_class: str | None = None


class PhysicalProfileOut(BaseModel):
    shore_normal_deg: float
    forecast_point_depth_m: float | None
    controlling_depth_m: float | None
    focusing_ref: float
    focusing_kappa: float
    has_shadow_table: bool
    data_quality: dict[str, str]
    notes: str


class ResearchSourceOut(BaseModel):
    title: str
    url: str | None
    publisher: str
    source_type: str
    published_year: int | None
    accessed_at: datetime | None


class ResearchVariableOut(BaseModel):
    variable: str  # one of models.research.RESEARCH_VARIABLES
    label: str  # e.g. "Swell direction"
    research_class: str  # "high" | "moderate" | "low"
    summary: str  # one short sentence
    gaps: list[str]  # what is missing or conflicting, in plain language
    source_count: int


class ResearchOut(BaseModel):
    """How well the spot's ideal conditions are understood (not forecast confidence)."""

    research_class: str
    summary: str
    gaps: list[str]
    variables: list[ResearchVariableOut]
    sources: list[ResearchSourceOut]
    methodology_version: str
    assessed_at: datetime
    research_updated_at: datetime | None


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
    physical_profile: PhysicalProfileOut | None = None
    research: ResearchOut | None = None


class ForecastPoint(BaseModel):
    valid_time: datetime
    is_daylight: bool
    sig_wave_height_m: float | None
    wave_period_s: float | None = None
    wave_period_kind: str | None = None
    wave_direction_deg: float | None = None
    primary_swell_height_m: float | None
    primary_swell_period_s: float | None
    primary_swell_direction_deg: float | None
    secondary_swell_height_m: float | None
    secondary_swell_period_s: float | None
    secondary_swell_direction_deg: float | None
    tertiary_swell_height_m: float | None = None
    tertiary_swell_period_s: float | None = None
    tertiary_swell_direction_deg: float | None = None
    wind_wave_height_m: float | None
    wind_wave_period_s: float | None
    wind_wave_direction_deg: float | None = None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_gust_kmh: float | None
    pressure_msl_hpa: float | None
    sea_level_m: float | None
    wave_power_kw_m: float | None
    breaking_height_min_ft: float
    breaking_height_max_ft: float
    breaking_height_mid_ft: float | None = None
    breaking_p10_ft: float | None = None
    breaking_p90_ft: float | None = None
    uncertainty_kind: str | None = None
    breaking_height_method: str | None = None
    algorithm_version: str | None = None
    ml_model_version: str | None = None
    score: int
    label: str
    confidence: int
    confidence_label: str
    wind_relation: str
    explanation: str
    components: dict[str, Any]
    # Forecast confidence (the offshore forecast) and local confidence (the breaking
    # prediction at this break, which also depends on how well the spot is researched):
    # separate classes "high" | "moderate" | "low", plus short plain-language reasons.
    # These are documented classifications, not calibrated probabilities.
    forecast_confidence_class: str | None = None
    local_confidence_class: str | None = None
    confidence_reasons: list[str] = []
    lead_hours: float | None = None


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


class ModelPoint(BaseModel):
    valid_time: datetime
    hs_m: float | None
    tp_s: float | None
    direction_deg: float | None
    wind_kmh: float | None
    wind_direction_deg: float | None


class ModelSeries(BaseModel):
    """Raw output of one numerical model (or our blend) at this spot, 3-hourly."""

    source_code: str
    name: str
    family: str
    kind: str
    # "authoritative" (drives our prediction), "member" (blended in), "cross_check"
    # (same model family as a member, compared only) or "reference" (shown only).
    role: str
    is_demo: bool
    run_issued_at: datetime
    period_kind: str | None
    points: list[ModelPoint]


class EnsemblePoint(BaseModel):
    valid_time: datetime
    hs_mean_m: float | None
    hs_spread_m: float | None
    members: int | None


class BlendInfo(BaseModel):
    method: str
    members: list[dict[str, Any]]
    cross_checks: list[dict[str, Any]]
    flagged_hours: int
    flags: dict[str, int]
    skill: Any


class SpotForecastOut(BaseModel):
    spot_slug: str
    timezone: str
    available: bool
    message: str | None = None
    source: ForecastSourceOut | None = None
    run: ForecastRunOut | None = None
    stale: bool = False
    breaking_height_method: str | None = None
    algorithm_version: str | None = None
    calibrated: bool = False
    points: list[ForecastPoint] = []
    daily: list[DailySummary] = []
    models: list[ModelSeries] = []
    ensemble: list[EnsemblePoint] = []
    blend: BlendInfo | None = None


class RegionOut(BaseModel):
    region_group: str
    spot_count: int
    countries: list[dict[str, str]]
