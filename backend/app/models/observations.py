"""Observations and model forecasts at observation sites (for verification and training).

* ``buoy_stations`` / ``buoy_observations``: measured offshore sea state (AODN, Met Office,
  NDBC, CDIP ...). Only real measurements go here — never model output.
* ``point_forecasts``: what a numerical model forecast at a station location for a given
  cycle and lead time (GFS-Wave station bulletins, GEFS-Wave, ECMWF grid samples,
  GEFSv12 reforecast tables). Pairing these with observations by valid time gives true
  forecast errors at each lead.
* ``nearshore_observations``: reported surf at a break (consented user reports, lifeguard
  logs, camera-derived). The only possible ground truth for breaking-height calibration.
"""

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
from app.models.types import TimestampMixin

# Height definitions kept explicit so model Hm0 is never silently compared with H1/3.
HS_KINDS = ("spectral_hm0", "h_third", "unknown")
QC_FLAGS = ("good", "probably_good", "not_qc", "bad")


class BuoyStation(TimestampMixin, Base):
    __tablename__ = "buoy_stations"
    __table_args__ = (
        UniqueConstraint("code"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="lat_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="lon_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Provider-qualified identifier, e.g. "ndbc:46026", "aodn:tweed-offshore".
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    water_depth_m: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    license: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    attribution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Provider-specific identifiers (site_name, WMO id, model bulletin id ...).
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )


class BuoyObservation(Base):
    __tablename__ = "buoy_observations"
    __table_args__ = (
        UniqueConstraint("station_id", "observed_at"),
        Index("ix_buoy_observations_observed_at", "observed_at"),
        CheckConstraint("hs_m IS NULL OR (hs_m >= 0 AND hs_m < 30)", name="hs_range"),
        CheckConstraint("tp_s IS NULL OR (tp_s > 0 AND tp_s < 40)", name="tp_range"),
        CheckConstraint("hs_kind IN ('spectral_hm0', 'h_third', 'unknown')", name="hs_kind"),
        CheckConstraint("qc_flag IN ('good', 'probably_good', 'not_qc', 'bad')", name="qc_flag"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("buoy_stations.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hs_m: Mapped[float | None] = mapped_column(Float)
    hs_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    tp_s: Mapped[float | None] = mapped_column(Float)
    tm_s: Mapped[float | None] = mapped_column(Float)
    tm_kind: Mapped[str | None] = mapped_column(String(8))  # "tm01", "tm02", "apd"
    peak_direction_deg: Mapped[float | None] = mapped_column(Float)  # FROM
    mean_direction_deg: Mapped[float | None] = mapped_column(Float)  # FROM
    swell_hs_m: Mapped[float | None] = mapped_column(Float)
    swell_tp_s: Mapped[float | None] = mapped_column(Float)
    swell_direction_deg: Mapped[float | None] = mapped_column(Float)
    windsea_hs_m: Mapped[float | None] = mapped_column(Float)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    qc_flag: Mapped[str] = mapped_column(String(16), nullable=False, default="not_qc")
    dataset: Mapped[str] = mapped_column(String(80), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    station: Mapped[BuoyStation] = relationship(lazy="joined")


class PointForecast(Base):
    """A model forecast at a station location for one (cycle, valid time)."""

    __tablename__ = "point_forecasts"
    __table_args__ = (
        UniqueConstraint("station_id", "source_code", "cycle_time", "valid_time"),
        Index("ix_point_forecasts_station_valid", "station_id", "valid_time"),
        Index("ix_point_forecasts_source_cycle", "source_code", "cycle_time"),
        CheckConstraint("lead_hours >= 0", name="lead"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("buoy_stations.id", ondelete="CASCADE"), nullable=False
    )
    # e.g. "gfswave_bulletin", "gefs_wave_mean", "ecmwf_ifs_wave", "gefsv12_reforecast_c00"
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    # Dependency family (see app.services.forecasts.sources): "ncep_gfs", "ecmwf_ifs" ...
    family: Mapped[str] = mapped_column(String(40), nullable=False)
    cycle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lead_hours: Mapped[float] = mapped_column(Float, nullable=False)
    hs_m: Mapped[float | None] = mapped_column(Float)
    tp_s: Mapped[float | None] = mapped_column(Float)
    direction_deg: Mapped[float | None] = mapped_column(Float)  # FROM
    swell1_hs_m: Mapped[float | None] = mapped_column(Float)
    swell1_tp_s: Mapped[float | None] = mapped_column(Float)
    swell1_direction_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    hs_spread_m: Mapped[float | None] = mapped_column(Float)
    # Where the model value came from (grid cell or bulletin point) and its distance.
    model_latitude: Mapped[float | None] = mapped_column(Float)
    model_longitude: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class NearshoreObservation(Base):
    """Observed surf at a break. Heights are trough-to-crest faces in feet."""

    __tablename__ = "nearshore_observations"
    __table_args__ = (
        Index("ix_nearshore_observations_spot_time", "spot_id", "observed_at"),
        CheckConstraint("face_height_min_ft >= 0", name="min_positive"),
        CheckConstraint("face_height_max_ft >= face_height_min_ft", name="height_order"),
        CheckConstraint(
            "height_convention IN ('face_h13', 'face_h110', 'hawaiian_scale')",
            name="convention",
        ),
        CheckConstraint("quality_rating IS NULL OR quality_rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint(
            "source IN ('user_report', 'lifeguard', 'camera_derived', 'expert')", name="source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    face_height_min_ft: Mapped[float] = mapped_column(Float, nullable=False)
    face_height_max_ft: Mapped[float] = mapped_column(Float, nullable=False)
    height_convention: Mapped[str] = mapped_column(String(16), nullable=False)
    quality_rating: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
