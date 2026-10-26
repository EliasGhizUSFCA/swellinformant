"""Celery tasks: thin wrappers around the job registry (app.jobs.registry).

Each job runs through :func:`app.jobs.runner.run_job` (the job lock, the
``background_job_logs`` row, the judge); a success queues the next pipeline stage here, as
a Celery signature (NEXT_STAGE).

Forecast data is synced (design D14): beat runs ``sync.tick`` every SYNC_CHECK_MINUTES,
which reads the providers' run metadata and, when a new model run is out, runs one sync
(app.services.sync.orchestrator: spots, ensemble, map layers, blend, predictions, publish,
detection, matching, flights, alerts, delivery) inside this one task, with its own time
limit. The acquisition tasks below are no longer scheduled; they stay callable by hand
(``forecasts.acquire`` still chains through every stage).

Data acquisition jobs (forecasts, ensemble, map fields) record provider failures instead of
raising them, so one provider's outage never stops another's. Their task then decides:
the job is FAILED (and does not chain) when nothing was acquired, and a transient failure
(timeout, 5xx, a full call-budget window, a model run still being published) makes the
task retry itself after a countdown (:func:`retry_transient`), up to ACQUIRE_MAX_RETRIES
times.

Messaging tasks (account emails, SMS) ignore their result: the caller never reads it, and
without a result the API's ``.delay()`` does not subscribe to the result backend: with
Redis refusing connections it raised a RuntimeError after 19 s before, and raises kombu's
OperationalError (which app.workers.dispatch handles) in 0.7 s now (measured 2026-10-21).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from celery import Task
from sqlalchemy.orm import Session

from app.jobs import runner
from app.jobs.registry import JobSpec, discover_flights_run, job_spec
from app.jobs.runner import ACQUIRE_MAX_RETRIES, Judge, retry_countdown
from app.jobs.schedule import (
    DATA_HEALTH_TASK,
    SYNC_TICK_SOFT_TIME_LIMIT_SECONDS,
    SYNC_TICK_TIME_LIMIT_SECONDS,
)
from app.services.http import TransientProviderError
from app.services.notifications.transactional import deliver_sms
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def queue_next_stage(stage: str) -> None:
    """Queue the next pipeline stage. A signature (not send_task) so eager mode in tests
    runs the chain in-process."""
    celery_app.signature(stage).delay()


def run_job(
    job_name: str,
    fn: Callable[[Session], Any],
    *,
    task_id: str | None = None,
    lock_ttl: int = 1800,
    chain: bool = True,
    judge: Judge | None = None,
) -> dict[str, Any]:
    """:func:`app.jobs.runner.run_job` with Celery chaining: when the job succeeds and
    ``chain`` is true, its NEXT_STAGE is queued (:func:`queue_next_stage`)."""
    return runner.run_job(
        job_name,
        fn,
        task_id=task_id,
        lock_ttl=lock_ttl,
        chain=chain,
        judge=judge,
        on_success=queue_next_stage,
    )


def run_spec(spec: JobSpec, task: Task) -> dict[str, Any]:
    """Run a registry job for a Celery task (its id goes into the job log)."""
    return run_job(
        spec.name,
        spec.run,
        task_id=task.request.id,
        lock_ttl=spec.lock_ttl,
        chain=spec.chain,
        judge=spec.judge,
    )


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


# ---------------------------------------------------------------- jobs A–H
@celery_app.task(name="forecasts.acquire", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_forecasts(self: Task) -> dict[str, Any]:
    result = run_spec(job_spec("forecasts.acquire"), self)
    retry_transient(self, result)
    return result


@celery_app.task(name="forecasts.blend", bind=True)
def blend_forecasts(self: Task) -> dict[str, Any]:
    """Combine the newest run of each independent model family (FORECAST_BLEND_ENABLED)."""
    return run_spec(job_spec("forecasts.blend"), self)


@celery_app.task(name="quality.predict", bind=True)
def predict_quality(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("quality.predict"), self)


@celery_app.task(name="detection.detect", bind=True)
def detect_swells(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("detection.detect"), self)


@celery_app.task(name="matching.match", bind=True)
def match_searches(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("matching.match"), self)


@celery_app.task(
    name="flights.discover",
    bind=True,
    autoretry_for=(TransientProviderError,),
    retry_backoff=120,
    max_retries=3,
)
def discover_flights_task(self: Task, match_ids: list[str] | None = None) -> dict[str, Any]:
    spec = replace(job_spec("flights.discover"), run=discover_flights_run(match_ids))
    return run_spec(spec, self)


@celery_app.task(name="alerts.generate", bind=True)
def generate_alerts_task(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("alerts.generate"), self)


@celery_app.task(name="notifications.deliver", bind=True)
def deliver_notifications(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("notifications.deliver"), self)


@celery_app.task(name="maintenance.cleanup", bind=True)
def cleanup_task(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("maintenance.cleanup"), self)


@celery_app.task(name="forecasts.acquire_ensemble", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_ensemble_task(self: Task) -> dict[str, Any]:
    """GEFS-Wave ensemble mean/spread for forecast uncertainty (GEFS_ENABLED)."""
    result = run_spec(job_spec("forecasts.acquire_ensemble"), self)
    retry_transient(self, result)
    return result


@celery_app.task(name="forecasts.map_fields", bind=True, max_retries=ACQUIRE_MAX_RETRIES)
def acquire_map_fields_task(self: Task) -> dict[str, Any]:
    """Global map fields on their own (standalone mode only; otherwise a no-op).

    In "with_noaa" mode the NOAA acquisition stores the fields itself; with "off" nothing
    is fetched (see app.services.map_fields.map_fields_mode).
    """
    result = run_spec(job_spec("forecasts.map_fields"), self)
    retry_transient(self, result)
    return result


@celery_app.task(name="observations.ingest", bind=True)
def ingest_observations(self: Task) -> dict[str, Any]:
    """Buoy observations for verification and training (OBSERVATIONS_ENABLED)."""
    return run_spec(job_spec("observations.ingest"), self)


@celery_app.task(name="tides.acquire", bind=True)
def acquire_tides_task(self: Task) -> dict[str, Any]:
    """Daily tide predictions for every spot's tide station (TIDE_PROVIDERS)."""
    return run_spec(job_spec("tides.acquire"), self)


