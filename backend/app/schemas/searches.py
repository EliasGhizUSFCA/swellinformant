"""Saved search schemas. Wave heights are accepted in the search's display units."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    BreakType,
    CabinClass,
    DateMode,
    DestinationMode,
    NotificationChannelPref,
    QualityLabel,
    RankingPriority,
    SearchStatus,
    WindRequirement,
)
from app.schemas.common import CurrencyCode, IataCode, PlainText

# saved_searches.alert_lead_*_days CHECK bound (migration 0009).
MAX_ALERT_LEAD_DAYS = 16


class TravelIn(BaseModel):
    max_price: Decimal = Field(gt=0, le=100000, decimal_places=2)
    currency: CurrencyCode = "USD"
    direct_only: bool = False
    max_layovers: int = Field(default=2, ge=0, le=3)
    max_flight_hours: float = Field(default=30, ge=1, le=60)
    min_days_at_destination: int = Field(default=3, ge=1, le=60)
    max_trip_days: int = Field(default=14, ge=1, le=90)
    travelers: int = Field(default=1, ge=1, le=9)
    preferred_airlines: list[str] = Field(default_factory=list, max_length=10)
    cabin_class: CabinClass = CabinClass.ECONOMY
    arrival_buffer_days: int = Field(default=2, ge=0, le=14)
    departure_buffer_days: int = Field(default=1, ge=0, le=14)

    @model_validator(mode="after")
    def _check(self) -> TravelIn:
        if self.max_trip_days < self.min_days_at_destination:
            raise ValueError(
                "Maximum trip length must be at least the minimum days at destination."
            )
        cleaned = []
        for code in self.preferred_airlines:
            c = code.strip().upper()
            if len(c) != 2 or not c.isalnum():
                raise ValueError(f"Airline code {code!r} must be a 2-character IATA code.")
            cleaned.append(c)
        self.preferred_airlines = sorted(set(cleaned))
        return self


class TravelOut(TravelIn):
    pass


class DestinationsIn(BaseModel):
    spot_slugs: list[str] = Field(default_factory=list, max_length=60)
    countries: list[str] = Field(default_factory=list, max_length=60)
    region_groups: list[str] = Field(default_factory=list, max_length=20)


class SearchIn(BaseModel):
    name: PlainText = Field(min_length=1, max_length=120)
    date_mode: DateMode = DateMode.FLEXIBLE
    date_start: date | None = None
    date_end: date | None = None
    horizon_days: int = Field(default=30, ge=1, le=365)
    units: str = Field(default="ft", pattern="^(ft|m)$")
    wave_min: float = Field(ge=0, le=100)
    wave_max: float = Field(gt=0, le=100)
    wave_preferred: float | None = Field(default=None, ge=0, le=100)
    min_quality: QualityLabel = QualityLabel.GOOD
    min_period_s: float | None = Field(default=None, ge=4, le=25)
    max_wind_kmh: float | None = Field(default=None, ge=0, le=100)
    wind_requirement: WindRequirement = WindRequirement.ANY
    swell_direction_min: float | None = Field(default=None, ge=0, lt=360)
    swell_direction_max: float | None = Field(default=None, ge=0, lt=360)
    min_consistency: int | None = Field(default=None, ge=0, le=100)
    break_types: list[BreakType] = Field(default_factory=list)
    min_window_hours: int = Field(default=4, ge=1, le=240)
    destination_mode: DestinationMode = DestinationMode.ALL
    destinations: DestinationsIn = Field(default_factory=DestinationsIn)
    max_transfer_minutes: int | None = Field(default=None, ge=0, le=2880)
    origins: list[IataCode] = Field(min_length=1, max_length=5)
    max_origin_ground_km: int = Field(default=0, ge=0, le=500)
    notification_channel: NotificationChannelPref = NotificationChannelPref.EMAIL
    notify_surf_only: bool = False
    notify_on_updates: bool = True
    # How far ahead a swell may start to be alerted (whole days, spec 28). None: the
    # deployment's ALERT_LEAD_DEFAULT_MIN_DAYS / _MAX_DAYS for a new search, the current
    # value when an existing search is saved without it.
    alert_lead_min_days: int | None = Field(default=None, ge=0, le=MAX_ALERT_LEAD_DAYS)
    alert_lead_max_days: int | None = Field(default=None, ge=0, le=MAX_ALERT_LEAD_DAYS)
    priority: RankingPriority = RankingPriority.BALANCED
    travel: TravelIn

    @model_validator(mode="after")
    def _check(self) -> SearchIn:
        if self.wave_min >= self.wave_max:
            raise ValueError("Minimum wave height must be lower than the maximum.")
        if self.wave_preferred is not None and not (
            self.wave_min <= self.wave_preferred <= self.wave_max
        ):
            raise ValueError("Preferred wave height must lie between the minimum and maximum.")
        if self.min_quality == QualityLabel.POOR:
            raise ValueError("Minimum quality must be Fair or better.")
        if self.date_mode == DateMode.FIXED:
            if not self.date_start or not self.date_end:
                raise ValueError("Fixed-date searches need a start and end date.")
            if self.date_end < self.date_start:
                raise ValueError("End date must be after the start date.")
            if self.date_start < date.today() - timedelta(days=1):
                raise ValueError("Start date must not be in the past.")
            if (self.date_end - self.date_start).days > 365:
                raise ValueError("Travel window must be at most one year.")
        if (self.swell_direction_min is None) != (self.swell_direction_max is None):
            raise ValueError("Provide both swell direction bounds or neither.")
        if (
            self.alert_lead_min_days is not None
            and self.alert_lead_max_days is not None
            and self.alert_lead_min_days > self.alert_lead_max_days
        ):
            raise ValueError("The earliest alert lead time must not be later than the latest one.")
        d = self.destinations
        if self.destination_mode == DestinationMode.SPOTS and not d.spot_slugs:
            raise ValueError("Select at least one surf spot.")
        if self.destination_mode == DestinationMode.REGIONS and not (
            d.countries or d.region_groups
        ):
            raise ValueError("Select at least one country or region.")
        self.origins = list(dict.fromkeys(self.origins))
        return self


class MatchCounts(BaseModel):
    total: int = 0
    flight_found: int = 0
    surf_only: int = 0
    pending_flights: int = 0
    expired: int = 0


class SearchOut(BaseModel):
    id: uuid.UUID
    name: str
    status: SearchStatus
    date_mode: DateMode
    date_start: date | None
    date_end: date | None
    horizon_days: int
    units: str
    wave_min: float
    wave_max: float
    wave_preferred: float | None
    wave_min_ft: float
    wave_max_ft: float
    min_quality: QualityLabel
    min_period_s: float | None
    max_wind_kmh: float | None
    wind_requirement: WindRequirement
    swell_direction_min: float | None
    swell_direction_max: float | None
    min_consistency: int | None
    break_types: list[str]
    min_window_hours: int
    destination_mode: DestinationMode
    destinations: DestinationsIn
    max_transfer_minutes: int | None
    origins: list[str]
    max_origin_ground_km: int
    notification_channel: NotificationChannelPref
    notify_surf_only: bool
    notify_on_updates: bool
    alert_lead_min_days: int
    alert_lead_max_days: int
    priority: RankingPriority
    travel: TravelOut
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None
    last_evaluated_at: datetime | None
    match_counts: MatchCounts


class SearchDefaultsOut(BaseModel):
    """What the search wizard needs to know about this deployment's limits."""

    alert_lead_min_days: int
    alert_lead_max_days: int
    # The forecast horizon and the global detection window that bound every search.
    forecast_days: int
    detection_lead_min_days: float
    detection_lead_max_days: float
    # Swells starting this many days ahead rank first (short notice, spec 28).
    short_notice_min_days: float
    short_notice_max_days: float
    max_origins: int
    max_layovers: int
    max_alert_lead_days: int
    flight_searches_per_day: int
