"""Airports and surf spots."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import BreakType, SkillLevel, TidePreference
from app.models.types import TimestampMixin, str_enum


class Airport(Base):
    __tablename__ = "airports"
    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="lat_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="lon_range"),
        CheckConstraint("iata ~ '^[A-Z]{3}$'", name="iata_format"),
    )

    iata: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    country: Mapped[str] = mapped_column(String(80), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)


class SurfSpot(TimestampMixin, Base):
    """A monitored surf break and the physical parameters the scoring engine needs.

    Directions are compass degrees the swell/wind comes FROM. Windows run clockwise from
    ``*_min`` to ``*_max`` and may wrap through north (e.g. 300 → 20).
    """

    __tablename__ = "surf_spots"
    __table_args__ = (
        UniqueConstraint("slug"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="lat_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="lon_range"),
        CheckConstraint("forecast_latitude BETWEEN -90 AND 90", name="flat_range"),
        CheckConstraint("forecast_longitude BETWEEN -180 AND 180", name="flon_range"),
        CheckConstraint("wave_height_min_ft < wave_height_max_ft", name="height_range"),
        CheckConstraint("min_swell_period_s <= ideal_swell_period_s", name="period_order"),
        CheckConstraint("height_factor > 0 AND height_factor <= 3", name="height_factor"),
        CheckConstraint(
            "swell_window_min >= 0 AND swell_window_min < 360 AND swell_window_max >= 0 "
            "AND swell_window_max < 360 AND optimal_swell_direction_min >= 0 "
            "AND optimal_swell_direction_min < 360 AND optimal_swell_direction_max >= 0 "
            "AND optimal_swell_direction_max < 360 AND offshore_wind_direction >= 0 "
            "AND offshore_wind_direction < 360",
            name="direction_ranges",
        ),
        Index("ix_surf_spots_region_group", "region_group"),
        Index("ix_surf_spots_country_code", "country_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(80), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str] = mapped_column(String(120), nullable=False)
    region_group: Mapped[str] = mapped_column(String(60), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    forecast_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    forecast_longitude: Mapped[float] = mapped_column(Float, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    break_type: Mapped[BreakType] = mapped_column(
        str_enum(BreakType, "spot_break_type"), nullable=False
    )
    wave_direction: Mapped[str] = mapped_column(String(8), nullable=False)
    is_big_wave: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    swell_window_min: Mapped[float] = mapped_column(Float, nullable=False)
    swell_window_max: Mapped[float] = mapped_column(Float, nullable=False)
    optimal_swell_direction_min: Mapped[float] = mapped_column(Float, nullable=False)
    optimal_swell_direction_max: Mapped[float] = mapped_column(Float, nullable=False)
    offshore_wind_direction: Mapped[float] = mapped_column(Float, nullable=False)
    min_swell_period_s: Mapped[float] = mapped_column(Float, nullable=False)
    ideal_swell_period_s: Mapped[float] = mapped_column(Float, nullable=False)
    tide_preference: Mapped[TidePreference | None] = mapped_column(
        str_enum(TidePreference, "spot_tide_preference")
    )
    wave_height_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    wave_height_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    height_factor: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    # Calibrated breaking-height coefficients, only once enough observations exist:
    # {"a": float, "b": float, "c": float, "n_observations": int, "r2": float}
    calibration: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    skill_level: Mapped[SkillLevel] = mapped_column(
        str_enum(SkillLevel, "spot_skill_level"), nullable=False
    )
    best_months: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, server_default=text("'{}'::integer[]")
    )
    seasonality: Mapped[str] = mapped_column(Text, nullable=False, default="")
    accessibility_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hazards: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    airports: Mapped[list[SpotAirport]] = relationship(
        back_populates="spot",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="(SpotAirport.is_primary.desc(), SpotAirport.transfer_minutes)",
    )

    @property
    def primary_airport(self) -> SpotAirport | None:
        for a in self.airports:
            if a.is_primary:
                return a
        return self.airports[0] if self.airports else None


class SpotAirport(Base):
    __tablename__ = "spot_airports"
    __table_args__ = (
        UniqueConstraint("spot_id", "airport_iata"),
        CheckConstraint("transfer_minutes >= 0", name="transfer_positive"),
        Index(
            "uq_spot_airports_one_primary",
            "spot_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    airport_iata: Mapped[str] = mapped_column(
        String(3), ForeignKey("airports.iata", ondelete="RESTRICT"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transfer_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    transfer_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="car")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    spot: Mapped[SurfSpot] = relationship(back_populates="airports")
    airport: Mapped[Airport] = relationship(lazy="joined")
