"""sync runs: one row per coordinated sync of the synced data pipeline

* sync_runs: trigger, status, lease (lease_expires_at, holder), the plan and the per-stage
  results, and what the publish stage published (published_at, map_run_id, blend_run_id);
* uq_sync_runs_running: a unique partial index on status WHERE status = 'running', so at
  most one sync holds the lease;
* ix_sync_runs_published_at, ix_sync_runs_started_at.

The new table starts empty; nothing else changes. Downgrade drops it.

Revision ID: 0007_sync_runs
Revises: 0006_forecast_upgrade
Create Date: 2026-10-18 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_sync_runs"
down_revision: str | Sequence[str] | None = "0006_forecast_upgrade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_TRIGGERS = ("daemon", "celery", "cli", "pipeline", "github", "http")
SYNC_STATUSES = ("running", "success", "partial", "failed", "skipped", "abandoned")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("holder", sa.String(length=120), nullable=True),
        sa.Column("anchor_cycle", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "stages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("map_run_id", sa.Integer(), nullable=True),
        sa.Column("blend_run_id", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("trigger", SYNC_TRIGGERS), name=op.f("ck_sync_runs_trigger")),
        sa.CheckConstraint(_in("status", SYNC_STATUSES), name=op.f("ck_sync_runs_status")),
        sa.ForeignKeyConstraint(
            ["map_run_id"],
            ["forecast_runs.id"],
            name=op.f("fk_sync_runs_map_run_id_forecast_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["blend_run_id"],
            ["forecast_runs.id"],
            name=op.f("fk_sync_runs_blend_run_id_forecast_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "uq_sync_runs_running",
        "sync_runs",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_sync_runs_published_at", "sync_runs", ["published_at"], unique=False)
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sync_runs_started_at", table_name="sync_runs")
    op.drop_index("ix_sync_runs_published_at", table_name="sync_runs")
    op.drop_index(
        "uq_sync_runs_running",
        table_name="sync_runs",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_table("sync_runs")
