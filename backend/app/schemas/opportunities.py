"""Opportunity (match), flight offer and notification schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.schemas.spots import DailySummary, EventOut, SpotAirportOut, SpotMini


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


class Page(BaseModel):
    total: int
    items: list[Any]
