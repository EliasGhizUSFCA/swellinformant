"""Forecast sources, model runs, normalised forecasts, quality predictions and swell events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
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
    ForecastRunStatus,
    QualityLabel,
    SwellEventStatus,
)
from app.models.spot import SurfSpot
from app.models.types import TimestampMixin, str_enum


class ForecastSource(TimestampMixin, Base):
    """A configured forecast provider (e.g. NOAA GFS + GFS-Wave via Open-Meteo)."""

    __tablename__ = "forecast_sources"
    __table_args__ = (UniqueConstraint("code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    wave_model: Mapped[str | None] = mapped_column(String(60))
    atmosphere_model: Mapped[str | None] = mapped_column(String(60))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    license_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ForecastRun(Base):
    """One acquisition of a model run for all spots from a given source.

    ``run_key`` identifies the upstream model cycle(s) (e.g. ``gfswave:2026101006|gfs:...``)
    so the same cycle is never ingested twice.
    """

    __tablename__ = "forecast_runs"
    __table_args__ = (
        UniqueConstraint("source_id", "run_key"),
        Index("ix_forecast_runs_source_status", "source_id", "status", "completed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_sources.id", ondelete="CASCADE"), nullable=False
    )
    run_key: Mapped[str] = mapped_column(String(120), nullable=False)
    wave_model_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    atmosphere_model_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ForecastRunStatus] = mapped_column(
        str_enum(ForecastRunStatus, "forecast_run_status"), nullable=False
    )
    spot_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    predictions_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[ForecastSource] = relationship(lazy="joined")


class WaveForecast(Base):
    """Normalised offshore conditions at a spot's forecast grid point for one valid time.

    Heights are metres, periods seconds, directions compass degrees FROM, wind km/h.
    ``sig_wave_height_m`` is the offshore significant wave height of the combined sea
    state — it is NOT the breaking wave height surfers see at the beach.
    """

    __tablename__ = "wave_forecasts"
    __table_args__ = (
        UniqueConstraint("run_id", "spot_id", "valid_time"),
        Index("ix_wave_forecasts_spot_valid", "spot_id", "valid_time"),
        CheckConstraint("sig_wave_height_m IS NULL OR sig_wave_height_m >= 0", name="hs_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sig_wave_height_m: Mapped[float | None] = mapped_column(Float)
    wave_period_s: Mapped[float | None] = mapped_column(Float)
    wave_direction_deg: Mapped[float | None] = mapped_column(Float)
    primary_swell_height_m: Mapped[float | None] = mapped_column(Float)
    primary_swell_period_s: Mapped[float | None] = mapped_column(Float)
    primary_swell_direction_deg: Mapped[float | None] = mapped_column(Float)
    secondary_swell_height_m: Mapped[float | None] = mapped_column(Float)
    secondary_swell_period_s: Mapped[float | None] = mapped_column(Float)
    secondary_swell_direction_deg: Mapped[float | None] = mapped_column(Float)
    wind_wave_height_m: Mapped[float | None] = mapped_column(Float)
    wind_wave_period_s: Mapped[float | None] = mapped_column(Float)
    wind_wave_direction_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    wind_gust_kmh: Mapped[float | None] = mapped_column(Float)
    pressure_msl_hpa: Mapped[float | None] = mapped_column(Float)
    sea_level_m: Mapped[float | None] = mapped_column(Float)
    # Deep-water energy flux of the primary swell, P ≈ 0.49 · H² · T  (kW per metre of crest)
    wave_power_kw_m: Mapped[float | None] = mapped_column(Float)
    missing_fields: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class SurfQualityPrediction(Base):
    __tablename__ = "surf_quality_predictions"
    __table_args__ = (
        UniqueConstraint("forecast_id"),
        Index("ix_surf_quality_predictions_spot_valid", "spot_id", "valid_time"),
        Index("ix_surf_quality_predictions_run", "run_id"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score_range"),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="confidence_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    forecast_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("wave_forecasts.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_daylight: Mapped[bool] = mapped_column(Boolean, nullable=False)
    breaking_height_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    breaking_height_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    breaking_height_method: Mapped[str] = mapped_column(String(40), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[QualityLabel] = mapped_column(
        str_enum(QualityLabel, "prediction_quality_label"), nullable=False
    )
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    wind_relation: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_label: Mapped[ConfidenceLabel] = mapped_column(
        str_enum(ConfidenceLabel, "prediction_confidence_label"), nullable=False
    )
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    algorithm_version: Mapped[str] = mapped_column(String(16), nullable=False)

    forecast: Mapped[WaveForecast] = relationship(lazy="joined")


class SwellEvent(Base):
    """A contiguous window of good forecast surf at one spot.

    Events are updated in place as new model runs arrive, so a swell keeps one ID (and
    users get one alert) for its whole forecast life. The exclusion constraint created in
    the migration prevents two ACTIVE events overlapping in time at the same spot.
    """

    __tablename__ = "swell_events"
    __table_args__ = (
        Index("ix_swell_events_spot_start", "spot_id", "start_time"),
        Index("ix_swell_events_status_start", "status", "start_time"),
        CheckConstraint("end_time >= start_time", name="time_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SwellEventStatus] = mapped_column(
        str_enum(SwellEventStatus, "swell_event_status"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    peak_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    peak_score: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_score: Mapped[float] = mapped_column(Float, nullable=False)
    peak_label: Mapped[QualityLabel] = mapped_column(
        str_enum(QualityLabel, "event_quality_label"), nullable=False
    )
    peak_breaking_height_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    peak_breaking_height_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    peak_swell_height_m: Mapped[float | None] = mapped_column(Float)
    peak_swell_period_s: Mapped[float | None] = mapped_column(Float)
    peak_swell_direction_deg: Mapped[float | None] = mapped_column(Float)
    peak_wind_speed_kmh: Mapped[float | None] = mapped_column(Float)
    peak_wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    peak_wind_relation: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    qualifying_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_label: Mapped[ConfidenceLabel] = mapped_column(
        str_enum(ConfidenceLabel, "event_confidence_label"), nullable=False
    )
    latest_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="SET NULL")
    )
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("swell_events.id", ondelete="SET NULL")
    )

    spot: Mapped[SurfSpot] = relationship(lazy="joined")
