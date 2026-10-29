"""Opportunity (match), flight offer and notification schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.forecasts import SpotFreshnessOut
from app.schemas.spots import DailySummary, EventOut, SpotAirportOut, SpotMini

# Forecast and local confidence classes (documented heuristic classes, not probabilities).
ConfidenceClass = Literal["high", "moderate", "low"]


class SegmentOut(BaseModel):
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    departure_local: str
    arrival_local: str
    marketing_carrier: str
    carrier_name: str
    flight_number: str
    duration_minutes: int
    # The airline that flies the segment (its full name: Duffel's display rule). None for
    # offers stored before it was recorded, or when the marketing carrier flies it.
    operating_carrier: str | None = None
    operating_carrier_name: str | None = None


class SliceOut(BaseModel):
    origin: str
    destination: str
    departure_at: datetime
    arrival_at: datetime
    departure_local: str
    arrival_local: str
    duration_minutes: int
    stops: int
    segments: list[SegmentOut]


class LinkOut(BaseModel):
    label: str
    url: str
    # "booking": the provider's own booking flow for this offer; "route_search": a new
    # search for the same route and dates on a third-party site (fares may differ).
    kind: Literal["booking", "route_search"] = "route_search"
    note: str | None = None


class OfferOut(BaseModel):
    id: uuid.UUID
    rank: int | None = None
    provider: str
    provider_label: str
    is_mock: bool
    total_price: Decimal
    price_per_traveler: Decimal | None
    currency: str
    price_converted: Decimal | None = None
    airlines: list[str]
    airline_names: list[str]
    origin: str
    destination: str
    origin_timezone: str
    destination_timezone: str
    outbound: SliceOut
    inbound: SliceOut
    total_duration_minutes: int
    baggage: str | None
    quoted_at: datetime
    last_validated_at: datetime | None
    validation_status: str
    offer_expires_at: datetime | None
    links: list[LinkOut]
    # Full names of the airlines operating the segments, in travel order.
    operating_carriers: list[str] = []
    # The quote's own expiry has passed: shown as a past quote, never as bookable.
    expired: bool = False
    # The currency of ``price_converted`` when it differs from the offer's (converted
    # with the configured FX_RATES_USD_JSON rates); None when no conversion was needed.
    converted_currency: str | None = None


class MatchSummary(BaseModel):
    id: uuid.UUID
    search_id: uuid.UUID
    search_name: str
    search_status: str
    status: str
    status_reason: str | None
    spot: SpotMini
    event_id: uuid.UUID
    event_status: str
    window_start: datetime
    window_end: datetime
    qualifying_hours: int
    peak_score: int
    avg_score: float
    peak_label: str
    breaking_height_min_ft: float
    breaking_height_max_ft: float
    confidence: int
    confidence_label: str
    destination_iata: str | None
    transfer_minutes: int | None
    origin_iata: str | None
    recommended_arrival_date: date | None
    recommended_departure_date: date | None
    best_price: Decimal | None
    currency: str | None
    overall_score: int
    scores: dict[str, Any]
    is_demo: bool
    has_mock_flights: bool
    flight_checked_at: datetime | None
    notification_count: int
    lead_days: float
    created_at: datetime
    updated_at: datetime
    # Forecast confidence at the event's peak hour (the offshore forecast; heuristic
    # classes). None = not assessed. Research confidence is the spot's (spec 20: never
    # combined with it).
    forecast_confidence_class: ConfidenceClass | None = None
    # The forecast run behind the match is older than its source's stale limit.
    stale: bool = False
    # How fresh the spot's served forecast is (the published version's spot status: a spot
    # whose refresh failed is stale although its run is not; spec 14).
    freshness: SpotFreshnessOut | None = None
    # The swell starts SHORT_NOTICE_MIN_DAYS to _MAX_DAYS ahead (ranked first, spec 28).
    short_notice: bool = False
    # The best fare's quote has expired: a past price, re-check before booking.
    best_offer_expired: bool = False
    # The provider's currency when ``best_price`` was converted into the search currency.
    quoted_currency: str | None = None


class TravelWindowOut(BaseModel):
    arrive_after: datetime
    arrive_by: datetime
    depart_after: datetime
    depart_before: datetime
    destination_timezone: str
    nights: int


class MatchDetail(MatchSummary):
    event: EventOut
    spot_airports: list[SpotAirportOut]
    accessibility_notes: str
    daily: list[DailySummary]
    offers: list[OfferOut]
    travel_window: TravelWindowOut | None
    inspection_links: list[LinkOut]
    limitations: list[str]
    flight_search_error: str | None


class NotificationOut(BaseModel):
    id: uuid.UUID
    channel: str
    kind: str
    status: str
    subject: str
    recipient_masked: str
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None = None
    provider: str | None = None
    attempts: int
    last_error: str | None
    match_id: uuid.UUID | None
    match_status: str | None
    event_status: str | None
    spot_name: str | None
    spot_slug: str | None
    price: str | None
    currency: str | None
    peak_label: str | None
    window_label: str | None
    is_demo: bool
    # Confidence at the alerted event's peak hour (see MatchSummary).
    forecast_confidence_class: ConfidenceClass | None = None
