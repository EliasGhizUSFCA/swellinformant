"""Tide stations, tide predictions and the station(s) each spot uses.

* ``tide_stations``: a prediction point of one provider: a NOAA CO-OPS harmonic or
  subordinate station, or a point of a global harmonic tide model (kind "model"). Heights
  are always relative to a named datum; ``datums`` holds the offsets the provider publishes
  (e.g. {"MSL": 0.911} metres above MLLW).
* ``tide_predictions``: predicted heights (metres, relative to ``datum``) at regular times
  (the curve, ``is_event`` false) and at high and low water (``is_event`` true with
  ``event_type``). ``interpolated`` marks curve points computed by this app between a
  subordinate station's published high and low waters; they are never the station's own
  output. Only future predictions and a short past window are kept
  (TIDE_RETENTION_PAST_DAYS).
* ``spot_tide_stations``: which station(s) represent a spot, with the distance and why.

These are predictions (astronomical tide), not observed water levels.
"""

from __future__ import annotations

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
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.types import TimestampMixin


class TideStation(TimestampMixin, Base):
    __tablename__ = "tide_stations"
    __table_args__ = (
        UniqueConstraint("provider", "code"),
        CheckConstraint("kind IN ('harmonic', 'subordinate', 'model')", name="kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The provider's own identifier, e.g. "9414131" (CO-OPS) or "eot20:mavericks".
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    kind: Mapped[str | None] = mapped_column(String(16))
    # Subordinate stations: the harmonic station their predictions are derived from.
    reference_code: Mapped[str | None] = mapped_column(String(32))
    # Datum offsets published by the provider, metres, e.g. {"MSL": 0.911, "MLLW": 0.0}.
    datums: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    licence: Mapped[str | None] = mapped_column(String(64))
    attribution: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class TidePrediction(Base):
    __tablename__ = "tide_predictions"
    __table_args__ = (
        UniqueConstraint("station_id", "valid_time", "datum", "is_event"),
        Index("ix_tide_predictions_station_valid", "station_id", "valid_time"),
        CheckConstraint("event_type IN ('high', 'low')", name="event_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tide_stations.id", ondelete="CASCADE"), nullable=False
    )
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    height_m: Mapped[float] = mapped_column(Float, nullable=False)
    # Datum the height is relative to: "MLLW", "MSL", "LAT" or "model_msl".
    datum: Mapped[str] = mapped_column(String(16), nullable=False)
    is_event: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    event_type: Mapped[str | None] = mapped_column(String(4))
    interpolated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # What produced the value, e.g. "noaa_coops" or "eot20".
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpotTideStation(Base):
    __tablename__ = "spot_tide_stations"
    __table_args__ = (CheckConstraint("relation IN ('primary', 'fallback')", name="relation"),)

    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), primary_key=True
    )
    station_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tide_stations.id", ondelete="CASCADE"), primary_key=True
    )
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    relation: Mapped[str | None] = mapped_column(String(16))
    # Why this station represents the spot (distance, same coast, reference station ...).
    justification: Mapped[str | None] = mapped_column(Text)
