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
* :func:`run_due_data_health` (the refresh endpoint): ``ops.data_health``, which raises the
  admin problems and emails the admins (spec 16), once DATA_HEALTH_CHECK_MINUTES have passed
  since the endpoint last ran it, and sooner after a refresh that failed or published part
  of the spots, or when a component's metadata becomes unreadable (:func:`refresh_health_
  signal`; never twice within SYNC_RETRY_MINUTES, and a lasting unreadable component only
  when it starts and once PROVIDER_UNAVAILABLE_AFTER later). Not while a daemon runs (its
  heartbeat is in Redis: it runs the check on its own timer). Two Redis commands when it is
  not due.
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

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
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
# Redis key: ISO time the refresh endpoint last started ops.data_health (no daemon).
DATA_HEALTH_AT_KEY = "sync:data-health-at"
# Redis key: the lasting unreadable-metadata signal (refresh_health_signal) that ran the
# check, when it was first seen and whether its follow-up ran. It outlives the gap between
# two refresh checks (a scheduler silent this long is a problem of its own,
# REFRESH_SCHEDULER_EXPECTED_MINUTES); once no check sees the signal, it expires.
DATA_HEALTH_SIGNAL_KEY = "sync:data-health-signal"
DATA_HEALTH_SIGNAL_TTL_SECONDS = 30 * 60
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
# A daily cron fires anywhere within its hour on Vercel Hobby, and each job of its run
# starts after the run's ``now`` (the watchdog and the jobs before it first): a job of a
# 24 h or longer interval is due once it is within this of its interval, or a job that
# started seconds into one day's run would wait for the day after the next (review
# r2-backend-N1).
STANDIN_SLACK = timedelta(hours=2)


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


def refresh_health_signal(out: dict[str, Any] | None) -> str | None:
    """What of a refresh check the admins hear of without waiting for the interval: a sync
    that failed or published part of the spots, or a component whose metadata could not be
    read. ``"<status>|<kind:component,...>"`` (the unreadable components), or None."""
    if not out:
        return None
    status = str(out.get("status") or "")
    components = (out.get("plan") or {}).get("components") or []
    unknown = sorted(
        {
            f"{c.get('kind')}:{c.get('component')}"
            for c in components
            if isinstance(c, dict) and c.get("state") == "unknown"
        }
    )
    if status in ("failed", "partial") or unknown:
        return f"{status}|{','.join(unknown)}"
    return None


def refresh_needs_health(out: dict[str, Any] | None) -> bool:
    """A refresh check whose sync failed or published part of the spots, or that could not
    read a component's metadata (:func:`refresh_health_signal`)."""
    return refresh_health_signal(out) is not None


def _lasting_signal_due(client: Any, signal: str, now: datetime) -> bool:
    """For a signal of unreadable metadata only: True when it starts (a new signal), and
    once more when it has lasted PROVIDER_UNAVAILABLE_AFTER (that admin problem needs a
    check after that time); False while it lasts otherwise, so a metadata outage or a
    refused provider setting wakes the database hourly, not at every check (review
    r3-backend-R3-2). One GET and one SET or EXPIRE."""
    from app.services.health import PROVIDER_UNAVAILABLE_AFTER

    raw = client.get(DATA_HEALTH_SIGNAL_KEY)
    try:
        stored = json.loads(raw) if raw else None
    except ValueError:
        stored = None
    if not isinstance(stored, dict) or stored.get("signal") != signal:
        record = {"signal": signal, "since": now.isoformat(), "followed": False}
        client.set(DATA_HEALTH_SIGNAL_KEY, json.dumps(record), ex=DATA_HEALTH_SIGNAL_TTL_SECONDS)
        return True
    try:
        since: datetime | None = datetime.fromisoformat(str(stored.get("since")))
    except ValueError:
        since = None
    if (
        not stored.get("followed")
        and since is not None
        and now - since >= PROVIDER_UNAVAILABLE_AFTER
    ):
        record = {**stored, "followed": True}
        client.set(DATA_HEALTH_SIGNAL_KEY, json.dumps(record), ex=DATA_HEALTH_SIGNAL_TTL_SECONDS)
        return True
    client.expire(DATA_HEALTH_SIGNAL_KEY, DATA_HEALTH_SIGNAL_TTL_SECONDS)
    return False


def note_data_health(now: datetime | None = None, settings: Settings | None = None) -> None:
    """Record that ``ops.data_health`` just ran (the daily cron's watchdog), so the refresh
    check that follows in the same cron does not run it again. Best effort."""
    settings = settings or get_settings()
    interval = timedelta(minutes=settings.data_health_check_minutes)
    try:
        get_redis().set(
            DATA_HEALTH_AT_KEY,
            (now or utcnow()).isoformat(),
            ex=int(2 * interval.total_seconds()),
        )
    except redis.RedisError as exc:
        logger.warning("Data-health time not recorded: %s", type(exc).__name__)


def run_due_data_health(
    now: datetime | None = None,
    settings: Settings | None = None,
    *,
    urgent: bool = False,
    signal: str | None = None,
) -> dict[str, Any] | None:
    """Run ``ops.data_health`` from the refresh endpoint when no daemon runs it (the hosted
    path without the optional daemon, :func:`daemon_optional`, and no daemon heartbeat in
    Redis): once DATA_HEALTH_CHECK_MINUTES have passed since the endpoint last started it,
    or sooner for a refresh check's ``signal`` (:func:`refresh_health_signal`; ``urgent``:
    as a failed refresh's). Sooner is never within SYNC_RETRY_MINUTES of the last start: a
    failed or partial refresh runs it (its retries are that far apart, and refresh_failures
    needs three in a row), an unreadable component only as it starts and once more when it
    has lasted PROVIDER_UNAVAILABLE_AFTER (:func:`_lasting_signal_due`). None when it is not
    due: two Redis commands and no database read (review r2-spec-SPEC-3; a signal adds
    up to two)."""
    settings = settings or get_settings()
    if not daemon_optional(settings):
        return None
    now = now or utcnow()
    interval = timedelta(minutes=settings.data_health_check_minutes)
    try:
        client = get_redis()
        if client.exists(state.HEARTBEAT_KEY):
            return None  # a daemon runs the check on its own timer
        raw = client.get(DATA_HEALTH_AT_KEY)
        try:
            last: datetime | None = datetime.fromisoformat(str(raw)) if raw else None
        except ValueError:
            last = None
        soon = urgent or signal is not None
        if (
            soon
            and last is not None
            and now - last < timedelta(minutes=settings.sync_retry_minutes)
        ):
            soon = False  # it ran moments ago
        if soon and signal is not None and signal.split("|", 1)[0] not in ("failed", "partial"):
            soon = _lasting_signal_due(client, signal, now)
    except redis.RedisError as exc:
        logger.warning("Data-health time not read: %s", type(exc).__name__)
        return None
    if not soon and last is not None and now - last < interval:
        return None
    note_data_health(now, settings)
    return {"jobs": _job(DATA_HEALTH_TASK, job_spec(DATA_HEALTH_TASK).run, chain=False)}


def _newest_starts(db: Session, names: list[str]) -> dict[str, datetime]:
    rows = db.execute(
        select(BackgroundJobLog.job_name, func.max(BackgroundJobLog.started_at))
        .where(BackgroundJobLog.job_name.in_(names))
        .group_by(BackgroundJobLog.job_name)
    ).all()
    return {str(name): at for name, at in rows if at is not None}


def overdue_jobs(db: Session, now: datetime, settings: Settings | None = None) -> list[str]:
    """The periodic jobs (``daemon.periodic_entries``, without the data-health check the
    cron runs itself) whose newest run started longer ago than their interval (less
    :data:`STANDIN_SLACK` for an interval of a day or more), in :data:`DAILY_ORDER` then
    name order."""
    from app.services.sync.daemon import periodic_entries

    settings = settings or get_settings()
    gaps: dict[str, Any] = {}
    for entry in periodic_entries(settings):
        if entry.task == DATA_HEALTH_TASK:
            continue
        gap = max_interval(entry.schedule)
        gaps[entry.task] = min(gap, gaps.get(entry.task, gap))
    newest = _newest_starts(db, sorted(gaps))

    def overdue_after(gap: timedelta) -> timedelta:
        return gap - STANDIN_SLACK if gap >= timedelta(hours=24) else gap

    due = [t for t, gap in gaps.items() if t not in newest or now - newest[t] > overdue_after(gap)]
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
