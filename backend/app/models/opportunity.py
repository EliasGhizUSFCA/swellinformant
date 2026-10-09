"""Opportunity matches (search × swell event), their ranked offers, notifications, job logs."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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
from app.models.enums import (
    ConfidenceLabel,
    JobStatus,
    MatchStatus,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
    QualityLabel,
)
from app.models.flight import FlightOffer
from app.models.forecast import SwellEvent
from app.models.search import SavedSearch
from app.models.spot import SurfSpot
from app.models.types import TimestampMixin, str_enum


class OpportunityMatch(TimestampMixin, Base):
    """A saved search matched against a swell event, plus the best travel option found."""

    __tablename__ = "opportunity_matches"
    __table_args__ = (
        UniqueConstraint("search_id", "swell_event_id"),
        Index("ix_opportunity_matches_user_status", "user_id", "status"),
        Index("ix_opportunity_matches_status_checked", "status", "flight_checked_at"),
        CheckConstraint("window_end >= window_start", name="window_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("saved_searches.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    swell_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("swell_events.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[MatchStatus] = mapped_column(
        str_enum(MatchStatus, "match_status"), nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    # The user-specific surf window (sub-window of the event meeting this search's criteria)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qualifying_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    peak_score: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_score: Mapped[float] = mapped_column(Float, nullable=False)
    peak_label: Mapped[QualityLabel] = mapped_column(
        str_enum(QualityLabel, "match_quality_label"), nullable=False
    )
    breaking_height_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    breaking_height_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_label: Mapped[ConfidenceLabel] = mapped_column(
        str_enum(ConfidenceLabel, "match_confidence_label"), nullable=False
    )
    # Travel recommendation (destination-local dates)
    destination_iata: Mapped[str | None] = mapped_column(String(3))
    transfer_minutes: Mapped[int | None] = mapped_column(Integer)
    recommended_arrival_date: Mapped[date | None] = mapped_column(Date)
    recommended_departure_date: Mapped[date | None] = mapped_column(Date)
    best_offer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("flight_offers.id", ondelete="SET NULL")
    )
    best_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    origin_iata: Mapped[str | None] = mapped_column(String(3))
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flight_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flight_search_error: Mapped[str | None] = mapped_column(Text)
    flights_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_notified_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notification_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_demo: Mapped[bool] = mapped_column(nullable=False, default=False)

    search: Mapped[SavedSearch] = relationship(lazy="joined")
    swell_event: Mapped[SwellEvent] = relationship(lazy="joined")
    spot: Mapped[SurfSpot] = relationship(lazy="joined")
    best_offer: Mapped[FlightOffer | None] = relationship(lazy="joined")
    offers: Mapped[list[OpportunityOffer]] = relationship(
        back_populates="match",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OpportunityOffer.rank",
    )


class OpportunityOffer(Base):
    """Ranked eligible offers attached to a match (offers are shared across matches)."""

    __tablename__ = "opportunity_offers"
    __table_args__ = (UniqueConstraint("match_id", "offer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunity_matches.id", ondelete="CASCADE"), nullable=False
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("flight_offers.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    price_converted: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    match: Mapped[OpportunityMatch] = relationship(back_populates="offers")
    offer: Mapped[FlightOffer] = relationship(lazy="joined")


class Notification(TimestampMixin, Base):
    """Every alert ever generated, with delivery state. ``dedup_key`` is the idempotency key."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("dedup_key"),
        Index("ix_notifications_status_next", "status", "next_attempt_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("opportunity_matches.id", ondelete="SET NULL")
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        str_enum(NotificationChannel, "notification_channel"), nullable=False
    )
    kind: Mapped[NotificationKind] = mapped_column(
        str_enum(NotificationKind, "notification_kind"), nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, "notification_status"), nullable=False
    )
    dedup_key: Mapped[str] = mapped_column(String(200), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    match: Mapped[OpportunityMatch | None] = relationship(lazy="select")


class BackgroundJobLog(Base):
    __tablename__ = "background_job_logs"
    __table_args__ = (Index("ix_background_job_logs_name_started", "job_name", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(60), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus, "job_status"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
