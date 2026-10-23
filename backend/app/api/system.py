"""Health checks and a non-sensitive system status overview."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.api.spots import public_json
from app.core.config import get_settings
from app.core.database import check_database, get_db
from app.core.http_cache import cache_headers
from app.core.ratelimit import rate_limit
from app.core.redis import check_redis
from app.models import BackgroundJobLog
from app.schemas.health import DataHealthOut
from app.services import public_cache as snapshots
from app.services.forecasts.queries import forecast_status
from app.services.health import data_health
from app.services.spot_views import event_counts

router = APIRouter(prefix="/api", tags=["system"])

# GET /api/health/data without ``strict``: the same report for every visitor (no user
# dependency, no cookie), shared by the CDN for a minute, so several status pages and
# monitors cost one report (about 4 Redis commands and a handful of database reads) a
# minute between them. Browsers always ask again (max-age=0), so a page sees at most a
# minute-old report. ``strict`` (uptime monitors that want the 503) stays no-store.
DATA_HEALTH_CDN_SECONDS = 60

# Every job the worker or the sync daemon runs: the schedule's tasks, the sync itself
# (sync.run, the log row of one whole sync under every runner), the stages a sync runs
# (app.services.sync.types.STAGE_JOB_NAMES), and forecasts.blend, which also runs chained
# after an acquisition. tests/integration/test_public_and_opportunity_api.py checks that the
# schedule has no task missing here, tests/unit/test_job_schedule_floor.py the stages.
JOB_NAMES = [
    "sync.tick",
    "sync.run",
    "forecasts.acquire",
    "forecasts.blend",
    "forecasts.acquire_ensemble",
    "forecasts.map_fields",
    "forecasts.check_staleness",
    "tides.acquire",
    "quality.predict",
    "sync.publish",
    "detection.detect",
    "matching.match",
    "flights.discover",
    "alerts.generate",
    "notifications.deliver",
    "observations.ingest",
    "verification.collect_point_forecasts",
    "verification.run",
    "maintenance.cleanup",
    "maintenance.prune_pass",
    "ops.data_health",
]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def ready() -> JSONResponse:
    checks: dict[str, str] = {}
    for name, fn in (("database", check_database), ("redis", check_redis)):
        try:
            fn()
            checks[name] = "ok"
        except Exception:
            checks[name] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
    )


@router.get(
    "/health/data",
    response_model=DataHealthOut,
    dependencies=[Depends(rate_limit("health-data", 60, 60))],
)
def data_health_report(
    response: Response,
    strict: bool = Query(
        False, description="Answer 503 instead of 200 when the status is 'failing'"
    ),
    db: Session = Depends(get_db),
) -> DataHealthOut:
    """Data freshness and background-job health (ok / degraded / failing, with reasons).

    Public on purpose, like /api/system/status: it holds operational metadata only (run
    keys, ages, limits, job statuses, reasons written by the service; no personal data,
    no raw error text), and an external uptime monitor must be able to read it without
    credentials, because a dead worker cannot send its own alert. Rate limited per IP.
    Shared by the CDN for DATA_HEALTH_CDN_SECONDS; with ``strict`` never cached.
    """
    report = data_health(db)
    if strict:
        response.headers["Cache-Control"] = "no-store"
        if report.status == "failing":
            response.status_code = 503
    else:
        cache_headers(response, browser_seconds=0, cdn_seconds=DATA_HEALTH_CDN_SECONDS)
    return report.out


def _latest_jobs(db: Session) -> list[dict[str, Any]]:
    """The newest log of every job in JOB_NAMES (``never_run`` without one), in one
    statement over the four columns shown (the newest-inserted of equal start times)."""
    log = BackgroundJobLog
    latest = {
        row.job_name: row
        for row in db.execute(
            select(log.job_name, log.status, log.started_at, log.duration_ms)
            .where(log.job_name.in_(JOB_NAMES))
            .order_by(log.job_name, log.started_at.desc(), log.id.desc())
            .ext(distinct_on(log.job_name))
        )
    }
    jobs = []
    for name in JOB_NAMES:
        row = latest.get(name)
        jobs.append(
            {
                "job": name,
                "status": row.status.value if row else "never_run",
                "started_at": row.started_at.isoformat() if row else None,
                "duration_ms": row.duration_ms if row else None,
            }
        )
    return jobs


SYSTEM_STATUS = TypeAdapter(dict[str, Any])


# Every page's shell asks for this report (the demo ribbon and the forecast freshness), so it
# is the same for every visitor (no user dependency, no cookie) and served like the other
# public GETs: from the public snapshot cache (app.services.public_cache: computed at most
# once per publication and UTC hour, for the start of the hour; the job list is therefore
# that of the hour's first request, at most an hour old, and new after each sync) with the
# public browser and CDN windows. Before, every page view read about 22.7 kB from the
# database and kept Neon's compute awake (docs/map-layers.md, "Free database budget").
@router.get("/system/status", response_model=dict[str, Any])
def system_status(db: Session = Depends(get_db)) -> Response:
    return public_json(
        snapshots.snapshot(
            db,
            "system-status",
            "",
            lambda now: snapshots.render(SYSTEM_STATUS, _system_status(db, now)),
        )
    )


def _system_status(db: Session, now: datetime) -> dict[str, Any]:
    s = get_settings()
    jobs = _latest_jobs(db)
    return {
        "mode": {
            "demo_mode": s.demo_mode,
            "forecast_providers": s.forecast_provider_list,
            "flight_provider": s.flight_provider,
            "email_provider": s.email_provider,
            "email_delivery": "capture" if s.email_capture_only else "live",
            "dev_inbox_url": s.dev_inbox_url if s.email_provider == "mailpit" else None,
            "captured_email_location": s.captured_email_location,
            "sms_provider": s.sms_provider,
            "dev_tools": s.enable_dev_endpoints,
        },
        "detection": {
            "lead_min_days": s.detection_lead_min_days,
            "lead_max_days": s.detection_lead_max_days,
            "event_min_score": s.swell_event_min_score,
        },
        "forecast": forecast_status(db, now),
        "events": event_counts(db),
        "jobs": jobs,
    }
