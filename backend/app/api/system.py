"""Health checks and a non-sensitive system status overview."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import check_database, get_db
from app.core.ratelimit import rate_limit
from app.core.redis import check_redis
from app.models import BackgroundJobLog
from app.schemas.health import DataHealthOut
from app.services.forecasts.queries import forecast_status
from app.services.health import data_health
from app.services.spot_views import event_counts

router = APIRouter(prefix="/api", tags=["system"])

JOB_NAMES = [
    "forecasts.acquire",
    "forecasts.blend",
    "forecasts.acquire_ensemble",
    "quality.predict",
    "detection.detect",
    "matching.match",
    "flights.discover",
    "alerts.generate",
    "notifications.deliver",
    "observations.ingest",
    "verification.collect_point_forecasts",
    "verification.run",
    "maintenance.cleanup",
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
    """
    report = data_health(db)
    response.headers["Cache-Control"] = "no-store"
    if strict and report.status == "failing":
        response.status_code = 503
    return report.out


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    jobs = []
    for name in JOB_NAMES:
        row = db.scalar(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == name)
            .order_by(BackgroundJobLog.started_at.desc())
            .limit(1)
        )
        jobs.append(
            {
                "job": name,
                "status": row.status.value if row else "never_run",
                "started_at": row.started_at.isoformat() if row else None,
                "duration_ms": row.duration_ms if row else None,
            }
        )
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
        "forecast": forecast_status(db),
        "events": event_counts(db),
        "jobs": jobs,
    }
