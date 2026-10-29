"""The daemon's other work, done by the refresh endpoint and the daily cron when no daemon
does it (design D8: on Vercel Hobby the sync daemon is optional, ``SYNC_DAEMON_KINDS=map``).

Under JOB_RUNNER=daemon the API asks for work through Redis (``state.request_kick``: an
explicit "Re-check flights", a new saved search to match) and the daemon runs the periodic
jobs on its timers. Without a daemon:

* :func:`run_kicks` (the refresh endpoint, every 5 minutes): takes the pending kicks and
  runs them as the daemon does (``daemon.poll_kicks``): matching.match and flights.discover,
  each followed by the stages after it while they succeed (alerts, delivery). With no kick
  pending it is one Redis command and no database read, so a check with nothing due still
  leaves a scale-to-zero database asleep.
* :func:`run_due_delivery` (the refresh endpoint): a notification whose delivery failed
  for a temporary reason is retried once its backoff has passed. The delivery job records
  the earliest retry time in Redis (:func:`note_next_delivery`); the endpoint reads that
  one key and runs ``notifications.deliver`` only when it is due.
* :func:`run_overdue_jobs` (the daily cron): every periodic job of the schedule whose
  newest run is older than its own interval (``schedule.max_interval``): maintenance
  cleanup, the matching housekeeping, tide acquisition, alert generation, delivery, the
  staleness check, ... A daemon that runs them on time leaves nothing overdue, so nothing
  runs twice; Celery beat likewise.

Every job runs through ``app.jobs.runner.run_job``: its Redis lock (another runner holding
it makes this run SKIPPED), its log row and its CPU accounting. Nothing here raises: a
failed job is logged and reported in the summary.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, utcnow
from app.core.redis import get_redis
from app.jobs.registry import discover_flights_run, job_spec
from app.jobs.runner import run_job
from app.jobs.schedule import DATA_HEALTH_TASK, max_interval
from app.models import BackgroundJobLog
from app.services.sync import state

logger = logging.getLogger(__name__)

# Redis key: ISO time of the earliest queued notification's next attempt (no key: none).
DELIVERY_DUE_KEY = "sync:delivery-due"
DELIVERY_DUE_TTL_SECONDS = 7 * 86400
DELIVER_JOB = "notifications.deliver"
# The order the daily cron runs overdue jobs in: cleanup first (it frees space), the
# matching housekeeping (chained to flights, alerts and delivery) before alerts.
DAILY_ORDER = (
    "maintenance.cleanup",
    "matching.match",
    "tides.acquire",
    "alerts.generate",
    DELIVER_JOB,
    "forecasts.check_staleness",
)
# Stop starting jobs after this many seconds of the daily cron's run, leaving room in the
# Function's maxDuration (300 s) for the refresh backstop that follows.
DAILY_BUDGET_SECONDS = 150.0


def daemon_optional(settings: Settings | None = None) -> bool:
    """JOB_RUNNER=daemon with spot syncs left to the refresh endpoint (``spots`` not in
    SYNC_DAEMON_KINDS, the hosted profile): the daemon may not run at all."""
    settings = settings or get_settings()
    return settings.job_runner == "daemon" and "spots" not in settings.sync_daemon_kind_list


def _job(name: str, body: Callable[[Session], Any], *, chain: bool) -> list[dict[str, Any]]:
    """Run ``name`` (and, with ``chain``, the stages after it while they succeed); one
    ``{job, status}`` per job run. Never raises."""
    queue: list[tuple[str, Callable[[Session], Any]]] = [(name, body)]
    out: list[dict[str, Any]] = []
    while queue:
        job, fn = queue.pop(0)
        spec = job_spec(job)
        following: list[str] = []
        try:
            result = run_job(
                job,
                fn,
                lock_ttl=spec.lock_ttl,
                judge=spec.judge,
                chain=chain and spec.chain,
                on_success=following.append,
            )
        except Exception as exc:  # run_job logged it FAILED
            logger.warning("Stand-in job %s failed: %s", job, type(exc).__name__)
            out.append({"job": job, "status": "failed", "error": type(exc).__name__})
            continue
        out.append({"job": job, "status": result.get("status")})
        queue.extend((n, job_spec(n).run) for n in following)
    return out


def run_kicks(settings: Settings | None = None) -> dict[str, Any] | None:
    """Take the API's pending kicks and run them (as ``daemon.poll_kicks``); None when
    there were none (or JOB_RUNNER is not daemon: Celery queues the kicked tasks itself)."""
    settings = settings or get_settings()
    if settings.job_runner != "daemon":
        return None
    kicks = state.pop_kicks()
    if not kicks:
        return None
    logger.info("Kicks taken by the refresh endpoint: %s", {k: len(v) for k, v in kicks.items()})
    ran: list[dict[str, Any]] = []
    if "matching.match" in kicks:
        ran += _job("matching.match", job_spec("matching.match").run, chain=True)
    ids = kicks.get("flights.discover")
    if ids:
        match_ids = None if state.ALL_MATCHES in ids else ids
        done = {r["job"] for r in ran if r["status"] == "success"}
        if match_ids is not None or "flights.discover" not in done:
            ran += _job("flights.discover", discover_flights_run(match_ids), chain=True)
    return {"kicks": {k: len(v) for k, v in kicks.items()}, "jobs": ran}


def note_next_delivery(db: Session) -> datetime | None:
    """Record in Redis when the earliest queued notification is due (its backoff after a
    temporary failure), or clear the record when none is queued. Best effort."""
    from app.models import Notification
    from app.models.enums import NotificationStatus

    due = db.scalar(
        select(func.min(Notification.next_attempt_at)).where(
            Notification.status == NotificationStatus.QUEUED
        )
    )
    try:
        client = get_redis()
        if due is None:
            client.delete(DELIVERY_DUE_KEY)
        else:
            client.set(DELIVERY_DUE_KEY, due.isoformat(), ex=DELIVERY_DUE_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Next delivery time not recorded: %s", type(exc).__name__)
    return due


def delivery_due(now: datetime) -> bool:
    """Whether a queued notification's retry is due (one Redis GET; False on any error)."""
    try:
        raw = get_redis().get(DELIVERY_DUE_KEY)
    except redis.RedisError as exc:
        logger.warning("Next delivery time not read: %s", type(exc).__name__)
        return False
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw)) <= now
    except ValueError:
        return True  # unreadable: deliver, which writes it again


