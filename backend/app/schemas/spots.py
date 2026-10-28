"""Surf spot, forecast and swell event schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.common import ORMModel
from app.schemas.forecasts import SpotFreshnessOut

# Wind regime (Beaufort-based km/h thresholds) and favourability at the spot, from the
# stored wind_detail of a prediction (docs/SURF_SCORING.md, "Stored components contract").
WindStrength = Literal["glassy", "light", "moderate", "strong"]
WindFavourability = Literal["favourable", "workable", "unfavourable"]
# Gust definitions: GFS instantaneous gust, ECMWF maximum over the last 3 h or since the
# previous post-processing step, or not documented by the source.
GustKind = Literal["gfs_instantaneous", "ecmwf_max_3h", "ecmwf_max_since_pp", "unknown"]
ConfidenceClass = Literal["high", "moderate", "low"]


class ForecastConfidenceOut(BaseModel):
    """Forecast confidence of one hour: how dependable the offshore forecast is (lead time,
    freshness, ensemble spread and model agreement where available; methodology
    docs/forecast-confidence.md). A heuristic class from documented rules, not a calibrated
    probability: ``kind`` says so. Its reasons are ``forecast_confidence_reasons``. Never
    combined with research confidence."""

    level: ConfidenceClass
    kind: Literal["heuristic"] = "heuristic"
    method: str | None = None  # e.g. "forecast-v1.2"


class HourOutputs(BaseModel):
    """Per-hour outputs of the quality model (algorithm 2.2.0; docs/SURF_SCORING.md 3a)."""

    # Main favourable and unfavourable conditions, short plain phrases from the score's
    # component fits ("well-aligned swell", "light offshore wind", "onshore wind 25 km/h"):
    # at most three each, [] when none; None for hours scored before 2.2.0.
    favourable: list[str] | None = None
    unfavourable: list[str] | None = None
    # Offshore-wind classification: strength + relation ("light offshore", "strong
    # onshore", "glassy"); offshore = wind from the land to the sea at this break.
    offshore_class: str | None = None
    # The scoring model version of this hour (algorithm_version; "+w..." when
    # SCORING_WEIGHTS_JSON changed the weights).
    model_version: str | None = None


class SeparateConfidences(BaseModel):
    """Forecast confidence and research confidence of one hour, kept apart (spec 20)."""

    # The forecast class at read time (capped at Moderate once the run is stale).
    forecast_confidence: ForecastConfidenceOut | None = None
    # The spot's overall research class the hour was scored with ("high" | "moderate" |
    # "low"; None = not assessed): how well its ideal conditions are documented.
    research_confidence: ConfidenceClass | None = None


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


class Conditions(SeparateConfidences):
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
    # Forecast confidence class (the offshore forecast: "high" | "moderate" | "low"), a
    # documented heuristic classification, not a calibrated probability; also in
    # forecast_confidence with its label.
    forecast_confidence_class: str | None = None
    # Deprecated: forecast, research and wind-dispute reasons mixed in one list. Use
    # forecast_confidence_reasons and wind_dispute_note.
    confidence_reasons: list[str] = []
    lead_hours: float | None = None
    # Forecast-confidence reasons only (lead time, model agreement, freshness ...), with the
    # stale reason first when staleness capped the class.
    forecast_confidence_reasons: list[str] = []
    # Set when researched sources disagree about this spot's offshore wind direction.
    wind_dispute_note: str | None = None
    # Wind regime from our wind model (km/h thresholds in docs/SURF_SCORING.md) and how
    # favourable the wind is at this break; None for predictions made before 2.1.0.
    wind_strength: WindStrength | None = None
    wind_favourability: WindFavourability | None = None
    # How the source defines the gust value (instantaneous, maximum over 3 h, ...).
    gust_kind: GustKind | None = None


class MapSpotOut(BaseModel):
    slug: str
    conditions: Conditions | None
    event_at_time: bool  # an active swell event covers the selected time
    upcoming_event_count: int
    # How fresh the spot's served forecast is (published version, last update, stale).
    freshness: SpotFreshnessOut | None = None


class AttributionLinkOut(BaseModel):
    """A credit or licence link the data source asks for (shown next to the data)."""

    label: str
    href: str


class MapSnapshotOut(BaseModel):
    """Every monitored spot at one forecast time, from cached predictions only."""

    # The time asked for (?at=) as answered: its UTC hour, within 2 days before and 17 days
    # after the current hour (app.services.public_cache.map_time). None: "now".
    requested_at: datetime | None
    valid_time: datetime | None
    range_start: datetime | None
    range_end: datetime | None
    step_hours: int
    spots: list[MapSpotOut]
    # Freshness of the predictions behind the snapshot: the newest authoritative run's issue
    # time, when it was last updated, and whether it is older than its stale limit.
    stale: bool = False
    newest_run_issued_at: datetime | None = None
    last_updated_at: datetime | None = None
    # Credit and licence links of the sources behind the snapshot's runs (e.g. Open-Meteo's
    # CC BY 4.0 link, ECMWF's notice): shown next to the model data in the spot panel.
    attribution_links: list[AttributionLinkOut] = []
    # The published forecast version the snapshot serves (None before the first one).
    version_id: int | None = None


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
    # Forecast confidence at the event's peak hour (forecast-v1; see Conditions). None for
    # events last updated before these classes existed.
    forecast_confidence_class: str | None = None
    # Deprecated: mixed reasons (see Conditions.confidence_reasons).
    confidence_reasons: list[str] = []
    forecast_confidence_reasons: list[str] = []
    wind_dispute_note: str | None = None
    wind_strength: WindStrength | None = None
    wind_favourability: WindFavourability | None = None
    gust_kind: GustKind | None = None
    # The forecast run behind the event is older than its source's stale limit.
    stale: bool = False
    # Credit and licence links of the sources behind the event's forecast run (every
    # member's for a blend), shown next to the event's model values.
    attribution_links: list[AttributionLinkOut] = []


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
    # How fresh the spot's served forecast is (published version, last update, stale).
    freshness: SpotFreshnessOut | None = None


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
    # How the source was read: "fetched" (the page itself) or "web_search" (only a search
    # tool's summary of the page). None for assessments made before this was recorded.
    retrieval: str | None = None


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


class ForecastPoint(HourOutputs, SeparateConfidences):
    """One stored forecast hour of the spot forecast: raw model values, our breaking-height
    estimate (``breaking_height_*``, the nearshore model's typical-to-sets range and
    central value, an estimate that is not calibrated against observed surf), the score
    and its per-hour outputs (``HourOutputs``) and the two separate confidences."""

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
    # Forecast confidence class (see Conditions.forecast_confidence_class).
    forecast_confidence_class: str | None = None
    # Deprecated: forecast, research and wind-dispute reasons mixed in one list. Use
    # forecast_confidence_reasons and wind_dispute_note.
    confidence_reasons: list[str] = []
    lead_hours: float | None = None
    # Forecast-confidence reasons only (lead time, model agreement, freshness ...), with the
    # stale reason first when staleness capped the class.
    forecast_confidence_reasons: list[str] = []
    # Set when researched sources disagree about this spot's offshore wind direction.
    wind_dispute_note: str | None = None
    # Wind regime from our wind model (km/h thresholds in docs/SURF_SCORING.md) and how
    # favourable the wind is at this break; None for predictions made before 2.1.0.
    wind_strength: WindStrength | None = None
    wind_favourability: WindFavourability | None = None
    # How the source defines the gust value (instantaneous, maximum over 3 h, ...).
    gust_kind: GustKind | None = None


class DailySummary(BaseModel):
    date: date
    best_score: int
    best_label: str
    best_time: datetime | None
    height_min_ft: float
    height_max_ft: float
    avg_wind_kmh: float | None
    # Hours of Good or better daylight surf, weighted by each forecast step's duration.
    good_hours: float
    confidence: int
    # Forecast confidence at the day's best hour (``best_time``).
    forecast_confidence_class: str | None = None


class ForecastSourceOut(BaseModel):
    code: str
    name: str
    is_demo: bool
    attribution: str
    wave_model: str | None
    atmosphere_model: str | None
    attribution_links: list[AttributionLinkOut] = []


class RunMemberOut(BaseModel):
    """One model run behind a forecast (a blend member, or the single source itself)."""

    code: str
    name: str
    family: str
    run_key: str
    # Initialisation (cycle) time of the model run; None when the source does not say.
    init_time: datetime | None
    wave_model: str | None
    atmosphere_model: str | None


class ForecastRunOut(BaseModel):
    run_key: str
    issued_at: datetime
    completed_at: datetime | None
    wave_model_run_at: datetime | None
    atmosphere_model_run_at: datetime | None
    status: str
    members: list[RunMemberOut] = []


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
    # Alerts for this spot are held back because the forecast is too old to act on.
    alerts_paused: bool = False
    # Server time when this response was built (UTC).
    generated_at: datetime | None = None
    # How fresh the spot's served forecast is (published version, last update, stale).
    freshness: SpotFreshnessOut | None = None


class RegionOut(BaseModel):
    region_group: str
    spot_count: int
    countries: list[dict[str, str]]
