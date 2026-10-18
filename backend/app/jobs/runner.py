"""Run one background job: lock, log, judge, and hand the next stage to the caller.

Each job:

* takes a Redis lock so only one instance runs at a time (others log SKIPPED);
* records a ``background_job_logs`` row with status, duration, the CPU time the process
  used meanwhile (``details["cpu_s"]``) and a result summary;
* on success, passes the next pipeline stage (``NEXT_STAGE``) to ``on_success``: Celery
  queues it (app.workers.tasks), the sync orchestrator runs its own stage order instead
  (``chain=False``).

Data acquisition jobs record provider failures instead of raising them; a ``judge`` turns
such a result into a FAILED job (which never chains). No Celery import here: the sync
daemon runs jobs with this module alone.
"""

from __future__ import annotations

import logging
import resource
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal, utcnow
from app.core.logging import redact
from app.core.redis import LockNotAcquired, redis_lock
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


def cpu_seconds() -> float:
    """CPU time (user + system) this process has used so far."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


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
    job ran (resource.getrusage, so other threads of the process count too).
    """
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
        details = {**details, "cpu_s": round(cpu_seconds() - cpu_started, 3)}
        with SessionLocal() as log_db:
            row = log_db.get(BackgroundJobLog, log_id)
            if row is not None:
                row.status = status
                row.finished_at = utcnow()
                row.duration_ms = int((time.monotonic() - started) * 1000)
                row.details = _jsonable(details)
                row.error_message = error
                log_db.commit()
    if chain and on_success is not None and status == JobStatus.SUCCESS and job_name in NEXT_STAGE:
        on_success(NEXT_STAGE[job_name])
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


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