def run_due_delivery(
    now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any] | None:
    """Run ``notifications.deliver`` when a retry is due (JOB_RUNNER=daemon only); None
    when nothing is due."""
    settings = settings or get_settings()
    if settings.job_runner != "daemon" or not delivery_due(now or utcnow()):
        return None
    return {"jobs": _job(DELIVER_JOB, job_spec(DELIVER_JOB).run, chain=False)}


def _newest_starts(db: Session, names: list[str]) -> dict[str, datetime]:
    rows = db.execute(
        select(BackgroundJobLog.job_name, func.max(BackgroundJobLog.started_at))
        .where(BackgroundJobLog.job_name.in_(names))
        .group_by(BackgroundJobLog.job_name)
    ).all()
    return {str(name): at for name, at in rows if at is not None}


def overdue_jobs(db: Session, now: datetime, settings: Settings | None = None) -> list[str]:
    """The periodic jobs (``daemon.periodic_entries``, without the data-health check the
    cron runs itself) whose newest run started longer ago than their interval, in
    :data:`DAILY_ORDER` then name order."""
    from app.services.sync.daemon import periodic_entries

    settings = settings or get_settings()
    gaps: dict[str, Any] = {}
    for entry in periodic_entries(settings):
        if entry.task == DATA_HEALTH_TASK:
            continue
        gap = max_interval(entry.schedule)
        gaps[entry.task] = min(gap, gaps.get(entry.task, gap))
    newest = _newest_starts(db, sorted(gaps))
    due = [t for t, gap in gaps.items() if t not in newest or now - newest[t] > gap]
    rank = {name: i for i, name in enumerate(DAILY_ORDER)}
    return sorted(due, key=lambda t: (rank.get(t, len(rank)), t))


def run_overdue_jobs(
    now: datetime | None = None,
    settings: Settings | None = None,
    *,
    budget_s: float = DAILY_BUDGET_SECONDS,
) -> dict[str, Any]:
    """The daily cron's stand-in for the daemon's timers: run every overdue periodic job
    (:func:`overdue_jobs`; a job chained in by an earlier one is not run twice), starting
    none after ``budget_s`` seconds. ``{overdue, jobs, deferred}``."""
    settings = settings or get_settings()
    now = now or utcnow()
    started = time.monotonic()
    with SessionLocal() as db:
        due = overdue_jobs(db, now, settings)
    ran: list[dict[str, Any]] = []
    deferred: list[str] = []
    for name in due:
        if any(r["job"] == name for r in ran):
            continue
        if time.monotonic() - started > budget_s:
            deferred.append(name)
            continue
        ran += _job(name, job_spec(name).run, chain=name == "matching.match")
    if ran or deferred:
        logger.info(
            "Daily stand-in jobs: %s%s",
            ", ".join(f"{r['job']} {r['status']}" for r in ran) or "none",
            f"; deferred: {', '.join(deferred)}" if deferred else "",
        )
    return {"overdue": due, "jobs": ran, "deferred": deferred}
