"""Spot research evidence and research-confidence assessments.

Research confidence answers "how well do we understand this break's ideal conditions?"
(swell direction, size, period, wind, tide, bathymetry, seasonality, local behaviour). It
is a different question from forecast confidence ("how trustworthy is this upcoming
prediction?", stored on each prediction) and the two are never merged into one number.

* ``spot_research_sources``: a source actually consulted for a spot (title, URL,
  publisher, type, year, when it was accessed).
* ``spot_research_claims``: what one source says about one variable, with a short excerpt
  and whether it agrees with the value in the spot profile.
* ``spot_research_assessments``: the computed classification per variable and overall,
  with the methodology version and a fingerprint of the evidence used. Assessments are
  never updated in place: a changed methodology or new evidence inserts a new row and
  marks it current, so earlier assessments stay available for evaluation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, utcnow

# Researched variables (spot_research_claims.variable, assessment per_variable keys).
RESEARCH_VARIABLES = (
    "swell_direction",
    "swell_size",
    "swell_period",
    "wind",
    "tide",
    "bathymetry",
    "seasonality",
    "local_behavior",
)
# Kinds of source (spot_research_sources.source_type), strongest evidence first.
SOURCE_TYPES = (
    "survey_data",  # measured bathymetry / instrument records
    "peer_reviewed",
    "official",  # government, harbour, marine-park or event-organiser publications
    "guidebook",  # published surf guides (e.g. The Stormrider Guide)
    "encyclopedia",
    "surf_forecast_site",  # spot guides of forecast sites (descriptive pages only)
    "magazine",
    "community",  # forums, blogs, user-generated spot pages
)
CONFIDENCE_CLASSES = ("high", "moderate", "low")


class SpotResearchSource(Base):
    __tablename__ = "spot_research_sources"
    __table_args__ = (
        UniqueConstraint("spot_id", "source_key", name="uq_spot_research_sources_key"),
        CheckConstraint(
            "source_type IN (" + ", ".join(f"'{t}'" for t in SOURCE_TYPES) + ")",
            name="source_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    publisher: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    published_year: Mapped[int | None] = mapped_column(Integer)
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class SpotResearchClaim(Base):
    __tablename__ = "spot_research_claims"
    __table_args__ = (
        CheckConstraint(
            "variable IN (" + ", ".join(f"'{v}'" for v in RESEARCH_VARIABLES) + ")",
            name="variable",
        ),
        Index("ix_spot_research_claims_spot_variable", "spot_id", "variable"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("spot_research_sources.id", ondelete="CASCADE"), nullable=False
    )
    variable: Mapped[str] = mapped_column(String(24), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalised value when one can be stated (e.g. {"min": 280, "max": 320} degrees FROM).
    value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Does the source support the value in the spot profile? None = not comparable.
    agrees: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class SpotResearchAssessment(Base):
    __tablename__ = "spot_research_assessments"
    __table_args__ = (
        Index(
            "uq_spot_research_assessments_one_current",
            "spot_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        CheckConstraint("overall_class IN ('high', 'moderate', 'low')", name="overall_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    methodology_version: Mapped[str] = mapped_column(String(32), nullable=False)
    overall_class: Mapped[str] = mapped_column(String(12), nullable=False)
    # variable -> {class, sources, independent_sources, strongest_source_type, agreement,
    #              newest_year, summary, gaps: [...]}
    per_variable: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    gaps: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # sha256 of the evidence (sources + claims + profile values) the assessment used.
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Newest accessed_at among the evidence: when the research itself was last updated.
    research_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
