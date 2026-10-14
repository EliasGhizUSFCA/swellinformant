"""Celery tasks: thin wrappers around the service layer (jobs A–H).

Each job:
* takes a Redis lock so only one instance runs at a time (others log SKIPPED),
* records a ``background_job_logs`` row with status, duration and a result summary,
* triggers the next stage of the pipeline when it succeeds, so new forecast data flows
  through to alerts within minutes, while the beat schedule re-runs every stage
  periodically as a safety net.

Data acquisition jobs (forecasts, ensemble, map fields) record provider failures instead of
raising them, so one provider's outage never stops another's. Their task then decides:
the job is FAILED (and does not chain) when nothing was acquired, and a transient failure
(timeout, 5xx, a full call-budget window, a model run still being published) makes the
task retry itself after a countdown (:func:`retry_transient`), up to ACQUIRE_MAX_RETRIES
times, instead of waiting hours for the next scheduled run.
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
from app.workers.schedule import DATA_HEALTH_TASK, NEXT_STAGE

logger = logging.getLogger(__name__)

__all__ = ["NEXT_STAGE", "run_job"]

# Retries of acquisition tasks after a transient failure: 2, 4, 8 min (capped at 15 min),
# or the provider's own retry time when it gives one (capped at 1 h).
ACQUIRE_MAX_RETRIES = 3
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 900
RETRY_HINT_MAX_SECONDS = 3600

Judge = Callable[[dict[str, Any]], tuple[JobStatus, str | None]]


def run_job(
    job_name: str,
    fn: Callable[[Session], Any],
    *,
    task_id: str | None = None,
    lock_ttl: int = 1800,
    chain: bool = True,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """Run ``fn`` under the job lock and log it.

    ``judge`` turns a result into the job status (and an error message), e.g. FAILED when
    every provider failed; a FAILED job never chains to the next stage.
    """
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
            if judge is not None:
                status, error = judge(details)
                error = redact(error)[:2000] if error else None
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


def judge_acquisition(result: dict[str, Any]) -> tuple[JobStatus, str | None]:
    """forecasts.acquire: FAILED when every configured provider failed."""
    outcomes = result.get("outcomes") or []
    failed = [o for o in outcomes if o.get("status") == "failed"]
    if outcomes and len(failed) == len(outcomes):
        reasons = "; ".join(f"{o.get('source')}: {o.get('message')}" for o in failed)
        return JobStatus.FAILED, f"every forecast provider failed ({reasons})"
    return JobStatus.SUCCESS, None


def judge_single(result: dict[str, Any]) -> tuple[JobStatus, str | None]:
    """One-source jobs (ensemble, map fields): FAILED when the acquisition failed."""
    if result.get("status") == "failed":
        return JobStatus.FAILED, str(result.get("message") or "acquisition failed")
    return JobStatus.SUCCESS, None


def retry_countdown(retries: int, hints: list[float | None]) -> int:
    """Seconds before retry number ``retries + 1``: the provider's hint, else backoff."""
    hinted = [h for h in hints if h]
    if hinted:
        return int(min(RETRY_HINT_MAX_SECONDS, max(60.0, max(hinted))))
    return int(min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2**retries))


def retry_transient(task: Task, result: dict[str, Any]) -> None:
    """Retry ``task`` (raises celery's Retry) when a provider failed transiently.

    Eager mode (tests, single-process development) does not retry: the failure is
    already recorded, and the next scheduled run picks it up.
    """
    outcomes = result.get("outcomes") or [result]
    retryable = [o for o in outcomes if isinstance(o, dict) and o.get("retryable")]
    if not retryable:
        return
    if task.request.is_eager:
        logger.info("%s: transient failure not retried in eager mode", task.name)
        return
    if task.request.retries >= ACQUIRE_MAX_RETRIES:
        logger.warning("%s: transient failure, retries exhausted", task.name)
        return
    countdown = retry_countdown(task.request.retries, [o.get("retry_after_s") for o in retryable])
    logger.info("%s: transient failure, retrying in %d s", task.name, countdown)
    raise task.retry(countdown=countdown, max_retries=ACQUIRE_MAX_RETRIES)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------- jobs A–H
