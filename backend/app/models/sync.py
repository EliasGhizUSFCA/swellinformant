"""Synced data pipeline: one row per coordinated sync (app.services.sync).

A sync acquires every component whose model run is new (spot forecasts, ensemble, map
layers), then blends, predicts, publishes and runs the user-facing stages, under a lease:
the row with status ``running`` is the lease, and the unique partial index
``uq_sync_runs_running`` allows at most one. ``lease_expires_at`` is renewed at every stage;
an expired lease is marked ``abandoned`` by the next sync.

* ``trigger``: who started it (app.services.sync.types.SyncTrigger).
* ``status``: running, success, partial (some component failed), failed (nothing new stored,
  nothing published), skipped (nothing was due) or abandoned (the lease expired).
* ``holder``: ``host:pid:token`` of the process holding the lease.
* ``anchor_cycle``: the GFS cycle a spot run and the map layers were coalesced on.
* ``plan``: the SyncPlan that started the sync (SyncPlan.as_dict()).
* ``stages``: one entry per stage, ``{name, status, started_at, duration_ms, cpu_s, detail}``.
* ``published_at``, ``map_run_id``, ``blend_run_id``: set by the publish stage. The map index
  serves ``map_run_id`` of the newest published row (SYNC_PUBLISH_GATE).
* ``error_message``: redacted; never served by the public API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, utcnow

SYNC_TRIGGERS = ("daemon", "celery", "cli", "pipeline", "github", "http")
SYNC_STATUSES = ("running", "success", "partial", "failed", "skipped", "abandoned")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        # The lease: at most one running sync.
        Index(
            "uq_sync_runs_running",
            "status",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_sync_runs_published_at", "published_at"),
        Index("ix_sync_runs_started_at", "started_at"),
        CheckConstraint(_in("trigger", SYNC_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", SYNC_STATUSES), name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    holder: Mapped[str | None] = mapped_column(String(120))
    anchor_cycle: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    stages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    map_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="SET NULL")
    )
    blend_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forecast_runs.id", ondelete="SET NULL")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
