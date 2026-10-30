"""forecast versions and per-search alert lead times (Swell 2.0, contract C2)

* forecast_versions: one row per staged refresh (status staging, published, superseded or
  failed), the forecast run and sync it came from, the wind and wave model runs, the
  components and per-spot runs (JSONB), the spot counts, the pipeline timestamps and the
  timings. The unique partial index uq_forecast_versions_published on status WHERE status =
  'published' allows at most one current version; ix_forecast_versions_published_at
  (published_at DESC) serves the newest publications.
* saved_searches.alert_lead_min_days / alert_lead_max_days: whole days, NOT NULL with server
  defaults 2 and 10 (existing searches get them), CHECK 0 <= min <= max <= 16.

The new table starts empty: until the first version is published the public reads keep the
run selection of Upgrade 4 (app.services.forecasts.versions.serving_runs). No existing row
changes except the two new search columns. Downgrade drops both.

Revision ID: 0009_forecast_versions
Revises: 0008_publish_stored_runs
Create Date: 2026-10-27 04:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_forecast_versions"
down_revision: str | Sequence[str] | None = "0008_publish_stored_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy (a migration must not follow later code): app.models.version.VERSION_STATUSES.
VERSION_STATUSES = ("staging", "published", "superseded", "failed")
ALERT_LEAD_CHECK = (
    "alert_lead_min_days >= 0 AND alert_lead_min_days <= alert_lead_max_days "
    "AND alert_lead_max_days <= 16"
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "forecast_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("source_code", sa.String(length=40), nullable=True),
        sa.Column("forecast_run_id", sa.Integer(), nullable=True),
        sa.Column("previous_version_id", sa.BigInteger(), nullable=True),
        sa.Column("sync_run_id", sa.Integer(), nullable=True),
        sa.Column("wind_model", sa.String(length=60), nullable=True),
        sa.Column("wind_model_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wave_model", sa.String(length=60), nullable=True),
        sa.Column("wave_model_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "wind_run_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "wave_run_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("fallback_from", sa.String(length=40), nullable=True),
        _jsonb("components"),
        _jsonb("spot_runs"),
        _count("spots_total"),
        _count("spots_updated"),
        _count("spots_stale"),
        _count("spots_failed"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upstream_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb("timings"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            _in("status", VERSION_STATUSES), name=op.f("ck_forecast_versions_status")
        ),
        sa.ForeignKeyConstraint(
            ["forecast_run_id"],
            ["forecast_runs.id"],
            name=op.f("fk_forecast_versions_forecast_run_id_forecast_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["previous_version_id"],
            ["forecast_versions.id"],
            name=op.f("fk_forecast_versions_previous_version_id_forecast_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_runs.id"],
            name=op.f("fk_forecast_versions_sync_run_id_sync_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forecast_versions")),
    )
    op.create_index(
        "uq_forecast_versions_published",
        "forecast_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "ix_forecast_versions_published_at",
        "forecast_versions",
        [sa.text("published_at DESC")],
        unique=False,
    )
    op.add_column(
        "saved_searches",
        sa.Column("alert_lead_min_days", sa.Integer(), server_default=sa.text("2"), nullable=False),
    )
    op.add_column(
        "saved_searches",
        sa.Column(
            "alert_lead_max_days", sa.Integer(), server_default=sa.text("10"), nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_saved_searches_alert_lead_days"), "saved_searches", ALERT_LEAD_CHECK
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_saved_searches_alert_lead_days"), "saved_searches", type_="check")
    op.drop_column("saved_searches", "alert_lead_max_days")
    op.drop_column("saved_searches", "alert_lead_min_days")
    op.drop_index("ix_forecast_versions_published_at", table_name="forecast_versions")
    op.drop_index(
        "uq_forecast_versions_published",
        table_name="forecast_versions",
        postgresql_where=sa.text("status = 'published'"),
    )
    op.drop_table("forecast_versions")
