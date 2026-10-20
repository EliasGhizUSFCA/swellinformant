"""Internal routes for the platform, not for browsers (foundation contract F8).

``GET /api/internal/cron/daily`` is the daily watchdog Vercel Cron calls with
``Authorization: Bearer $CRON_SECRET``:

* 404 (the same body as an unknown route) when CRON_SECRET is not set;
* 401 when the bearer token does not match (constant-time comparison);
* 200 ``{status, problems, alerts_sent, heartbeat_age_minutes}`` otherwise, with
  ``Cache-Control: private, no-store``.

The watchdog runs the ``ops.data_health`` job (app.services.notifications.ops_alerts:
the data-health report, and an email to the admins when a problem appears, rises, repeats
or clears) through the job runner, so its log row carries the alert state the next run
reads and a problem is not reported twice. It is independent of the sync daemon: it still
alerts when the daemon has stopped (its heartbeat goes stale) or when Redis refuses
commands (the job then runs without its Redis lock). ``status`` is the report's overall
status (ok, degraded, failing), or ``skipped`` when another run of the job held its lock;
``alerts_sent`` lists the alerts (``<reason>:<problem key>``) the admins were emailed;
``heartbeat_age_minutes`` is the age of the daemon's last check (None when there is no
heartbeat in Redis). An unexpected failure answers 503 with status ``error``. Vercel does
not retry a failed cron and may deliver one twice; both are safe here.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Any

import redis
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.http_cache import PRIVATE_NO_STORE

logger = logging.getLogger(__name__)

CRON_DAILY_PATH = "/api/internal/cron/daily"

router = APIRouter(tags=["internal"], include_in_schema=False)


def _authorize(authorization: str | None) -> None:
    secret = get_settings().cron_secret
    if not secret:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.strip().encode(), secret.encode()
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _run_unlocked() -> dict[str, Any]:
    """The data-health job without its Redis lock (Redis unavailable), logged like
    app.jobs.runner.run_job logs it."""
    from app.jobs.registry import job_spec
    from app.jobs.schedule import DATA_HEALTH_TASK
    from app.models import BackgroundJobLog
    from app.models.enums import JobStatus
    from app.services.sync.types import jsonable

    spec = job_spec(DATA_HEALTH_TASK)
    started_at, started = utcnow(), time.monotonic()
    status, error, details = JobStatus.SUCCESS, None, {}
    try:
        with SessionLocal() as db:
            details = spec.run(db)
    except Exception as exc:
        status, error = JobStatus.FAILED, f"{type(exc).__name__} (run without the Redis lock)"
        raise
    finally:
        with SessionLocal() as log_db:
            log_db.add(
                BackgroundJobLog(
                    job_name=spec.name,
                    status=status,
                    started_at=started_at,
                    finished_at=utcnow(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details=jsonable({**details, "lock": "not taken: Redis unavailable"}),
                    error_message=error,
                )
            )
            log_db.commit()
    return {"status": status.value, **details}


def run_watchdog_job() -> dict[str, Any]:
    """Run ``ops.data_health`` once; returns run_job's ``{"status": ..., **details}``."""
    from app.jobs.registry import job_spec
    from app.jobs.runner import run_job
    from app.jobs.schedule import DATA_HEALTH_TASK

    spec = job_spec(DATA_HEALTH_TASK)
    try:
        return run_job(spec.name, spec.run, lock_ttl=spec.lock_ttl, chain=False)
    except redis.RedisError as exc:  # the job lock needs Redis; the check itself does not
        logger.warning(
            "Watchdog: Redis unavailable (%s); checking without the job lock",
            type(exc).__name__,
        )
        return _run_unlocked()


def heartbeat_age_minutes() -> float | None:
    from app.services.sync.state import read_heartbeat

    heartbeat = read_heartbeat()
    return None if heartbeat is None else round(heartbeat.age_seconds() / 60, 1)


@router.get(CRON_DAILY_PATH)
def cron_daily(authorization: str | None = Header(default=None)) -> JSONResponse:
    _authorize(authorization)
    try:
        result = run_watchdog_job()
    except Exception as exc:
        logger.exception("Daily watchdog failed")
        body: dict[str, Any] = {
            "status": "error",
            "problems": [f"watchdog_failed:{type(exc).__name__}"],
            "alerts_sent": [],
            "heartbeat_age_minutes": heartbeat_age_minutes(),
        }
        return JSONResponse(body, status_code=503, headers={"Cache-Control": PRIVATE_NO_STORE})
    email = result.get("email") or {}
    body = {
        "status": result.get("status"),
        "problems": list(result.get("problems") or []),
        "alerts_sent": list(result.get("alerts") or []) if email.get("sent") else [],
        "heartbeat_age_minutes": heartbeat_age_minutes(),
    }
    logger.info(
        "Daily watchdog: %s, %d problem(s), %d alert(s) sent",
        body["status"],
        len(body["problems"]),
        len(body["alerts_sent"]),
    )
    return JSONResponse(body, headers={"Cache-Control": PRIVATE_NO_STORE})
