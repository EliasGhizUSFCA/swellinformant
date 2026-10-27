"""Internal routes for the platform and its schedulers, not for browsers.

``GET|POST /api/internal/refresh`` is the forecast refresh a scheduler calls (Upstash
QStash every 5 minutes on Vercel Hobby, a Vercel Cron entry on Pro, docs/sync.md) with
``Authorization: Bearer $CRON_SECRET`` (404 without CRON_SECRET, 401 for a wrong token, as
below). It runs ``orchestrator.refresh_check``: the free metadata check of the spot
components and, when one is due, a spot-only sync (never the map/GRIB stage) inside the
Function's time budget, and answers 200 with a JSON summary: ``status`` (``disabled`` with
the FORECAST_REFRESH_ENABLED kill switch, ``not_due``, ``busy`` when another sync holds
the lease, else the sync's status), the plan, the sync run, the version ids published and
the timings. A busy or failed refresh is still a 200 (the scheduler must not retry: the next
check retries on its own, with backoff), an unexpected error a 503. Idempotent: duplicate
deliveries find nothing due or the lease taken.

``GET /api/internal/cron/daily`` is the daily watchdog Vercel Cron calls with
``Authorization: Bearer $CRON_SECRET``:

* 404 (the same body as an unknown route) when CRON_SECRET is not set;
* 401 when the bearer token does not match (constant-time comparison);
* 200 ``{status, problems, alerts_sent, heartbeat_age_minutes, refresh}`` otherwise, with
  ``Cache-Control: private, no-store``.

The watchdog runs the ``ops.data_health`` job (app.services.notifications.ops_alerts: the
data-health report, and an email to the admins when a problem appears, rises, repeats or
clears) through the job runner, so its log row carries the alert state the next run reads
and a problem is not reported twice. It is independent of the sync daemon: it still alerts
when the daemon has stopped (its heartbeat goes stale) or when Redis refuses commands (the
job then runs without its Redis lock, and the report names ``redis_unavailable``). After the
watchdog it also runs one refresh check (the backstop for a scheduler that stopped calling
the refresh route) and adds its summary as ``refresh``. ``status`` is the report's overall
status (ok, degraded, failing), or ``skipped`` when another run of the job held its lock;
``alerts_sent`` lists the alerts (``<reason>:<problem key>``) the admins were emailed;
``heartbeat_age_minutes`` is the age of the daemon's last check (None when there is no
heartbeat in Redis). An unexpected failure answers 503 with status ``error``. When the
failure is the database itself (unreachable, e.g. Neon's compute suspended after its quota),
the admins cannot be read from it, so OPS_ALERT_EMAILS (when set) are emailed instead, at
most once per UTC day (``alerts_sent`` then lists ``new:database_unreachable``). Vercel does
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
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.http_cache import PRIVATE_NO_STORE

logger = logging.getLogger(__name__)

CRON_DAILY_PATH = "/api/internal/cron/daily"
REFRESH_PATH = "/api/internal/refresh"

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
    from app.jobs.runner import LOCK_NOT_TAKEN
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
                    details=jsonable({**details, "lock": LOCK_NOT_TAKEN}),
                    error_message=error,
                )
            )
            log_db.commit()
    return {"status": status.value, **details}


def _redis_reachable() -> bool:
    """One PING: whether the job lock can be taken at all."""
    from app.core.redis import get_redis

    try:
        get_redis().ping()
    except redis.RedisError as exc:
        logger.warning(
            "Watchdog: Redis unavailable (%s); checking without the job lock",
            type(exc).__name__,
        )
        return False
    return True


def run_watchdog_job() -> dict[str, Any]:
    """Run ``ops.data_health`` once; returns run_job's ``{"status": ..., **details}``.

    Redis is asked first: when it does not answer, the check runs without the job lock
    straight away. Letting run_job fail on the lock instead would log a FAILED
    ``ops.data_health`` row that the check itself then reports (and emails) as
    ``job_failed:ops.data_health``, a problem that does not exist; the real one,
    ``redis_unavailable``, is reported by the check. When Redis goes away between the PING
    and the lock, run_job itself runs the check without the lock under JOB_RUNNER=daemon
    (app.jobs.runner.job_lock); under Celery that FAILED row appears before the unlocked
    run.
    """
    from app.jobs.registry import job_spec
    from app.jobs.runner import run_job
    from app.jobs.schedule import DATA_HEALTH_TASK

    spec = job_spec(DATA_HEALTH_TASK)
    if not _redis_reachable():
        return _run_unlocked()
    try:
        return run_job(spec.name, spec.run, lock_ttl=spec.lock_ttl, chain=False)
    except redis.RedisError as exc:  # the job lock needs Redis; the check itself does not
        logger.warning(
            "Watchdog: Redis failed during the job (%s); checking without the job lock",
            type(exc).__name__,
        )
        return _run_unlocked()


def alert_without_database(exc: BaseException) -> list[str]:
    """The database is unreachable: email OPS_ALERT_EMAILS (ops_alerts); the alerts sent."""
    from app.services.notifications.ops_alerts import (
        DB_UNREACHABLE_ALERT,
        alert_database_unreachable,
    )

    try:
        result = alert_database_unreachable(exc)
    except Exception:  # the 503 answer must still go out
        logger.exception("Database alert to OPS_ALERT_EMAILS failed")
        return []
    return [DB_UNREACHABLE_ALERT] if result.get("sent") else []


def heartbeat_age_minutes() -> float | None:
    from app.services.sync.state import read_heartbeat

    heartbeat = read_heartbeat()
    return None if heartbeat is None else round(heartbeat.age_seconds() / 60, 1)


def run_refresh() -> dict[str, Any]:
    """One refresh check (``orchestrator.refresh_check``), JSON-safe."""
    from app.services.sync.orchestrator import refresh_check
    from app.services.sync.types import jsonable

    out = jsonable(refresh_check(trigger="http"))
    return out if isinstance(out, dict) else {"status": "error"}


@router.api_route(REFRESH_PATH, methods=["GET", "POST"])
def refresh(authorization: str | None = Header(default=None)) -> JSONResponse:
    _authorize(authorization)
    try:
        body = run_refresh()
    except Exception as exc:
        logger.exception("Refresh check failed")
        return JSONResponse(
            {"status": "error", "error": f"refresh_failed:{type(exc).__name__}"},
            status_code=503,
            headers={"Cache-Control": PRIVATE_NO_STORE},
        )
    logger.info(
        "Refresh check: %s (sync %s, versions %s)",
        body.get("status"),
        body.get("sync_run_id"),
        body.get("version_ids"),
    )
    return JSONResponse(body, headers={"Cache-Control": PRIVATE_NO_STORE})


def _refresh_backstop() -> dict[str, Any]:
    """The daily cron's refresh check, summarised; never raises (the watchdog's answer
    does not depend on it)."""
    try:
        out = run_refresh()
    except Exception as exc:
        logger.exception("Daily refresh backstop failed")
        return {"status": "error", "error": f"refresh_failed:{type(exc).__name__}"}
    return {
        k: out.get(k)
        for k in ("status", "reason", "sync_run_id", "version_ids", "published_at")
        if k in out
    }


@router.get(CRON_DAILY_PATH)
def cron_daily(authorization: str | None = Header(default=None)) -> JSONResponse:
    _authorize(authorization)
    try:
        result = run_watchdog_job()
    except Exception as exc:
        logger.exception("Daily watchdog failed")
        sent: list[str] = []
        if isinstance(exc, OperationalError | InterfaceError):
            sent = alert_without_database(exc)
        body: dict[str, Any] = {
            "status": "error",
            "problems": [f"watchdog_failed:{type(exc).__name__}"],
            "alerts_sent": sent,
            "heartbeat_age_minutes": heartbeat_age_minutes(),
        }
        return JSONResponse(body, status_code=503, headers={"Cache-Control": PRIVATE_NO_STORE})
    email = result.get("email") or {}
    body = {
        "status": result.get("status"),
        "problems": list(result.get("problems") or []),
        "alerts_sent": list(result.get("alerts") or []) if email.get("sent") else [],
        "heartbeat_age_minutes": heartbeat_age_minutes(),
        "refresh": _refresh_backstop(),
    }
    logger.info(
        "Daily watchdog: %s, %d problem(s), %d alert(s) sent",
        body["status"],
        len(body["problems"]),
        len(body["alerts_sent"]),
    )
    return JSONResponse(body, headers={"Cache-Control": PRIVATE_NO_STORE})
