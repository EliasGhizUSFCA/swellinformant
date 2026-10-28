"""Admin freshness dashboard (GET /api/admin/freshness, design D12, contract C5).

Admin accounts only: unlike the public data-health report (app.schemas.health) these
payloads carry redacted error text (failed refreshes, provider failures), quota counters
and per-spot failure reasons. Built by app.services.admin_freshness. Times are UTC;
durations are in the unit their name says.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.health import OverallStatus, Severity

Component = Literal["atmosphere", "wave"]


class AdminProviderHealthOut(BaseModel):
    """A provider's recorded health (the provider manager, when it records any)."""

    provider: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_kind: str | None = Field(
        None, description="credentials, testing_key, quota or unavailable"
    )
    last_failure_detail: str | None = None
    consecutive_unavailable: int = 0
    blocked_until: datetime | None = None
    blocking: bool = False


class AdminActiveProviderOut(BaseModel):
    primary: str = Field(description="The provider a refresh builds now")
    providers: list[str]
    configured: list[str] = Field(description="What the settings select")
    source: str = Field(description="FORECAST_PROVIDERS (advanced override) or WEATHER_PROVIDER")
    weather_provider: str
    windy_eligible: bool
    fallback: str | None = Field(None, description="The configured runtime fallback")
    fallback_active: bool = False
    fallback_from: str | None = Field(None, description="The provider the fallback stands in for")
    fallback_until: datetime | None = None
    reasons: list[str] = []
    health: list[AdminProviderHealthOut] = []


class AdminDomainOut(BaseModel):
    model: str
    init_time: datetime | None = None
    available_at: datetime | None = None


class AdminComponentFreshnessOut(BaseModel):
    """The model run the published version serves for one component, and when the next
    one is expected."""

    component: Component
    model: str | None = None
    run_key: str | None = None
    run: datetime | None = Field(None, description="Init time of the served run")
    estimated: bool = Field(False, description="The run time is an estimate (Windy)")
    available_at: datetime | None = Field(None, description="Upstream availability of that run")
    last_detected_at: datetime | None = None
    last_acquired_at: datetime | None = Field(
        None, description="When the served run was fetched (a carried component keeps it)"
    )
    changed: bool | None = Field(
        None, description="Fetched by the newest refresh (false: carried forward)"
    )
    carried_from_run_id: int | None = None
    newest_run: datetime | None = Field(
        None, description="The newest init time known (published, staged or detected)"
    )
    update_interval_minutes: float
    update_interval_recorded: bool = Field(
        description="From Open-Meteo meta.json update_interval_seconds (else 6 h assumed)"
    )
    typical_lag_minutes: float | None = Field(
        None, description="Median availability lag after the cycle, measured on recent runs"
    )
    lag_samples: int = 0
    expected_next_at: datetime | None = None
    late_after: datetime | None = None
    late: bool = False
    domains: list[AdminDomainOut] = []


class AdminEventOut(BaseModel):
    at: datetime | None = None
    detail: str | None = None
    version_id: int | None = None


class AdminVersionCountsOut(BaseModel):
    total: int
    updated: int
    stale: int
    failed: int


class AdminPublishedVersionOut(BaseModel):
    id: int
    published_at: datetime | None = None
    age_minutes: float | None = None
    provider: str | None = None
    source_code: str | None = None
    fallback_from: str | None = None
    wind_model: str | None = None
    wind_model_run: datetime | None = None
    wind_run_estimated: bool = False
    wave_model: str | None = None
    wave_model_run: datetime | None = None
    wave_run_estimated: bool = False
    counts: AdminVersionCountsOut


class AdminStaleSpotOut(BaseModel):
    spot_id: int
    slug: str | None = None
    name: str | None = None
    last_updated_at: datetime | None = None
    reason: str | None = Field(
        None, description="missing, refresh_failed, atmosphere_age, wave_age or age"
    )
    error: str | None = Field(None, description="The failed refresh's redacted error")


class AdminFailedRequestOut(BaseModel):
    provider: str = Field(description="Forecast source code or flight provider")
    status: str = Field(description="HTTP status, or 'failed' for a failed flight search")
    count: int


class AdminProviderRequestsOut(BaseModel):
    source: str
    provider: str | None = None
    runs: int
    failed_runs: int
    requests: int
    weighted_calls: float | None = None
    bytes_downloaded: int | None = None
    http_status_counts: dict[str, int] = {}
    failed_requests: int


class AdminUsageWindowOut(BaseModel):
    used: float | None = Field(None, description="None: the counter could not be read")
    limit: float | None = None
    fraction: float | None = None
    resets_at: datetime | None = None


class AdminOpenMeteoUsageOut(BaseModel):
    minute: AdminUsageWindowOut
    hour: AdminUsageWindowOut
    day: AdminUsageWindowOut
    month: AdminUsageWindowOut
    available: bool = Field(description="False: Redis could not be read")


class AdminWindyUsageOut(BaseModel):
    day: AdminUsageWindowOut
    available: bool
    counted: bool = Field(description="False: this build does not count Windy requests")


