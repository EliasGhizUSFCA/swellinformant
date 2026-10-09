"""Flight searches (shared cache across users) and the offers they returned."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, utcnow
from app.models.enums import CabinClass, FlightSearchStatus, OfferValidation
from app.models.types import str_enum


class FlightSearch(Base):
    """One provider query. ``cache_key`` lets every user with the same route/dates reuse it."""

    __tablename__ = "flight_searches"
    __table_args__ = (
        Index("ix_flight_searches_cache_key", "cache_key", "searched_at"),
        Index("ix_flight_searches_searched_at", "searched_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(160), nullable=False)
    origin_iata: Mapped[str] = mapped_column(String(3), nullable=False)
    destination_iata: Mapped[str] = mapped_column(String(3), nullable=False)
    departure_date: Mapped[date] = mapped_column(Date, nullable=False)
    return_date: Mapped[date] = mapped_column(Date, nullable=False)
    adults: Mapped[int] = mapped_column(Integer, nullable=False)
    cabin_class: Mapped[CabinClass] = mapped_column(
        str_enum(CabinClass, "flight_search_cabin"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    max_connections: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[FlightSearchStatus] = mapped_column(
        str_enum(FlightSearchStatus, "flight_search_status"), nullable=False
    )
    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    offers: Mapped[list[FlightOffer]] = relationship(
        back_populates="flight_search", cascade="all, delete-orphan", passive_deletes=True
    )


class FlightOffer(Base):
    """A priced round-trip itinerary exactly as quoted by the provider.

    All ``*_at`` instants are timezone-aware UTC; the airports' IANA zones are stored so
    local wall-clock times (which is what providers return) can be reconstructed.
    """

    __tablename__ = "flight_offers"
    __table_args__ = (
        UniqueConstraint("flight_search_id", "provider_offer_id"),
        CheckConstraint("total_price >= 0", name="price_positive"),
        Index("ix_flight_offers_search_price", "flight_search_id", "total_price"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flight_search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("flight_searches.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_offer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_per_traveler: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    airlines: Mapped[list[str]] = mapped_column(ARRAY(String(3)), nullable=False)
    airline_names: Mapped[list[str]] = mapped_column(ARRAY(String(120)), nullable=False)
    origin_iata: Mapped[str] = mapped_column(String(3), nullable=False)
    destination_iata: Mapped[str] = mapped_column(String(3), nullable=False)
    outbound_departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outbound_arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outbound_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    outbound_stops: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound_departure_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inbound_arrival_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    inbound_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    inbound_stops: Mapped[int] = mapped_column(Integer, nullable=False)
    origin_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    baggage: Mapped[str | None] = mapped_column(String(255))
    booking_url: Mapped[str | None] = mapped_column(Text)
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_status: Mapped[OfferValidation] = mapped_column(
        str_enum(OfferValidation, "offer_validation"),
        nullable=False,
        default=OfferValidation.UNVALIDATED,
    )
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Provider's original offer object, needed by providers whose re-pricing endpoint
    # takes the full offer back (Amadeus Flight Offers Price).
    provider_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    flight_search: Mapped[FlightSearch] = relationship(back_populates="offers")

    @property
    def total_duration_minutes(self) -> int:
        return self.outbound_duration_minutes + self.inbound_duration_minutes
