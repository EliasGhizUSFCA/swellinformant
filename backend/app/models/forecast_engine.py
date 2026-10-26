"""Forecast-engine tables: spot physics, ensemble statistics, source comparisons, archive.

* ``spot_physical_profiles`` hold the per-break parameters the nearshore model needs. Every
  parameter carries a provenance flag in ``data_quality``; nothing here is invented
  bathymetry: unknown depths stay NULL and the model then makes its documented
  deep-water / no-depth-limit assumptions.
* ``forecast_ensemble_stats`` store ensemble mean / spread (GEFS-Wave) per spot and hour.
* ``forecast_comparisons`` record, for each blended hour, what every source said, the
  weights used and any disagreement flags.
* ``map_fields`` hold downsampled global grids (wave height, period, direction, 10 m wind,
  pressure, and optionally the primary swell, secondary swell and wind-sea partitions) per
  model run and valid time for the map's ocean layers: quantised to one byte per cell and
  deflate-compressed.
* ``prediction_archive`` is the verification record: what we predicted, from which model
  run and at which lead time. It is exempt from the forecast-run retention policy (it has
  its own, much longer one) because forecast-time values cannot be reconstructed later.
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, utcnow
from app.models.types import TimestampMixin

# Provenance of one physical parameter (values used in ``data_quality``).
PROVENANCE_VALUES = ("measured", "literature", "calibrated", "estimated", "unknown")


class SpotPhysicalProfile(TimestampMixin, Base):
    """Physical description of one break for the nearshore transformation model."""

    __tablename__ = "spot_physical_profiles"
    __table_args__ = (
        CheckConstraint("shore_normal_deg >= 0 AND shore_normal_deg < 360", name="shore_normal"),
        CheckConstraint(
            "forecast_point_depth_m IS NULL OR forecast_point_depth_m > 0", name="point_depth"
        ),
        CheckConstraint(
            "controlling_depth_m IS NULL OR controlling_depth_m > 0", name="controlling_depth"
        ),
        CheckConstraint(
            "breaker_index IS NULL OR (breaker_index > 0.3 AND breaker_index <= 1.2)",
            name="breaker_index",
        ),
        CheckConstraint("focusing_ref > 0 AND focusing_ref <= 3", name="focusing_ref"),
        CheckConstraint("focusing_kappa >= 0 AND focusing_kappa <= 3", name="focusing_kappa"),
    )

    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), primary_key=True
    )
    # Azimuth (degrees FROM) of a wave that arrives normal to the breaking contour.
    shore_normal_deg: Mapped[float] = mapped_column(Float, nullable=False)
    # Water depth at the wave-model extraction point; NULL = unknown (deep water assumed).
    forecast_point_depth_m: Mapped[float | None] = mapped_column(Float)
    # Depth controlling breaking (e.g. reef ledge at chart datum); NULL = no depth cap.
    controlling_depth_m: Mapped[float | None] = mapped_column(Float)
    breaker_index: Mapped[float | None] = mapped_column(Float)
    # Focusing prior Kf(T) = focusing_ref * (1 + kappa * max(0, T - t_star) / t_star).
    focusing_ref: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    focusing_kappa: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    focusing_t_star_s: Mapped[float] = mapped_column(Float, nullable=False, default=12.0)
    # 36 transmission factors in [0, 1] for 10-degree FROM-direction bins centred on
    # 0, 10, ..., 350. NULL = derive from the spot's swell window with smooth edges.
    shadow_table: Mapped[list[float] | None] = mapped_column(JSONB)
    # Directional spreading (degrees, 1 sigma) override; NULL = period-based default.
    spreading_deg: Mapped[float | None] = mapped_column(Float)
    # Named, cited regional correction (e.g. "caldwell_aucan_2007" for Hawaiian reefs).
    regional_correction: Mapped[str | None] = mapped_column(String(40))
    # Observation stations that sample the same offshore sea state:
    # [{"code": "aodn:tweed-offshore", "distance_km": 21.5, "relation": "upstream"}]
    reference_stations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # Provenance per parameter, e.g. {"shore_normal_deg": "estimated", ...}
    data_quality: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Optional spot-specific wind behaviour from research (offshore tolerance, sheltered
    # sectors, extra offshore bearings); NULL = the general wind model applies. Validated by
    # app.services.seed; every value carries its provenance.
    wind_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ForecastEnsembleStat(Base):
    """Ensemble mean and spread at a spot for one valid time (e.g. GEFS-Wave, 31 members)."""

    __tablename__ = "forecast_ensemble_stats"
    __table_args__ = (
        UniqueConstraint("run_id", "spot_id", "valid_time"),
        Index("ix_forecast_ensemble_stats_spot_valid", "spot_id", "valid_time"),
        CheckConstraint("hs_spread_m IS NULL OR hs_spread_m >= 0", name="hs_spread"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lead_hours: Mapped[float] = mapped_column(Float, nullable=False)
    members: Mapped[int | None] = mapped_column(Integer)
    hs_mean_m: Mapped[float | None] = mapped_column(Float)
    hs_spread_m: Mapped[float | None] = mapped_column(Float)
    tp_mean_s: Mapped[float | None] = mapped_column(Float)
    tp_spread_s: Mapped[float | None] = mapped_column(Float)
    swell1_height_mean_m: Mapped[float | None] = mapped_column(Float)
    swell1_height_spread_m: Mapped[float | None] = mapped_column(Float)
    swell1_period_mean_s: Mapped[float | None] = mapped_column(Float)
    swell1_period_spread_s: Mapped[float | None] = mapped_column(Float)
    wind_speed_mean_kmh: Mapped[float | None] = mapped_column(Float)
    wind_speed_spread_kmh: Mapped[float | None] = mapped_column(Float)
    # Member quantiles / exceedance probabilities when individual members were read.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class ForecastComparison(Base):
    """What each source forecast for one spot and hour, and how they were combined."""

    __tablename__ = "forecast_comparisons"
    __table_args__ = (
        UniqueConstraint("blend_run_id", "spot_id", "valid_time"),
        Index("ix_forecast_comparisons_spot_valid", "spot_id", "valid_time"),
        CheckConstraint("agreement IS NULL OR (agreement >= 0 AND agreement <= 1)", name="agree"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    blend_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False
    )
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # {source_code: {"family", "run_id", "issued_at", "lead_hours", "hs", "tp", "dp",
    #                "wind_kmh", "wind_dir", "weight"}}
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    independent_families: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Largest |ln(Hs_a / Hs_b)| between independent model families.
    hs_log_ratio_max: Mapped[float | None] = mapped_column(Float)
    tp_diff_max_s: Mapped[float | None] = mapped_column(Float)
    direction_diff_max_deg: Mapped[float | None] = mapped_column(Float)
    wind_diff_max_kmh: Mapped[float | None] = mapped_column(Float)
    # 0..1 summary used by the confidence index (1 = independent models agree).
    agreement: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    method: Mapped[str] = mapped_column(String(40), nullable=False)


class PredictionArchive(Base):
    """Immutable record of one issued prediction (offshore inputs + our estimate)."""

    __tablename__ = "prediction_archive"
    __table_args__ = (
        # One row per run, spot and valid time (two runs may share an issue hour).
        UniqueConstraint(
            "source_code", "run_key", "spot_id", "valid_time", name="uq_prediction_archive_key"
        ),
        Index("ix_prediction_archive_spot_valid", "spot_id", "valid_time"),
        Index("ix_prediction_archive_issued", "issued_at"),
        CheckConstraint("lead_hours >= -6", name="lead"),
        CheckConstraint(
            "uncertainty_kind IS NULL OR uncertainty_kind IN "
            "('heuristic', 'ensemble_heuristic', 'calibrated')",
            name="uncertainty_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    spot_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("surf_spots.id", ondelete="CASCADE"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    run_key: Mapped[str] = mapped_column(String(120), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lead_hours: Mapped[float] = mapped_column(Float, nullable=False)
    sig_wave_height_m: Mapped[float | None] = mapped_column(Float)
    wave_period_s: Mapped[float | None] = mapped_column(Float)
    wave_direction_deg: Mapped[float | None] = mapped_column(Float)
    primary_swell_height_m: Mapped[float | None] = mapped_column(Float)
    primary_swell_period_s: Mapped[float | None] = mapped_column(Float)
    primary_swell_direction_deg: Mapped[float | None] = mapped_column(Float)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float)
    wind_direction_deg: Mapped[float | None] = mapped_column(Float)
    breaking_height_mid_ft: Mapped[float | None] = mapped_column(Float)
    breaking_p10_ft: Mapped[float | None] = mapped_column(Float)
    breaking_p90_ft: Mapped[float | None] = mapped_column(Float)
    uncertainty_kind: Mapped[str | None] = mapped_column(String(24))
    score: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[int | None] = mapped_column(Integer)
    method: Mapped[str | None] = mapped_column(String(40))
    algorithm_version: Mapped[str | None] = mapped_column(String(16))
    ml_model_version: Mapped[str | None] = mapped_column(String(60))
    # The two confidence assessments as issued (kept so they can be checked later).
    forecast_confidence_class: Mapped[str | None] = mapped_column(String(12))
    local_confidence_class: Mapped[str | None] = mapped_column(String(12))
    research_class: Mapped[str | None] = mapped_column(String(12))
    confidence_method: Mapped[str | None] = mapped_column(String(32))
    confidence_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


# Map field variables (map_fields.variable) and their unit. The six core variables define a
# complete (servable) valid time; the nine partition variables are optional layers that a run
# may or may not store, and their absence never makes a valid time incomplete.
CORE_MAP_FIELD_VARIABLES = {
    "hs": "m",  # significant wave height (offshore)
    "tp": "s",  # peak wave period
    "wave_dir": "deg",  # peak wave direction, degrees FROM
    "wind_u": "m/s",  # 10 m wind, eastward component
    "wind_v": "m/s",  # 10 m wind, northward component
    "pressure": "hPa",  # mean sea-level pressure
}
# Wave-model partitions: swell1 = primary swell, swell2 = secondary swell, windsea = wind
# sea. Heights are partition significant heights, periods partition mean periods, directions
# degrees FROM (as wave_dir; no +180).
PARTITION_MAP_FIELD_VARIABLES = {
    "swell1_hs": "m",
    "swell1_tp": "s",
    "swell1_dir": "deg",
    "swell2_hs": "m",
    "swell2_tp": "s",
    "swell2_dir": "deg",
    "windsea_hs": "m",
    "windsea_tp": "s",
    "windsea_dir": "deg",
}
MAP_FIELD_VARIABLES = CORE_MAP_FIELD_VARIABLES | PARTITION_MAP_FIELD_VARIABLES


class MapField(Base):
    """One downsampled global field of one model run at one valid time.

    ``data`` is zlib (RFC 1950) compressed uint8, ``nx * ny`` cells, row-major: row 0 is
    the northernmost latitude ``lat0``, row j is ``lat0 - j * dlat``; column i is
    ``lon0 + i * dlon`` (``lon0`` = -180, wrapping). value = offset + scale * byte, and
    byte 255 means no data (land, ice or missing).
    """

    __tablename__ = "map_fields"
    __table_args__ = (
        UniqueConstraint("run_id", "valid_time", "variable", name="uq_map_fields_key"),
        Index("ix_map_fields_variable_valid", "variable", "valid_time"),
        CheckConstraint(
            "variable IN (" + ", ".join(f"'{v}'" for v in MAP_FIELD_VARIABLES) + ")",
            name="variable",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False
    )
    source_code: Mapped[str] = mapped_column(String(40), nullable=False)
    run_key: Mapped[str] = mapped_column(String(120), nullable=False)
    cycle_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    variable: Mapped[str] = mapped_column(String(16), nullable=False)
    nx: Mapped[int] = mapped_column(Integer, nullable=False)
    ny: Mapped[int] = mapped_column(Integer, nullable=False)
    lat0: Mapped[float] = mapped_column(Float, nullable=False)
    lon0: Mapped[float] = mapped_column(Float, nullable=False)
    dlat: Mapped[float] = mapped_column(Float, nullable=False)
    dlon: Mapped[float] = mapped_column(Float, nullable=False)
    scale: Mapped[float] = mapped_column(Float, nullable=False)
    offset: Mapped[float] = mapped_column(Float, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