@celery_app.task(name="forecasts.acquire", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_forecasts(self: Task) -> dict[str, Any]:
    from app.services.forecasts.ingestion import acquire_all

    result = run_job(
        "forecasts.acquire",
        lambda db: {"outcomes": [o.as_dict() for o in acquire_all(db)]},
        task_id=self.request.id,
        lock_ttl=3600,
        judge=judge_acquisition,
    )
    retry_transient(self, result)
    return result


@celery_app.task(name="forecasts.blend", bind=True)
def blend_forecasts(self: Task) -> dict[str, Any]:
    """Combine the newest run of each independent model family (FORECAST_BLEND_ENABLED)."""
    from app.services.forecasts.blend import build_blend

    return run_job("forecasts.blend", lambda db: build_blend(db).as_dict(), task_id=self.request.id)


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


@celery_app.task(name="forecasts.acquire_ensemble", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_ensemble_task(self: Task) -> dict[str, Any]:
    """GEFS-Wave ensemble mean/spread for forecast uncertainty (GEFS_ENABLED)."""
    from app.services.forecasts.ensemble import acquire_ensemble

    result = run_job(
        "forecasts.acquire_ensemble",
        acquire_ensemble,
        task_id=self.request.id,
        lock_ttl=3600,
        judge=judge_single,
    )
    retry_transient(self, result)
    return result


@celery_app.task(name="forecasts.map_fields", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_map_fields_task(self: Task) -> dict[str, Any]:
    """Global map fields on their own (standalone mode only; otherwise a no-op).

    In "with_noaa" mode the NOAA acquisition stores the fields itself; with "off" nothing
    is fetched (see app.services.map_fields.map_fields_mode).
    """
    from app.core.config import get_settings
    from app.services.forecasts.ingestion import acquire_map_fields
    from app.services.map_fields import map_fields_mode

    def job(db: Session) -> dict[str, Any]:
        mode = map_fields_mode(get_settings())
        if mode != "standalone":
            return {"skipped": f"map fields mode is {mode!r}, not 'standalone'"}
        return acquire_map_fields(db)

    result = run_job(
        "forecasts.map_fields",
        job,
        task_id=self.request.id,
        lock_ttl=3600,
        chain=False,
        judge=judge_single,
    )
    retry_transient(self, result)
    return result


@celery_app.task(name="observations.ingest", bind=True)
def ingest_observations(self: Task) -> dict[str, Any]:
    """Buoy observations for verification and training (OBSERVATIONS_ENABLED)."""
    from app.services.observations.ingest import ingest_all

    return run_job("observations.ingest", ingest_all, task_id=self.request.id, lock_ttl=1800)


@celery_app.task(name="tides.acquire", bind=True)
def acquire_tides_task(self: Task) -> dict[str, Any]:
    """Daily tide predictions for every spot's tide station (TIDE_PROVIDERS)."""
    from app.services.tides.ingest import acquire_tides

    return run_job("tides.acquire", acquire_tides, task_id=self.request.id, lock_ttl=1800)


@celery_app.task(name="verification.collect_point_forecasts", bind=True)
def collect_point_forecasts(self: Task) -> dict[str, Any]:
    """Archive model forecasts at observation stations (VERIFICATION_ENABLED)."""
    from app.services.verification.point_forecasts import collect_recent

    return run_job("verification.collect_point_forecasts", collect_recent, task_id=self.request.id)


@celery_app.task(name="verification.run", bind=True)
def run_verification_task(self: Task) -> dict[str, Any]:
    """Pair forecasts with observations and refresh the accuracy metrics."""
    from app.services.verification.jobs import run_verification

    return run_job("verification.run", run_verification, task_id=self.request.id, lock_ttl=3600)


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


@celery_app.task(name=DATA_HEALTH_TASK, bind=True)
def data_health_task(self: Task) -> dict[str, Any]:
    """Data-health check; emails admin users when it degrades and when it recovers."""
    from app.services.notifications.ops_alerts import run_data_health_check

    return run_job(
        DATA_HEALTH_TASK, run_data_health_check, task_id=self.request.id, lock_ttl=600, chain=False
    )


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


ACCOUNT_EMAIL_MAX_RETRIES = 5


@celery_app.task(
    name="notifications.send_account_email", bind=True, max_retries=ACCOUNT_EMAIL_MAX_RETRIES
)
def send_account_email(self: Task, message_id: str, text: str, html: str | None) -> str | None:
    """Deliver a recorded account email; transient failures retry with backoff, and the
    record ends FAILED (with the provider's reason) once retries are exhausted."""
    from app.services.notifications.transactional import deliver_account_email

    if self.request.is_eager:
        # Eager mode (tests, single-process dev): Celery's retry() would re-raise into the
        # caller (e.g. the registration request), so retry in-process without waiting.
        for attempt in range(ACCOUNT_EMAIL_MAX_RETRIES + 1):
            try:
                status = deliver_account_email(
                    message_id, text, html, final_attempt=attempt == ACCOUNT_EMAIL_MAX_RETRIES
                )
            except TransientProviderError:
                continue
            return status.value if status else None
        return None
    final = self.request.retries >= ACCOUNT_EMAIL_MAX_RETRIES
    try:
        status = deliver_account_email(message_id, text, html, final_attempt=final)
    except TransientProviderError as exc:
        raise self.retry(exc=exc, countdown=min(600, 30 * 2**self.request.retries)) from exc
    return status.value if status else None


# Legacy task kept so messages queued by older versions are still delivered.
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
