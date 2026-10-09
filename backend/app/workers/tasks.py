"""Celery tasks: thin wrappers around the service layer (jobs A–H).

Each job:
* takes a Redis lock so only one instance runs at a time (others log SKIPPED),
* records a ``background_job_logs`` row with status, duration and a result summary,
* triggers the next stage of the pipeline when it succeeds, so new forecast data flows
  through to alerts within minutes, while the beat schedule re-runs every stage
  periodically as a safety net.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from celery import Task
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, utcnow
from app.core.logging import redact
from app.core.redis import LockNotAcquired, redis_lock
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.services.http import TransientProviderError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

NEXT_STAGE = {
    "forecasts.acquire": "quality.predict",
    "quality.predict": "detection.detect",
    "detection.detect": "matching.match",
    "matching.match": "flights.discover",
    "flights.discover": "alerts.generate",
    "alerts.generate": "notifications.deliver",
}


def run_job(
    job_name: str,
    fn: Callable[[Session], Any],
    *,
    task_id: str | None = None,
    lock_ttl: int = 1800,
    chain: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    with SessionLocal() as log_db:
        log = BackgroundJobLog(
            job_name=job_name,
            celery_task_id=task_id,
            status=JobStatus.RUNNING,
            started_at=utcnow(),
            details={},
        )
        log_db.add(log)
        log_db.commit()
        log_id = log.id
    status, error = JobStatus.SUCCESS, None
    details: dict[str, Any] = {}
    try:
        with redis_lock(f"job:{job_name}", ttl_seconds=lock_ttl):
            with SessionLocal() as db:
                result = fn(db)
            details = result if isinstance(result, dict) else {"result": result}
    except LockNotAcquired:
        status, details = JobStatus.SKIPPED, {"reason": "another worker is running this job"}
    except Exception as exc:
        status, error = JobStatus.FAILED, redact(f"{type(exc).__name__}: {exc}")[:2000]
        logger.exception("Job %s failed", job_name)
        raise
    finally:
        with SessionLocal() as log_db:
            row = log_db.get(BackgroundJobLog, log_id)
            if row is not None:
                row.status = status
                row.finished_at = utcnow()
                row.duration_ms = int((time.monotonic() - started) * 1000)
                row.details = _jsonable(details)
                row.error_message = error
                log_db.commit()
    if chain and status == JobStatus.SUCCESS and job_name in NEXT_STAGE:
        # A signature (not send_task) so eager mode in tests runs the chain in-process.
        celery_app.signature(NEXT_STAGE[job_name]).delay()
    return {"status": status.value, **details}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------- jobs A–H
@celery_app.task(
    name="forecasts.acquire",
    bind=True,
    autoretry_for=(TransientProviderError,),
    retry_backoff=60,
    retry_backoff_max=900,
    max_retries=3,
)
def acquire_forecasts(self: Task) -> dict[str, Any]:
    from app.services.forecasts.ingestion import acquire_all

    return run_job(
        "forecasts.acquire",
        lambda db: {"outcomes": [o.as_dict() for o in acquire_all(db)]},
        task_id=self.request.id,
        lock_ttl=3600,
    )


@celery_app.task(name="quality.predict", bind=True)
def predict_quality(self: Task) -> dict[str, Any]:
    from app.services.surf_quality.predict import compute_pending_predictions

    return run_job("quality.predict", compute_pending_predictions, task_id=self.request.id)


@celery_app.task(name="detection.detect", bind=True)
def detect_swells(self: Task) -> dict[str, Any]:
    from app.services.swell_detection.service import detect_events

    return run_job(
        "detection.detect", lambda db: detect_events(db).as_dict(), task_id=self.request.id
    )


@celery_app.task(name="matching.match", bind=True)
def match_searches(self: Task) -> dict[str, Any]:
    from app.services.matching.service import match_all

    return run_job("matching.match", lambda db: match_all(db).as_dict(), task_id=self.request.id)


@celery_app.task(
    name="flights.discover",
    bind=True,
    autoretry_for=(TransientProviderError,),
    retry_backoff=120,
    max_retries=3,
)
def discover_flights_task(self: Task, match_ids: list[str] | None = None) -> dict[str, Any]:
    from app.services.flights.service import discover_flights

    return run_job(
        "flights.discover",
        lambda db: discover_flights(db, match_ids=match_ids).as_dict(),
        task_id=self.request.id,
        lock_ttl=3600,
    )


@celery_app.task(name="alerts.generate", bind=True)
def generate_alerts_task(self: Task) -> dict[str, Any]:
    from app.services.notifications.alerts import generate_alerts

    return run_job(
        "alerts.generate", lambda db: generate_alerts(db).as_dict(), task_id=self.request.id
    )


@celery_app.task(name="notifications.deliver", bind=True)
def deliver_notifications(self: Task) -> dict[str, Any]:
    from app.services.notifications.delivery import deliver_pending

    return run_job(
        "notifications.deliver",
        lambda db: deliver_pending(db).as_dict(),
        task_id=self.request.id,
        lock_ttl=600,
    )


@celery_app.task(name="maintenance.cleanup", bind=True)
def cleanup_task(self: Task) -> dict[str, Any]:
    from app.services.maintenance import cleanup

    return run_job("maintenance.cleanup", cleanup, task_id=self.request.id, chain=False)


@celery_app.task(name="forecasts.check_staleness", bind=True)
def check_staleness(self: Task) -> dict[str, Any]:
    from app.services.forecasts.queries import forecast_status

    def check(db: Session) -> dict[str, Any]:
        status = forecast_status(db)
        stale = [s["code"] for s in status["sources"] if s["stale"]]
        if stale:
            logger.warning("Forecast data is stale for sources: %s", stale)
        return {"stale_sources": stale}

    return run_job("forecasts.check_staleness", check, task_id=self.request.id, chain=False)


# ---------------------------------------------------------------- transactional messages
def deliver_email(to: str, subject: str, text: str, html: str | None, tag: str) -> str | None:
    from app.services.notifications.providers import EmailMessageData, build_email_provider

    result = build_email_provider().send(
        EmailMessageData(to=to, subject=subject, text=text, html=html, tag=tag)
    )
    return result.message_id


def deliver_sms(to: str, body: str) -> str | None:
    from app.services.notifications.providers import build_sms_provider

    return build_sms_provider().send(to, body).message_id


@celery_app.task(
    name="notifications.send_email",
    autoretry_for=(TransientProviderError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def send_email(to: str, subject: str, text: str, html: str | None, tag: str) -> str | None:
    return deliver_email(to, subject, text, html, tag)


@celery_app.task(
    name="notifications.send_sms",
    autoretry_for=(TransientProviderError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def send_sms(to: str, body: str) -> str | None:
    return deliver_sms(to, body)
