"""Health checks and a non-sensitive system status overview."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import check_database, get_db
from app.core.redis import check_redis
from app.models import BackgroundJobLog
from app.services.forecasts.queries import forecast_status
from app.services.spot_views import event_counts

router = APIRouter(prefix="/api", tags=["system"])

JOB_NAMES = [
    "forecasts.acquire",
    "quality.predict",
    "detection.detect",
    "matching.match",
    "flights.discover",
    "alerts.generate",
    "notifications.deliver",
    "maintenance.cleanup",
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