@celery_app.task(name="verification.collect_point_forecasts", bind=True)
def collect_point_forecasts(self: Task) -> dict[str, Any]:
    """Archive model forecasts at observation stations (VERIFICATION_ENABLED)."""
    return run_spec(job_spec("verification.collect_point_forecasts"), self)


@celery_app.task(name="verification.run", bind=True)
def run_verification_task(self: Task) -> dict[str, Any]:
    """Pair forecasts with observations and refresh the accuracy metrics."""
    return run_spec(job_spec("verification.run"), self)


@celery_app.task(name="forecasts.check_staleness", bind=True)
def check_staleness(self: Task) -> dict[str, Any]:
    return run_spec(job_spec("forecasts.check_staleness"), self)


@celery_app.task(name=DATA_HEALTH_TASK, bind=True)
def data_health_task(self: Task) -> dict[str, Any]:
    """Data-health check; emails admin users when it degrades and when it recovers."""
    return run_spec(job_spec(DATA_HEALTH_TASK), self)


# ---------------------------------------------------------------- the synced pipeline
@celery_app.task(
    name="sync.tick",
    bind=True,
    time_limit=SYNC_TICK_TIME_LIMIT_SECONDS,
    soft_time_limit=SYNC_TICK_SOFT_TIME_LIMIT_SECONDS,
)
def sync_tick(self: Task) -> dict[str, Any]:
    """Beat's sync check: plan from the providers' run metadata and the database, and run
    one sync when a new model run is out (app.services.sync.orchestrator.tick, trigger
    "celery"). The job lock (1 h) keeps a second check from starting while a sync runs;
    the sync's own lease (sync_runs) keeps it from overlapping any other runner."""
    return run_spec(job_spec("sync.tick"), self)


# ---------------------------------------------------------------- transactional messages
ACCOUNT_EMAIL_MAX_RETRIES = 5


@celery_app.task(
    name="notifications.send_account_email",
    bind=True,
    max_retries=ACCOUNT_EMAIL_MAX_RETRIES,
    ignore_result=True,
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


@celery_app.task(
    name="notifications.send_sms",
    ignore_result=True,
    autoretry_for=(TransientProviderError,),
    retry_backoff=30,
    retry_backoff_max=600,
    max_retries=5,
)
def send_sms(to: str, body: str) -> str | None:
    return deliver_sms(to, body)