class AdminFlightUsageOut(BaseModel):
    provider: str
    today: int = Field(description="Provider searches stored today (UTC)")
    per_provider: dict[str, int] = {}
    failed_today: int = 0
    quota: AdminUsageWindowOut | None = Field(
        None, description="FLIGHT_DAILY_QUOTA counter (None: the demo provider has no quota)"
    )
    user_daily_quota: int


class AdminEmailUsageOut(BaseModel):
    provider: str
    today: int
    account: int
    alerts: int
    failed: int


class AdminApiUsageOut(BaseModel):
    checked_at: datetime
    alert_fraction: float = Field(description="QUOTA_ALERT_FRACTION")
    open_meteo: AdminOpenMeteoUsageOut
    windy: AdminWindyUsageOut
    flights: AdminFlightUsageOut
    emails: AdminEmailUsageOut
    requests_24h: list[AdminProviderRequestsOut] = []


class AdminDurationStatsOut(BaseModel):
    samples: int = 0
    last: float | None = None
    avg: float | None = None
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


class AdminStageTimeOut(BaseModel):
    stage: str
    runs: int
    avg_ms: float
    p95_ms: float
    max_ms: float
    avg_cpu_s: float | None = None


class AdminProcessingTimeOut(BaseModel):
    refreshes: int = Field(description="Finished refresh syncs in the sample (newest 20 syncs)")
    total_ms: AdminDurationStatsOut
    stages: list[AdminStageTimeOut] = []
    detected_to_acquired_s: AdminDurationStatsOut
    acquired_to_scored_s: AdminDurationStatsOut
    scored_to_published_s: AdminDurationStatsOut


class AdminLatencyOut(BaseModel):
    upstream_to_publication_s: AdminDurationStatsOut = Field(
        description="upstream_available_at -> published_at of the recent refresh versions"
    )
    publication_to_frontend_s: AdminDurationStatsOut = Field(
        description="Browser 'seen' reports after a version change (newest 200 in Redis)"
    )
    first_served_s: AdminDurationStatsOut = Field(
        description="published_at -> first served by the version endpoint (newest versions)"
    )


class AdminStageOut(BaseModel):
    name: str
    status: str
    duration_ms: int
    cpu_s: float | None = None


class AdminRefreshJobOut(BaseModel):
    id: int
    trigger: str
    status: str
    refresh: bool = Field(description="A spot forecast refresh (false: map or ensemble only)")
    kinds: list[str] = []
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    published_at: datetime | None = None
    stages: list[AdminStageOut] = []
    version_ids: list[int] = []
    error: str | None = Field(None, description="Redacted, shortened")


class AdminVersionRowOut(BaseModel):
    id: int
    status: str
    kind: str | None = None
    provider: str | None = None
    fallback_from: str | None = None
    detected_at: datetime | None = None
    upstream_available_at: datetime | None = None
    staged_at: datetime | None = None
    published_at: datetime | None = None
    wind_model_run: datetime | None = None
    wave_model_run: datetime | None = None
    changed: list[Component] = []
    spots_updated: int
    spots_stale: int
    spots_failed: int
    upstream_to_publication_s: float | None = None


class AdminSchedulerCheckOut(BaseModel):
    source: str
    at: datetime | None = None


class AdminSchedulerOut(BaseModel):
    expected_minutes: int = Field(description="REFRESH_SCHEDULER_EXPECTED_MINUTES (0: off)")
    enabled: bool
    refresh_enabled: bool = Field(description="FORECAST_REFRESH_ENABLED")
    last_check_at: datetime | None = None
    last_check_by: str | None = None
    checks: list[AdminSchedulerCheckOut] = []
    silent: bool


class AdminProblemOut(BaseModel):
    key: str
    severity: Severity
    message: str
    detail: str | None = Field(None, description="Redacted error text (admins only)")


class AdminFreshnessOut(BaseModel):
    checked_at: datetime
    status: OverallStatus = Field(description="Over the admin and the public problems")
    version_id: int | None = Field(None, description="The published forecast version")
    active_provider: AdminActiveProviderOut
    atmosphere: AdminComponentFreshnessOut
    wave: AdminComponentFreshnessOut
    last_detected_model_update: AdminEventOut
    last_successful_acquisition: AdminEventOut
    last_successful_scoring: AdminEventOut
    last_published_version: AdminPublishedVersionOut | None = None
    spots_updated: int | None = None
    stale_spots: list[AdminStaleSpotOut] = []
    failed_requests: list[AdminFailedRequestOut] = Field(
        [], description="Last 24 h, by provider and status"
    )
    api_usage: AdminApiUsageOut
    processing_time: AdminProcessingTimeOut
    latency: AdminLatencyOut
    refresh_jobs: list[AdminRefreshJobOut] = []
    versions: list[AdminVersionRowOut] = []
    scheduler: AdminSchedulerOut
    problems: list[AdminProblemOut] = Field([], description="The admin-only problems (D12)")
    health_problems: list[AdminProblemOut] = Field(
        [], description="The public data-health report's problems"
    )
    notes: list[str] = []
