"""Sync status served in GET /api/health/data (``DataHealthOut.sync``).

Public, operational metadata only: run keys, cycles, timestamps, durations and states. No
error text (``sync_runs.error_message`` stays in the database and in admin emails) and no
settings values other than the check interval and the CPU budget.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SyncComponentOut(BaseModel):
    kind: Literal["spots", "ensemble", "map"]
    source: str = Field(description="Forecast source code, e.g. open_meteo_gfs")
    provider: str
    state: Literal["current", "new", "waiting", "in_progress", "failed", "unknown", "disabled"]
    stored_run_key: str | None = None
    available_run_key: str | None = None
    available_cycle: datetime | None = None
    # Plain, written by the service (e.g. "waiting for the map layers until 14:30 UTC").
    detail: str | None = None


class SyncStageOut(BaseModel):
    name: str
    status: Literal["success", "skipped", "failed"]
    duration_ms: int
    cpu_s: float | None = None


class SyncRunOut(BaseModel):
    id: int
    trigger: Literal["daemon", "celery", "cli", "pipeline", "github", "http"]
    status: Literal["running", "success", "partial", "failed", "skipped", "abandoned"]
    started_at: datetime
    finished_at: datetime | None = None
    published_at: datetime | None = None
    anchor_cycle: datetime | None = None
    stages: list[SyncStageOut] = Field(default_factory=list)


class SyncHealthOut(BaseModel):
    runner: Literal["celery", "daemon"]
    # SYNC_CHECK_MINUTES.
    check_minutes: int
    # The daemon heartbeat (JOB_RUNNER=daemon) or the newest sync.tick job (celery).
    last_check_at: datetime | None = None
    heartbeat_stale: bool = False
    last_run: SyncRunOut | None = None
    last_published: SyncRunOut | None = None
    components: list[SyncComponentOut] = Field(default_factory=list)
    # CPU used by the sync runner this UTC month, and SYNC_CPU_BUDGET_HOURS (None = no budget).
    cpu_hours_month: float | None = None
    cpu_budget_hours: float | None = None
