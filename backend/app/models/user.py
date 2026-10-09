"""User accounts, profiles, sessions, single-use tokens and notification preferences."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import BreakType, SkillLevel, TokenPurpose
from app.models.types import TimestampMixin, str_enum

if TYPE_CHECKING:
    from app.models.search import SavedSearch


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[UserProfile] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    notification_preferences: Mapped[NotificationPreference] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    airports: Mapped[list[UserAirport]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="UserAirport.created_at",
    )
    searches: Mapped[list[SavedSearch]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None


class UserProfile(TimestampMixin, Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        CheckConstraint(
            "preferred_wave_min_ft IS NULL OR preferred_wave_max_ft IS NULL "
            "OR preferred_wave_min_ft <= preferred_wave_max_ft",
            name="wave_range",
        ),
        CheckConstraint("units IN ('ft', 'm')", name="units"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    home_airport: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("airports.iata", ondelete="SET NULL")
    )
    home_city: Mapped[str | None] = mapped_column(String(120))
    preferred_wave_min_ft: Mapped[float | None] = mapped_column(Float)
    preferred_wave_max_ft: Mapped[float | None] = mapped_column(Float)
    preferred_break_type: Mapped[BreakType | None] = mapped_column(
        str_enum(BreakType, "profile_break_type")
    )
    experience_level: Mapped[SkillLevel | None] = mapped_column(
        str_enum(SkillLevel, "profile_skill_level")
    )
    units: Mapped[str] = mapped_column(String(2), default="ft", nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)

    user: Mapped[User] = relationship(back_populates="profile")


class UserAirport(TimestampMixin, Base):
    """A departure airport saved by the user for reuse in searches."""

    __tablename__ = "user_airports"
    __table_args__ = (UniqueConstraint("user_id", "airport_iata"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    airport_iata: Mapped[str] = mapped_column(
        String(3), ForeignKey("airports.iata", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(60))
    is_home: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    user: Mapped[User] = relationship(back_populates="airports")


class UserSession(Base):
    """Server-side session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")


class UserToken(Base):
    """Single-use, expiring token for email verification and password reset."""

    __tablename__ = "user_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        Index("ix_user_tokens_user_purpose", "user_id", "purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[TokenPurpose] = mapped_column(
        str_enum(TokenPurpose, "token_purpose"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationPreference(TimestampMixin, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("unsubscribe_token"),
        CheckConstraint("max_alerts_per_day BETWEEN 1 AND 100", name="max_alerts_range"),
        CheckConstraint(
            "phone_number IS NULL OR phone_number ~ '^\\+[1-9][0-9]{6,14}$'", name="phone_e164"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Global kill switch ("disable notifications"): nothing is sent while true.
    all_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(16))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sms_opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verification_code_hash: Mapped[str | None] = mapped_column(String(64))
    phone_verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verification_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), nullable=False)
    max_alerts_per_day: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    user: Mapped[User] = relationship(back_populates="notification_preferences")

    @property
    def sms_ready(self) -> bool:
        """SMS may be sent only to a verified, opted-in, not opted-out number."""
        return bool(
            self.phone_number
            and self.phone_verified_at
            and self.sms_opt_in_at
            and (self.sms_opted_out_at is None or self.sms_opted_out_at < self.sms_opt_in_at)
        )
