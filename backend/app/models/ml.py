"""Experiment tracking, model registry and verification results.

A model version is only ever ``active`` after its held-out evaluation beat the raw model
and the simple baselines by the documented deployment criteria (see
app.services.ml.registry). Rejected candidates are kept, with the metrics that rejected
them, so the decision is auditable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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

from app.core.database import Base, utcnow
from app.models.enums import MLModelStatus
from app.models.types import str_enum


class MLExperiment(Base):
    __tablename__ = "ml_experiments"
    __table_args__ = (Index("ix_ml_experiments_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # e.g. "offshore_hs_log_residual"
    target: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Stations, sources, period, row counts, feature list, leakage guards applied.
    dataset: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Split design: chronological cut-offs, gap, rolling-origin folds.
    split: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Per candidate: metrics by lead bucket on validation and test sets.
    results: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    code_version: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MLModelVersion(Base):
    __tablename__ = "ml_model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        Index(
            "uq_ml_model_versions_one_active",
            "name",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    experiment_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ml_experiments.id", ondelete="SET NULL")
    )
    model_type: Mapped[str] = mapped_column(String(60), nullable=False)
    target: Mapped[str] = mapped_column(String(60), nullable=False)
    feature_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Training window, stations, sources and row counts.
    training: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Held-out metrics for this model and for every baseline, by lead bucket.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Deployment-criteria evaluation: each criterion, its threshold and whether it passed.
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Where the model may be applied: station codes / spot slugs / lead range.
    applicability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(String(400))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[MLModelStatus] = mapped_column(
        str_enum(MLModelStatus, "ml_model_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    experiment: Mapped[MLExperiment | None] = relationship(lazy="select")

    @property
    def label(self) -> str:
        return f"{self.name}@v{self.version}"


class PredictionValidation(Base):
    """One forecast/observation pair at a station (the raw material of every metric)."""

    __tablename__ = "prediction_validation"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "source_code",
            "model_version",
            "cycle_time",
            "valid_time",
            "variable",
            name="uq_prediction_validation_pair",
        ),
        Index("ix_prediction_validation_lookup", "source_code", "variable", "lead_hours"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("buoy_stations.id", ondelete="CASCADE"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    # "raw" for the unmodified model, otherwise "<name>@v<version>" or a baseline name.
    model_version: Mapped[str] = mapped_column(String(60), nullable=False, default="raw")
    cycle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lead_hours: Mapped[float] = mapped_column(Float, nullable=False)
    variable: Mapped[str] = mapped_column(String(16), nullable=False)  # hs, tp, dir
    forecast_value: Mapped[float] = mapped_column(Float, nullable=False)
    observed_value: Mapped[float] = mapped_column(Float, nullable=False)
    # forecast - observed; for directions the signed circular difference in degrees.
    error: Mapped[float] = mapped_column(Float, nullable=False)
    observed_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ForecastAccuracyMetric(Base):
    """Aggregated verification statistics for one scope, source, variable and lead bucket."""

    __tablename__ = "forecast_accuracy_metrics"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "scope_key",
            "source_code",
            "model_version",
            "variable",
            "lead_bucket_h",
            "period_start",
            "period_end",
            name="uq_forecast_accuracy_metrics_key",
        ),
        CheckConstraint("n >= 0", name="n_positive"),
        CheckConstraint("scope IN ('station', 'region', 'global', 'spot')", name="scope"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(80), nullable=False)
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    model_version: Mapped[str] = mapped_column(String(60), nullable=False, default="raw")
    variable: Mapped[str] = mapped_column(String(16), nullable=False)
    lead_bucket_h: Mapped[int] = mapped_column(Integer, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    bias: Mapped[float | None] = mapped_column(Float)
    mae: Mapped[float | None] = mapped_column(Float)
    rmse: Mapped[float | None] = mapped_column(Float)
    scatter_index: Mapped[float | None] = mapped_column(Float)
    hh: Mapped[float | None] = mapped_column(Float)
    correlation: Mapped[float | None] = mapped_column(Float)
    skill_vs_persistence: Mapped[float | None] = mapped_column(Float)
    skill_vs_climatology: Mapped[float | None] = mapped_column(Float)
    # Confidence intervals (block bootstrap by cycle), event scores, coverage ...
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
