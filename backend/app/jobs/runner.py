"""Run one background job: lock, log, judge, and hand the next stage to the caller.

Each job:

* takes a Redis lock so only one instance runs at a time (others log SKIPPED). Under
  JOB_RUNNER=daemon a Redis that does not answer does not stop the job: it runs without
  the lock (logged with a warning and ``details["lock"]``), because the sync daemon runs
  one job at a time and a sync's stages also hold the Postgres sync lease, so the lock
  only guards against a second runner (a manual CLI or GitHub Actions run). Under Celery,
  where several workers would otherwise run the same job, the job fails instead;
* records a ``background_job_logs`` row with status, duration, the CPU time the process
  used meanwhile (``details["cpu_s"]``) and a result summary;
* on success, passes the next pipeline stage (``NEXT_STAGE``) to ``on_success``: Celery
  queues it (app.workers.tasks), the sync orchestrator runs its own stage order instead
  (``chain=False``).

Data acquisition jobs record provider failures instead of raising them; a ``judge`` turns
such a result into a FAILED job (which never chains). No Celery import here: the sync
daemon runs jobs with this module alone.

CPU this month: every job that is not running inside another job (Celery's sync.tick runs
the stages of a sync as jobs of their own) adds its ``cpu_s`` to a Redis counter for the
current UTC month (:func:`cpu_seconds_this_month`). The data-health CPU budget
(SYNC_CPU_BUDGET_HOURS) reads it, because the job logs alone cannot answer for a whole
month once RETENTION_JOB_LOG_DAYS is shorter than that (7 days in the free database
profile). Best effort: a Redis error never fails a job.
"""

from __future__ import annotations

import logging
import resource
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import redis
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal, utcnow
from app.core.logging import redact
from app.core.redis import LockNotAcquired, acquire_lock, get_redis, release_lock
from app.jobs.schedule import NEXT_STAGE
from app.models import BackgroundJobLog
from app.models.enums import JobStatus

logger = logging.getLogger(__name__)

# Retries of acquisition tasks after a transient failure: 2, 4, 8 min (capped at 15 min),
# or the provider's own retry time when it gives one (capped at 1 h).
ACQUIRE_MAX_RETRIES = 3
RETRY_BASE_SECONDS = 120
RETRY_MAX_SECONDS = 900
RETRY_HINT_MAX_SECONDS = 3600

Judge = Callable[[dict[str, Any]], tuple[JobStatus, str | None]]

# Redis counter of the CPU seconds jobs used in one UTC month ("jobs:cpu_s:2026-10"); kept
# 62 days after its last update, so last month's total is still there at the start of this.
CPU_MONTH_KEY_PREFIX = "jobs:cpu_s:"
CPU_MONTH_TTL_SECONDS = 62 * 86400
# How many run_job calls this thread is inside (only the outermost one counts its CPU).
_nesting = threading.local()
# details["lock"] of a job that ran without its Redis lock (JOB_RUNNER=daemon).
LOCK_NOT_TAKEN = "not taken: Redis unavailable"


