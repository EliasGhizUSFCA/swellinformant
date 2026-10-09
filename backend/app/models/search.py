"""Saved surf travel searches and their travel preferences."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    CabinClass,
    DateMode,
    DestinationMode,
    NotificationChannelPref,
    QualityLabel,
    RankingPriority,
    SearchStatus,
    WindRequirement,
)
from app.models.types import TimestampMixin, str_enum

if TYPE_CHECKING:
    from app.models.user import User


class SavedSearch(TimestampMixin, Base):
    """A user's standing request: "tell me when these waves line up with these flights".

    Wave heights are stored canonically in feet of estimated *breaking* face height;
    ``units`` only controls display.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        CheckConstraint("wave_min_ft >= 0 AND wave_min_ft < wave_max_ft", name="wave_range"),
        CheckConstraint(
            "wave_preferred_ft IS NULL OR (wave_preferred_ft >= wave_min_ft "
            "AND wave_preferred_ft <= wave_max_ft)",
            name="wave_preferred",
        ),
        CheckConstraint(
            "date_mode <> 'fixed' OR (date_start IS NOT NULL AND date_end IS NOT NULL "
            "AND date_end >= date_start)",
            name="fixed_dates",
        ),
        CheckConstraint("horizon_days BETWEEN 1 AND 365", name="horizon_range"),
        CheckConstraint("min_window_hours BETWEEN 1 AND 240", name="window_hours"),
        CheckConstraint("units IN ('ft', 'm')", name="units"),
        Index("ix_saved_searches_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[SearchStatus] = mapped_column(
        str_enum(SearchStatus, "search_status"), nullable=False, default=SearchStatus.ACTIVE
    )
    # Travel dates
    date_mode: Mapped[DateMode] = mapped_column(
        str_enum(DateMode, "search_date_mode"), nullable=False, default=DateMode.FLEXIBLE
    )
    date_start: Mapped[date | None] = mapped_column(Date)
    date_end: Mapped[date | None] = mapped_column(Date)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # Surf preferences (feet, breaking face height)
    wave_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    wave_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    wave_preferred_ft: Mapped[float | None] = mapped_column(Float)
    units: Mapped[str] = mapped_column(String(2), nullable=False, default="ft")
    min_quality: Mapped[QualityLabel] = mapped_column(
        str_enum(QualityLabel, "search_min_quality"), nullable=False, default=QualityLabel.GOOD
    )
    min_period_s: Mapped[float | None] = mapped_column(Float)
    max_wind_kmh: Mapped[float | None] = mapped_column(Float)
    wind_requirement: Mapped[WindRequirement] = mapped_column(
        str_enum(WindRequirement, "search_wind_requirement"),
        nullable=False,
        default=WindRequirement.ANY,
    )
    swell_direction_min: Mapped[float | None] = mapped_column(Float)
    swell_direction_max: Mapped[float | None] = mapped_column(Float)
    min_consistency: Mapped[int | None] = mapped_column(Integer)
    break_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    min_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    # Destinations
    destination_mode: Mapped[DestinationMode] = mapped_column(
        str_enum(DestinationMode, "search_destination_mode"),
        nullable=False,
        default=DestinationMode.ALL,
    )
    max_transfer_minutes: Mapped[int | None] = mapped_column(Integer)
    # Departure
    max_origin_ground_km: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Notifications & ranking
    notification_channel: Mapped[NotificationChannelPref] = mapped_column(
        str_enum(NotificationChannelPref, "search_notification_channel"),
        nullable=False,
        default=NotificationChannelPref.EMAIL,
    )
    notify_surf_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_on_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[RankingPriority] = mapped_column(
        str_enum(RankingPriority, "search_priority"),
        nullable=False,
        default=RankingPriority.BALANCED,
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="searches")
    origins: Mapped[list[SearchOrigin]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="SearchOrigin.id",
    )
    destinations: Mapped[list[SearchDestination]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="SearchDestination.id",
    )
    travel: Mapped[TravelPreference] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
        lazy="selectin",
    )


class SearchOrigin(Base):
    __tablename__ = "search_origins"
    __table_args__ = (UniqueConstraint("search_id", "airport_iata"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False
    )
    airport_iata: Mapped[str] = mapped_column(
        String(3), ForeignKey("airports.iata", ondelete="RESTRICT"), nullable=False
    )

    search: Mapped[SavedSearch] = relationship(back_populates="origins")


class SearchDestination(Base):
    """One destination filter row: a spot, a country, or a region group."""

    __tablename__ = "search_destinations"
    __table_args__ = (
        CheckConstraint(
            "(spot_id IS NOT NULL)::int + (country_code IS NOT NULL)::int "
            "+ (region_group IS NOT NULL)::int = 1",
            name="exactly_one_target",
        ),
        Index("ix_search_destinations_search", "search_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE")
    )
    country_code: Mapped[str | None] = mapped_column(String(2))
    region_group: Mapped[str | None] = mapped_column(String(60))

    search: Mapped[SavedSearch] = relationship(back_populates="destinations")


class TravelPreference(TimestampMixin, Base):
    __tablename__ = "travel_preferences"
    __table_args__ = (
        CheckConstraint("max_price > 0", name="price_positive"),
        CheckConstraint("travelers BETWEEN 1 AND 9", name="travelers_range"),
        CheckConstraint("max_layovers BETWEEN 0 AND 3", name="layovers_range"),
        CheckConstraint("arrival_buffer_days BETWEEN 0 AND 14", name="arrival_buffer"),
        CheckConstraint("departure_buffer_days BETWEEN 0 AND 14", name="departure_buffer"),
        CheckConstraint("min_days_at_destination >= 1", name="min_days"),
        CheckConstraint("max_trip_days >= min_days_at_destination", name="trip_days"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("saved_searches.id", ondelete="CASCADE"), primary_key=True
    )
    max_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    direct_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_layovers: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_flight_hours: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    min_days_at_destination: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_trip_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    travelers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    preferred_airlines: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), nullable=False, server_default=text("'{}'::varchar[]")
    )
    cabin_class: Mapped[CabinClass] = mapped_column(
        str_enum(CabinClass, "travel_cabin_class"), nullable=False, default=CabinClass.ECONOMY
    )
    arrival_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    departure_buffer_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    search: Mapped[SavedSearch] = relationship(back_populates="travel")
