"""research confidence, forecast confidence and map fields

* spot_research_sources / spot_research_claims: the evidence consulted per spot and what
  each source says about each researched variable;
* spot_research_assessments: computed research confidence per variable and overall, with
  methodology version and evidence fingerprint; history kept (one current row per spot);
* forecast and local confidence classes, method, inputs, issue time, lead time and the
  research assessment used, on surf_quality_predictions and (as issued) prediction_archive;
* map_fields: downsampled global ocean/atmosphere grids for the map layers.

Existing rows are kept; the new prediction columns are nullable and filled by the next
scoring run.

Revision ID: 0004_confidence_map_fields
Revises: 0003_forecast_engine
Create Date: 2026-10-11 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_confidence_map_fields"
down_revision: str | Sequence[str] | None = "0003_forecast_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "spot_research_assessments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spot_id", sa.Integer(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("methodology_version", sa.String(length=32), nullable=False),
        sa.Column("overall_class", sa.String(length=12), nullable=False),
        sa.Column("per_variable", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("gaps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("research_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "overall_class IN ('high', 'moderate', 'low')",
            name=op.f("ck_spot_research_assessments_overall_class"),
        ),
        sa.ForeignKeyConstraint(
            ["spot_id"],
            ["surf_spots.id"],
            name=op.f("fk_spot_research_assessments_spot_id_surf_spots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spot_research_assessments")),
    )
    op.create_index(
        op.f("ix_spot_research_assessments_spot_id"),
        "spot_research_assessments",
        ["spot_id"],
        unique=False,
    )
    op.create_index(
        "uq_spot_research_assessments_one_current",
        "spot_research_assessments",
        ["spot_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "spot_research_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spot_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("publisher", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("published_year", sa.Integer(), nullable=True),
        sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('survey_data', 'peer_reviewed', 'official', 'guidebook', 'encyclopedia', 'surf_forecast_site', 'magazine', 'community')",
            name=op.f("ck_spot_research_sources_source_type"),
        ),
        sa.ForeignKeyConstraint(
            ["spot_id"],
            ["surf_spots.id"],
            name=op.f("fk_spot_research_sources_spot_id_surf_spots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spot_research_sources")),
        sa.UniqueConstraint("spot_id", "source_key", name="uq_spot_research_sources_key"),
    )
    op.create_index(
        op.f("ix_spot_research_sources_spot_id"), "spot_research_sources", ["spot_id"], unique=False
    )
    op.create_table(
        "map_fields",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.String(length=40), nullable=False),
        sa.Column("run_key", sa.String(length=120), nullable=False),
        sa.Column("cycle_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("variable", sa.String(length=16), nullable=False),
        sa.Column("nx", sa.Integer(), nullable=False),
        sa.Column("ny", sa.Integer(), nullable=False),
        sa.Column("lat0", sa.Float(), nullable=False),
        sa.Column("lon0", sa.Float(), nullable=False),
        sa.Column("dlat", sa.Float(), nullable=False),
        sa.Column("dlon", sa.Float(), nullable=False),
        sa.Column("scale", sa.Float(), nullable=False),
        sa.Column("offset", sa.Float(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "variable IN ('hs', 'tp', 'wave_dir', 'wind_u', 'wind_v', 'pressure')",
            name=op.f("ck_map_fields_variable"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["forecast_runs.id"],
            name=op.f("fk_map_fields_run_id_forecast_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_map_fields")),
        sa.UniqueConstraint("run_id", "valid_time", "variable", name="uq_map_fields_key"),
    )
    op.create_index(
        "ix_map_fields_variable_valid", "map_fields", ["variable", "valid_time"], unique=False
    )
    op.create_table(
        "spot_research_claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("spot_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("variable", sa.String(length=24), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("agrees", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "variable IN ('swell_direction', 'swell_size', 'swell_period', 'wind', 'tide', 'bathymetry', 'seasonality', 'local_behavior')",
            name=op.f("ck_spot_research_claims_variable"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["spot_research_sources.id"],
            name=op.f("fk_spot_research_claims_source_id_spot_research_sources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["spot_id"],
            ["surf_spots.id"],
            name=op.f("fk_spot_research_claims_spot_id_surf_spots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spot_research_claims")),
    )
    op.create_index(
        "ix_spot_research_claims_spot_variable",
        "spot_research_claims",
        ["spot_id", "variable"],
        unique=False,
    )
    op.add_column(
        "prediction_archive",
        sa.Column("forecast_confidence_class", sa.String(length=12), nullable=True),
    )
    op.add_column(
        "prediction_archive",
        sa.Column("local_confidence_class", sa.String(length=12), nullable=True),
    )
    op.add_column(
        "prediction_archive", sa.Column("research_class", sa.String(length=12), nullable=True)
    )
    op.add_column(
        "prediction_archive", sa.Column("confidence_method", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "prediction_archive",
        sa.Column("confidence_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "surf_quality_predictions",
        sa.Column("forecast_confidence_class", sa.String(length=12), nullable=True),
    )
    op.add_column(
        "surf_quality_predictions",
        sa.Column("local_confidence_class", sa.String(length=12), nullable=True),
    )
    op.add_column(
        "surf_quality_predictions",
        sa.Column("confidence_method", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "surf_quality_predictions",
        sa.Column("confidence_inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "surf_quality_predictions",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("surf_quality_predictions", sa.Column("lead_hours", sa.Float(), nullable=True))
    op.add_column(
        "surf_quality_predictions", sa.Column("research_assessment_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_predictions_research_assessment",
        "surf_quality_predictions",
        "spot_research_assessments",
        ["research_assessment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_predictions_research_assessment", "surf_quality_predictions", type_="foreignkey"
    )
    op.drop_column("surf_quality_predictions", "research_assessment_id")
    op.drop_column("surf_quality_predictions", "lead_hours")
    op.drop_column("surf_quality_predictions", "issued_at")
    op.drop_column("surf_quality_predictions", "confidence_inputs")
    op.drop_column("surf_quality_predictions", "confidence_method")
    op.drop_column("surf_quality_predictions", "local_confidence_class")
    op.drop_column("surf_quality_predictions", "forecast_confidence_class")
    op.drop_column("prediction_archive", "confidence_inputs")
    op.drop_column("prediction_archive", "confidence_method")
    op.drop_column("prediction_archive", "research_class")
    op.drop_column("prediction_archive", "local_confidence_class")
    op.drop_column("prediction_archive", "forecast_confidence_class")
    op.drop_index("ix_spot_research_claims_spot_variable", table_name="spot_research_claims")
    op.drop_table("spot_research_claims")
    op.drop_index("ix_map_fields_variable_valid", table_name="map_fields")
    op.drop_table("map_fields")
    op.drop_index(op.f("ix_spot_research_sources_spot_id"), table_name="spot_research_sources")
    op.drop_table("spot_research_sources")
    op.drop_index(
        "uq_spot_research_assessments_one_current",
        table_name="spot_research_assessments",
        postgresql_where=sa.text("is_current"),
    )
    op.drop_index(
        op.f("ix_spot_research_assessments_spot_id"), table_name="spot_research_assessments"
    )
    op.drop_table("spot_research_assessments")