def cpu_seconds() -> float:
    """CPU time (user + system) this process has used so far."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def cpu_month_key(when: datetime) -> str:
    """The Redis key of the CPU counter for the UTC month of ``when``."""
    return f"{CPU_MONTH_KEY_PREFIX}{when.astimezone(UTC):%Y-%m}"


def record_cpu_seconds(seconds: float, *, when: datetime | None = None) -> None:
    """Add ``seconds`` of CPU to the counter of the UTC month of ``when`` (now by default).

    Best effort: a Redis error is logged, never raised. run_job calls it for every job that
    is not nested in another; a runner that uses CPU outside jobs can call it too.
    """
    if seconds <= 0:
        return
    key = cpu_month_key(when or utcnow())
    try:
        pipe = get_redis().pipeline(transaction=False)
        pipe.incrbyfloat(key, round(float(seconds), 3))
        pipe.expire(key, CPU_MONTH_TTL_SECONDS)
        pipe.execute()
    except redis.RedisError as exc:
        logger.warning("Job CPU time not recorded: %s", type(exc).__name__)


def cpu_seconds_this_month(now: datetime | None = None) -> float | None:
    """CPU seconds recorded by jobs in the UTC month of ``now``; None when Redis does not
    answer (0.0 when nothing was recorded, e.g. after a Redis flush)."""
    try:
        raw = get_redis().get(cpu_month_key(now or utcnow()))
    except redis.RedisError as exc:
        logger.warning("Job CPU time not read: %s", type(exc).__name__)
        return None
    try:
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return None


@contextmanager
def job_lock(job_name: str, ttl_seconds: int) -> Iterator[str | None]:
    """The job's Redis lock; yields None when it is held, :data:`LOCK_NOT_TAKEN` when the
    job runs without it (JOB_RUNNER=daemon and Redis did not answer, see the module docs).

    Raises LockNotAcquired when another runner holds it, and redis.RedisError under Celery.
    Only taking the lock is guarded: a Redis error inside the job is the job's own.
    """
    name = f"job:{job_name}"
    try:
        token = acquire_lock(name, ttl_seconds)
    except redis.RedisError as exc:
        if get_settings().runs_celery:
            raise
        logger.warning(
            "Job %s runs without its Redis lock (%s): the daemon runs one job at a time",
            job_name,
            type(exc).__name__,
        )
        yield LOCK_NOT_TAKEN
        return
    try:
        yield None
    finally:
        release_lock(name, token)


def run_job(
    job_name: str,
    fn: Callable[[Session], Any],
    *,
    task_id: str | None = None,
    lock_ttl: int = 1800,
    chain: bool = True,
    judge: Judge | None = None,
    on_success: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run ``fn`` under the job lock and log it; returns ``{"status": ..., **details}``.

    ``judge`` turns a result into the job status (and an error message), e.g. FAILED when
    every provider failed. When the job succeeds, ``chain`` is true and the job has a next
    stage (``NEXT_STAGE``), ``on_success`` is called with that stage's job name. A FAILED
    or SKIPPED job never chains. ``details["cpu_s"]`` is the process CPU time used while the
    job ran (resource.getrusage, so other threads of the process count too); a job that is
    not nested in another adds it to this month's counter (:func:`record_cpu_seconds`).
    """
    depth = getattr(_nesting, "depth", 0)
    _nesting.depth = depth + 1
    try:
        status, details = _run_logged(
            job_name, fn, task_id=task_id, lock_ttl=lock_ttl, judge=judge, outermost=depth == 0
        )
    finally:
        _nesting.depth = depth
    # Outside the nesting count: in eager mode the next stage runs right here, and it is a
    # job of its own (as in a worker), not part of this one.
    if chain and on_success is not None and status == JobStatus.SUCCESS and job_name in NEXT_STAGE:
        on_success(NEXT_STAGE[job_name])
    return {"status": status.value, **details}


def _run_logged(
    job_name: str,
    fn: Callable[[Session], Any],
    *,
    task_id: str | None,
    lock_ttl: int,
    judge: Judge | None,
    outermost: bool,
) -> tuple[JobStatus, dict[str, Any]]:
    """The job under its lock, with its log row: (status, details)."""
    started = time.monotonic()
    cpu_started = cpu_seconds()
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
        with job_lock(job_name, lock_ttl) as lock_note:
            with SessionLocal() as db:
                result = fn(db)
            details = result if isinstance(result, dict) else {"result": result}
            if lock_note is not None:
                details = {**details, "lock": lock_note}
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
        details = {**details, "cpu_s": round(cpu_seconds() - cpu_started, 3)}
        if outermost:
            record_cpu_seconds(details["cpu_s"])
        with SessionLocal() as log_db:
            row = log_db.get(BackgroundJobLog, log_id)
            if row is not None:
                row.status = status
                row.finished_at = utcnow()
                row.duration_ms = int((time.monotonic() - started) * 1000)
                row.details = _jsonable(details)
                row.error_message = error
                log_db.commit()
    return status, details


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
