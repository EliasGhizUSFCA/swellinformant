"""Data-health report (GET /api/health/data), built by app.services.health.

Operational metadata only: run keys, timestamps, ages, limits, job statuses and reasons
written by the service itself. No personal data, no raw exception text, no settings
values other than the limits the checks use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

OverallStatus = Literal["ok", "degraded", "failing"]
Severity = Literal["degraded", "failing"]
SourceRole = Literal["forecast", "blend", "ensemble"]
Freshness = Literal["ok", "stale", "missing"]
MapFieldsStatus = Literal["ok", "stale", "missing", "off"]


class HealthProblemOut(BaseModel):
    key: str = Field(description="Stable identifier, e.g. forecast_stale:noaa_gfs_direct")
    severity: Severity
    message: str


class SourceHealthOut(BaseModel):
    code: str
    name: str
    # forecast = a FORECAST_PROVIDERS source; blend = the multi-model blend; ensemble =
    # GEFS-Wave statistics (uncertainty only).
    role: SourceRole
    status: Freshness
    newest_run_key: str | None = None
    newest_issued_at: datetime | None = None
    age_hours: float | None = None
    # None for the blend: it is stale as soon as one member is stale by its own limit.
    stale_after_hours: float | None = None
    latest_attempt_status: str | None = None
    latest_attempt_started_at: datetime | None = None


class JobHealthOut(BaseModel):
    job: str
    schedule: str
    status: Literal["ok", "failed", "overdue", "stuck", "running", "never_run"]
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: int | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    overdue_after_minutes: float | None = None


class MapFieldsHealthOut(BaseModel):
    mode: Literal["with_noaa", "standalone", "off"]
    status: MapFieldsStatus
    run_key: str | None = None
    cycle_time: datetime | None = None
    age_hours: float | None = None
    stale_after_hours: float | None = None
    complete_valid_times: int = 0
    last_valid_time: datetime | None = None


class ObservationProviderHealthOut(BaseModel):
    provider: str
    status: Freshness
    active_stations: int = 0
    newest_observed_at: datetime | None = None
    age_hours: float | None = None
    stale_after_hours: float


class ObservationsHealthOut(BaseModel):
    enabled: bool
    providers: list[ObservationProviderHealthOut] = Field(default_factory=list)


class HeartbeatOut(BaseModel):
    """Newest finished background job of any kind (the data-health job excluded)."""

    last_job_finished_at: datetime | None = None
    age_minutes: float | None = None
    limit_minutes: int


class DataHealthOut(BaseModel):
    # Kept first so a plain-text monitor can match '"status":"ok"' at the start.
    status: OverallStatus
    checked_at: datetime
    reasons: list[str] = Field(description="Plain reasons, the most severe first")
    problems: list[HealthProblemOut]
    forecast_sources: list[SourceHealthOut]
    jobs: list[JobHealthOut]
    map_fields: MapFieldsHealthOut
    observations: ObservationsHealthOut
    heartbeat: HeartbeatOut
