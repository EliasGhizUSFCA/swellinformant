"""Data health: is the stack still producing fresh data, unattended?

:func:`data_health` builds the report served by ``GET /api/health/data`` and read by the
``ops.data_health`` job (app.services.notifications.ops_alerts), which emails admin users.
It checks, with plain reasons:

* **Forecast sources** (every FORECAST_PROVIDERS source, the blend when two independent
  model families are configured, GEFS-Wave when GEFS_ENABLED): the newest successful run,
  its age and the source's own stale limit (``queries.stale_after_hours``, ADR-015). A
  stale blend is only a problem of its own when every member is fresh and no acquisition
  or blend run is in progress (the chain rebuilds it right after the acquisition).
* **Scheduled jobs** (the job schedule of the current settings, app.jobs.schedule, as the
  configured runner runs it: Celery beat every entry, the sync daemon every entry but the
  sync's own jobs; plus the jobs a sync runs as its stages): the last result from
  ``background_job_logs``,
  consecutive failures, overdue runs (no run started for ``2 x longest interval + 30
  min``; a run in progress counts) and stuck runs (still "running" 15 min after the hard
  task time limit: the process running it was killed). The sync's stages (forecast
  acquisition, map layers, ensemble, blend, predictions, publish, detection) have no
  interval of their own, so they are never overdue: a sync runs them when a provider
  publishes a new model run, and the source freshness below covers them.
* **Map fields** (unless the mode is "off"): the model cycle of the run the map serves
  (``map_fields.served_run``: the newest publication's map run under SYNC_PUBLISH_GATE)
  against MAP_FIELD_STALE_AFTER_HOURS, the limit the map index uses for its "out of date"
  badge.
* **Observations** (when OBSERVATIONS_ENABLED): the newest buoy observation per provider
  against DATA_HEALTH_OBSERVATION_STALE_HOURS (default: the ingestion look-back window).
* **Heartbeat**: under JOB_RUNNER=celery, the newest finished job of any kind (this
  check's own job excluded). Under JOB_RUNNER=daemon, the sync daemon's heartbeat in Redis
  (written at every check), stale after ``max(3 x SYNC_CHECK_MINUTES,
  DATA_HEALTH_HEARTBEAT_MINUTES)`` unless a sync with a valid lease is running.
* **Sync** (``DataHealthOut.sync``): the runner, the last check, the newest and the newest
  published sync with their stages, the components of the last check (daemon), and the
  CPU used this month against SYNC_CPU_BUDGET_HOURS (degraded above 80 %). The two newest
  finished syncs both failed: degraded.
* **Licence**: COMMERCIAL_USE=true with an Open-Meteo source and no OPEN_METEO_API_KEY is
  failing (the free API is for non-commercial use only; the provider refuses to run).
* **Storage**: the database size, against DATA_HEALTH_DATABASE_MAX_GB when that is set
  (default 20 GB: an alert only, nothing is deleted); Redis memory against its cap
  (Redis's maxmemory, or REDIS_MAXMEMORY when Redis runs without one), degraded above
  DATA_HEALTH_REDIS_MAX_FRACTION or with an evicting maxmemory-policy (the Celery broker
  must never lose messages; not checked under JOB_RUNNER=daemon, which has no broker and
  whose hosted Redis sets its own policy), or when Redis does not answer.
* **Tides**: the problems app.services.tides.health.tide_freshness reports. The tide
  model's constants file is checked where it is deployed (always under JOB_RUNNER=celery);
  the Vercel API function has no data/ and reports the sync daemon's own check from its
  heartbeat instead (``file_problems``), so both runs of the alert job see the same
  problems.

Overall status: ``failing`` when the product cannot serve fresh forecasts (no forecast
source is fresh), the job runner is down (Celery: no background job has finished for
DATA_HEALTH_HEARTBEAT_MINUTES; daemon: its heartbeat is stale) or the licence check fails;
``degraded`` for any other problem; ``ok`` otherwise.

Assumptions: the limits above are operational heuristics (how late a run may be before
someone should look), not statements about forecast accuracy. A worker that is down
cannot send its own alert: poll this endpoint from an external monitor as well
(docs/operations.md).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from pydantic import ValidationError
from sqlalchemy import Float, String, and_, column, func, select, true, values
from sqlalchemy.orm import Session

from app.core.config import OPEN_METEO_PROVIDER_TOKENS, Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.core.redis import parse_memory_size, redis_memory
from app.jobs.registry import JOB_SPECS
from app.jobs.runner import cpu_seconds_this_month
from app.jobs.schedule import (
    DATA_HEALTH_TASK,
    TASK_TIME_LIMIT_SECONDS,
    Every,
    build_schedule,
    describe_schedule,
    max_interval,
    time_limit_seconds,
)
from app.models import BackgroundJobLog, BuoyObservation, BuoyStation, ForecastRun, ForecastSource
from app.models.enums import JobStatus
from app.models.sync import SyncRun
from app.schemas.health import (
    DataHealthOut,
    Freshness,
    HealthProblemOut,
    HeartbeatOut,
    JobHealthOut,
    MapFieldsHealthOut,
    MapFieldsStatus,
    ObservationProviderHealthOut,
    ObservationsHealthOut,
    OverallStatus,
    RedisHealthOut,
    Severity,
    SourceHealthOut,
    SourceRole,
    StorageOut,
)
from app.schemas.sync import (
    SyncComponentOut,
    SyncHealthOut,
    SyncRunOut,
    SyncStageOut,
    SyncStoredRunOut,
)
from app.services.forecasts.open_meteo import free_api_refused
from app.services.forecasts.queries import BLEND_CODE, USABLE, is_stale, stale_after_hours
from app.services.forecasts.registry import (
    GEFS_WAVE_CODE,
    KNOWN_SOURCE_NAMES,
    configured_source_codes,
)
from app.services.map_fields import map_fields_mode, served_run
from app.services.sync import state
from app.services.sync.detect import newest_cycle
from app.services.sync.published import latest_publication
from app.services.sync.types import STAGE_JOB_NAMES, SYNC_OWNED_TASKS, SYNC_TICK_JOB
from app.services.tides.health import constants_file_present, tide_freshness

# Jobs that (re)build the blend: forecasts.acquire, then the chained forecasts.blend stage.
BLEND_BUILDERS = frozenset({"forecasts.acquire", "forecasts.blend"})
OVERDUE_FACTOR = 2  # missed runs tolerated before a job counts as overdue
OVERDUE_GRACE = timedelta(minutes=30)  # queueing behind long jobs (2 worker slots)
STUCK_GRACE = timedelta(minutes=15)
RECENT_LOGS = 25  # job log rows read per job (newest first)
# Runs that finished with a result; "skipped" (the job lock was held) and "running" are not.
REAL_RESULTS = (JobStatus.SUCCESS, JobStatus.FAILED)
# The schedule shown for the jobs a sync runs as its stages (no interval of their own).
SYNC_STAGE_SCHEDULE = "in each sync"
# The daemon's heartbeat is stale after this many check intervals (or
# DATA_HEALTH_HEARTBEAT_MINUTES, whichever is longer).
HEARTBEAT_CHECKS = 3
# A Celery runner is down when no job finished for this many of its shortest intervals (or
# DATA_HEALTH_HEARTBEAT_MINUTES, whichever is longer).
JOB_HEARTBEAT_INTERVALS = 2
# Sync statuses that count as a failed sync; the newest SYNC_FAILURES_IN_A_ROW finished
# syncs all failing is a problem.
FAILED_SYNC_STATUSES = frozenset({"failed", "abandoned"})
SYNC_FAILURES_IN_A_ROW = 2
# SYNC_CPU_BUDGET_HOURS: degraded above this share of the budget.
CPU_BUDGET_ALERT_FRACTION = 0.8
# Component states whose detail is shown as written; "unknown" carries a provider error.
UNKNOWN_COMPONENT_DETAIL = "the provider's run metadata could not be read"
# A failed component's detail after a sync (detect.settle_plan) ends with the stage's
# exception text (redacted for keys and addresses, but e.g. a database host, port and
# role): the public report keeps only "sync N failed". The text stays in sync_runs and in
# the admin email of a failed sync.
SYNC_FAILURE_DETAIL = re.compile(r"^(sync \d+) failed\b")
SYNC_FAILURE_PUBLIC = "{sync} failed; the error text is not shown publicly"
DETAIL_MAX_CHARS = 300


@dataclass(frozen=True)
class Problem:
    """One reason the data is not healthy.

    ``key`` is stable while the same problem persists (alert de-duplication uses it);
    ``message`` is public; ``detail`` (e.g. a job's redacted error) goes to admin emails
    only and is never served by the API.
    """

    key: str
    severity: Severity
    message: str
    detail: str | None = None


@dataclass(frozen=True)
class HealthReport:
    out: DataHealthOut
    problems: list[Problem]

    @property
    def status(self) -> OverallStatus:
        return self.out.status


# --------------------------------------------------------------------------- formatting
def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600.0, 2)


def ago(delta: timedelta) -> str:
    """Human duration: '12 min', '3.4 h', '2.1 days'."""
    minutes = delta.total_seconds() / 60
    if minutes < 90:
        return f"{max(0, round(minutes))} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"


def stamp(when: datetime) -> str:
    """'2026-10-12 06:00 UTC'. Converted first: psycopg returns ``timestamptz`` values in
    the session's time zone, which is UTC on the Compose server but need not be elsewhere."""
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- sources
def _source(db: Session, code: str) -> ForecastSource | None:
    return db.scalar(select(ForecastSource).where(ForecastSource.code == code))


def _newest_usable(db: Session, source: ForecastSource) -> ForecastRun | None:
    return db.scalar(
        select(ForecastRun)
        .where(ForecastRun.source_id == source.id, ForecastRun.status.in_(USABLE))
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def _latest_attempt(db: Session, source: ForecastSource) -> ForecastRun | None:
    return db.scalar(
        select(ForecastRun)
        .where(ForecastRun.source_id == source.id)
        .order_by(ForecastRun.started_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def _source_health(
    db: Session, code: str, role: SourceRole, now: datetime, settings: Settings
) -> tuple[SourceHealthOut, ForecastSource | None]:
    source = _source(db, code)
    limit = None if code == BLEND_CODE else stale_after_hours(code, settings)
    if source is None:
        out = SourceHealthOut(
            code=code, name=code, role=role, status="missing", stale_after_hours=limit
        )
        return out, None
    newest = _newest_usable(db, source)
    attempt = _latest_attempt(db, source)
    status: Freshness = (
        "missing" if newest is None else ("stale" if is_stale(newest, now) else "ok")
    )
    out = SourceHealthOut(
        code=code,
        name=source.name,
        role=role,
        status=status,
        newest_run_key=newest.run_key if newest else None,
        newest_issued_at=newest.issued_at if newest else None,
        age_hours=_hours(now - newest.issued_at) if newest else None,
        stale_after_hours=limit,
        latest_attempt_status=attempt.status.value if attempt else None,
        latest_attempt_started_at=attempt.started_at if attempt else None,
    )
    return out, source


def _source_message(s: SourceHealthOut) -> str:
    if s.status == "missing":
        text = f"{s.name}: no successful run yet"
    elif s.stale_after_hours is None:
        text = (
            f"{s.name}: newest run {s.newest_run_key} has a stale member "
            f"(issued {s.age_hours:.1f} h ago)"
        )
    else:
        text = (
            f"{s.name}: newest run {s.newest_run_key} was issued {s.age_hours:.1f} h ago "
            f"(stale after {s.stale_after_hours:.1f} h)"
        )
    if (
        s.latest_attempt_status == "failed"
        and s.latest_attempt_started_at is not None
        and (s.newest_issued_at is None or s.latest_attempt_started_at > s.newest_issued_at)
    ):
        text += f"; the latest attempt ({stamp(s.latest_attempt_started_at)}) failed"
    return text + "."


def _blend_building(db: Session, now: datetime) -> bool:
    """True while forecasts.acquire or forecasts.blend has a run in progress (not stuck)."""
    since = now - timedelta(seconds=TASK_TIME_LIMIT_SECONDS) - STUCK_GRACE
    running = db.scalar(
        select(func.count())
        .select_from(BackgroundJobLog)
        .where(
            BackgroundJobLog.job_name.in_(BLEND_BUILDERS),
            BackgroundJobLog.status == JobStatus.RUNNING,
            BackgroundJobLog.started_at > since,
        )
    )
    return bool(running)


def _check_sources(
    db: Session, settings: Settings, now: datetime, building: bool = False
) -> tuple[list[SourceHealthOut], list[Problem]]:
    """Forecast sources, blend and ensemble.

    ``building``: forecasts.acquire or forecasts.blend is running now. A member that has
    just been refreshed by that acquisition leaves the blend behind until the chained
    blend stage has run, so a stale blend is not a problem of its own meanwhile.
    """
    out: list[SourceHealthOut] = []
    problems: list[Problem] = []
    forecast_codes = configured_source_codes(settings)
    families: set[str] = set()
    for code in forecast_codes:
        health, source = _source_health(db, code, "forecast", now, settings)
        out.append(health)
        if (
            source is not None
            and not source.is_demo
            and source.kind == "deterministic"
            and source.family != "unknown"
        ):
            families.add(source.family)
        if health.status != "ok":
            problems.append(
                Problem(f"forecast_{health.status}:{code}", "degraded", _source_message(health))
            )
    forecasts = [s for s in out if s.role == "forecast"]
    if forecasts and all(s.status != "ok" for s in forecasts):
        problems.append(
            Problem(
                "forecasts_unavailable",
                "failing",
                "No forecast source has a fresh run: spot forecasts, swell events and alerts "
                "are based on stale data or none.",
            )
        )
    if settings.forecast_blend_enabled and len(families) >= 2:
        blend, _ = _source_health(db, BLEND_CODE, "blend", now, settings)
        out.insert(0, blend)
        # Only a problem of its own when every member is fresh: otherwise the member's
        # problem already explains it (and the blend is rebuilt once the member recovers).
        # Nor while an acquisition or the blend itself is running: the chain rebuilds it.
        if blend.status != "ok" and all(s.status == "ok" for s in forecasts) and not building:
            problems.append(
                Problem(
                    f"blend_{blend.status}",
                    "degraded",
                    _source_message(blend)[:-1]
                    + ", although every member source is fresh: check the forecasts.blend job.",
                )
            )
    if settings.gefs_enabled:
        ensemble, _ = _source_health(db, GEFS_WAVE_CODE, "ensemble", now, settings)
        out.append(ensemble)
        if ensemble.status != "ok":
            problems.append(
                Problem(
                    f"ensemble_{ensemble.status}",
                    "degraded",
                    _source_message(ensemble)[:-1]
                    + " (used for forecast uncertainty and confidence only).",
                )
            )
    return out, problems


# --------------------------------------------------------------------------- jobs
@dataclass(frozen=True)
class ExpectedJob:
    name: str
    schedule: str
    max_gap: timedelta | None  # None: chained stage without a schedule of its own


def sync_stage_jobs(settings: Settings) -> list[str]:
    """The jobs a sync runs as its stages under these settings (STAGE_JOB_NAMES, without
    the stages switched off: ensemble without GEFS_ENABLED, map layers outside the
    standalone map-field mode, the blend without FORECAST_BLEND_ENABLED, prune without
    SYNC_PRUNE_EACH_PASS)."""
    off = set()
    if not settings.gefs_enabled:
        off.add("ensemble")
    if map_fields_mode(settings) != "standalone":
        off.add("map")
    if not settings.forecast_blend_enabled:
        off.add("blend")
    if not settings.sync_prune_each_pass:
        off.add("prune")
    return [job for stage, job in STAGE_JOB_NAMES.items() if stage not in off]


def expected_jobs(settings: Settings) -> list[ExpectedJob]:
    """The scheduled jobs (app.jobs.schedule) for these settings plus the sync's stages.

    Under JOB_RUNNER=daemon only the entries the daemon runs on its timers are expected
    (app.services.sync.daemon.periodic_entries: not the sync's own jobs, SYNC_OWNED_TASKS,
    sync.tick included, nor a task without a job spec); its Redis heartbeat stands for the
    sync check.
    """
    jobs: dict[str, ExpectedJob] = {}
    for entry in build_schedule(settings).values():
        name, sched = entry.task, entry.schedule
        if not settings.runs_celery and (name in SYNC_OWNED_TASKS or name not in JOB_SPECS):
            continue
        gap = max_interval(sched)
        known = jobs.get(name)
        if known is None or (known.max_gap is not None and gap < known.max_gap):
            jobs[name] = ExpectedJob(name, describe_schedule(sched), gap)
    for stage_job in sync_stage_jobs(settings):
        jobs.setdefault(stage_job, ExpectedJob(stage_job, SYNC_STAGE_SCHEDULE, None))
    return sorted(jobs.values(), key=lambda j: j.name)


def runner_hint(settings: Settings) -> str:
    """Which processes run the jobs, for problem messages."""
    if settings.runs_celery:
        return "the scheduler (beat) or the worker"
    return "the sync daemon (on Vercel: the project's Daemons)"


def _recent_logs(db: Session, names: list[str]) -> dict[str, list[BackgroundJobLog]]:
    """The newest RECENT_LOGS job logs of each job in ``names`` (newest first), in one
    statement: each name LATERAL-joined to the (job_name, started_at) index."""
    out: dict[str, list[BackgroundJobLog]] = {name: [] for name in names}
    if not names:
        return out
    log = BackgroundJobLog
    jobs = values(column("name", String), name="jobs").data([(name,) for name in names])
    newest = (
        select(log.id)
        .where(log.job_name == jobs.c.name)
        .order_by(log.started_at.desc(), log.id.desc())
        .limit(RECENT_LOGS)
        .lateral("newest")
    )
    ids = select(newest.c.id).select_from(jobs).join(newest, true())
    stmt = (
        select(log)
        .where(log.id.in_(ids))
        .order_by(log.job_name, log.started_at.desc(), log.id.desc())
    )
    for row in db.scalars(stmt):
        out[row.job_name].append(row)
    return out


def _job_health(
    db: Session,
    job: ExpectedJob,
    rows: list[BackgroundJobLog],
    now: datetime,
    monitoring_since: datetime | None,
    runner: str = "the scheduler (beat) or the worker",
) -> tuple[JobHealthOut, Problem | None]:
    """``rows``: the job's newest RECENT_LOGS logs, newest first (:func:`_recent_logs`)."""
    real = [r for r in rows if r.status in REAL_RESULTS]
    if not real and rows:
        # Only skipped (lock held) or running rows in the window: look further back.
        real = list(
            db.scalars(
                select(BackgroundJobLog)
                .where(
                    BackgroundJobLog.job_name == job.name,
                    BackgroundJobLog.status.in_(REAL_RESULTS),
                )
                .order_by(BackgroundJobLog.started_at.desc(), BackgroundJobLog.id.desc())
                .limit(RECENT_LOGS)
            ).all()
        )
    success = next((r for r in real if r.status == JobStatus.SUCCESS), None)
    if success is None and real:
        success = db.scalar(
            select(BackgroundJobLog)
            .where(
                BackgroundJobLog.job_name == job.name,
                BackgroundJobLog.status == JobStatus.SUCCESS,
            )
            .order_by(BackgroundJobLog.started_at.desc())
            .limit(1)
        )
    failures = 0
    for r in real:
        if r.status != JobStatus.FAILED:
            break
        failures += 1
    overdue_after = (
        job.max_gap * OVERDUE_FACTOR + OVERDUE_GRACE if job.max_gap is not None else None
    )
    latest = real[0] if real else None  # the newest run that finished with a result
    # The run in progress: the newest "running" row that started after that result. A
    # duplicate that beat or the chain queued meanwhile is logged "skipped" (the job lock
    # is held) after it, so the newest row alone would hide the run (seen live: a false
    # "overdue" for forecasts.acquire while it was downloading after an outage). Older
    # "running" rows are left behind by killed worker processes and are ignored.
    active = next(
        (
            r
            for r in rows
            if r.status == JobStatus.RUNNING
            and (latest is None or r.started_at > latest.started_at)
        ),
        None,
    )
    last = next((r for r in rows if r.status != JobStatus.RUNNING), None)
    time_limit = time_limit_seconds(job.name)
    stuck_after = timedelta(seconds=time_limit) + STUCK_GRACE

    status: str = "ok"
    problem: Problem | None = None
    if active is not None and now - active.started_at > stuck_after:
        status = "stuck"
        problem = Problem(
            f"job_stuck:{job.name}",
            "degraded",
            f"Job {job.name} has been 'running' since {stamp(active.started_at)} "
            f"({ago(now - active.started_at)}), past the {time_limit // 60}-minute "
            "task time limit: the process running it was probably killed (e.g. out of memory).",
        )
    elif latest is not None and latest.status == JobStatus.FAILED:
        status = "failed"
        since = (
            f"last success {ago(now - success.started_at)} ago"
            if success is not None
            else "it has not succeeded yet"
        )
        more = "+" if failures == len(real) and RECENT_LOGS in (len(rows), len(real)) else ""
        runs = f"{failures}{more} run{'' if failures == 1 and not more else 's'}"
        problem = Problem(
            f"job_failed:{job.name}",
            "degraded",
            f"Job {job.name} failed at {stamp(latest.started_at)} ({runs} in a row; {since}).",
            detail=latest.error_message,
        )
    elif overdue_after is not None:
        # A run in progress (and not stuck, checked above) counts as a run: the job did
        # start. This also keeps ops.data_health from calling itself overdue while it runs
        # for the first time after an outage.
        starts = [r.started_at for r in (latest, active) if r is not None]
        reference = max(starts) if starts else None
        if reference is not None and now - reference > overdue_after:
            status = "overdue"
            problem = Problem(
                f"job_overdue:{job.name}",
                "degraded",
                f"Job {job.name} has not run for {ago(now - reference)} (schedule: "
                f"{job.schedule}): {runner} may be down or saturated.",
            )
        elif reference is None and monitoring_since is not None:
            if now - monitoring_since > overdue_after:
                status = "overdue"
                problem = Problem(
                    f"job_overdue:{job.name}",
                    "degraded",
                    f"Job {job.name} has never run, although background jobs have been "
                    f"logged for {ago(now - monitoring_since)} (schedule: {job.schedule}).",
                )
            else:
                status = "never_run"
        elif reference is None:
            status = "never_run"
    elif latest is None and active is None:
        status = "never_run"
    if status == "ok" and active is not None:
        status = "running"

    out = JobHealthOut(
        job=job.name,
        schedule=job.schedule,
        status=status,  # type: ignore[arg-type]
        last_status=last.status.value if last else None,
        last_started_at=last.started_at if last else None,
        last_finished_at=last.finished_at if last else None,
        last_duration_ms=last.duration_ms if last else None,
        last_success_at=success.started_at if success else None,
        consecutive_failures=failures,
        overdue_after_minutes=overdue_after.total_seconds() / 60 if overdue_after else None,
    )
    return out, problem


def _check_jobs(
    db: Session, settings: Settings, now: datetime
) -> tuple[list[JobHealthOut], HeartbeatOut, list[Problem]]:
    monitoring_since = db.scalar(select(func.min(BackgroundJobLog.started_at)))
    out: list[JobHealthOut] = []
    problems: list[Problem] = []
    runner = runner_hint(settings)
    jobs = expected_jobs(settings)
    recent = _recent_logs(db, [job.name for job in jobs])
    for job in jobs:
        health, problem = _job_health(db, job, recent[job.name], now, monitoring_since, runner)
        out.append(health)
        if problem is not None:
            problems.append(problem)

    last_finished = db.scalar(
        select(func.max(BackgroundJobLog.finished_at)).where(
            BackgroundJobLog.job_name != DATA_HEALTH_TASK
        )
    )
    shortest = _shortest_interval(settings)
    limit = settings.data_health_heartbeat_minutes
    if shortest is not None:
        # JOB_INTERVAL_FLOOR_MINUTES can space every job further apart than the limit.
        limit = max(limit, int(JOB_HEARTBEAT_INTERVALS * shortest[1]))
    heartbeat = HeartbeatOut(
        last_job_finished_at=last_finished,
        age_minutes=round((now - last_finished).total_seconds() / 60, 1) if last_finished else None,
        limit_minutes=limit,
    )
    if not settings.runs_celery:
        # The daemon runs jobs at most every JOB_INTERVAL_FLOOR_MINUTES and only syncs when
        # a model run is new: its Redis heartbeat says whether it is alive (_check_sync).
        return out, heartbeat, problems
    if last_finished is None:
        problems.append(
            Problem(
                "jobs_never_ran",
                "degraded",
                "No background job has finished yet: check that the worker and the scheduler "
                "(beat) are running.",
            )
        )
    elif now - last_finished > timedelta(minutes=limit):
        problems.append(
            Problem(
                "jobs_not_running",
                "failing",
                f"No background job has finished for {ago(now - last_finished)} (limit {limit} "
                f"min{_shortest_text(shortest)}): the worker or the scheduler (beat) is down.",
            )
        )
    return out, heartbeat, problems


def _shortest_interval(settings: Settings) -> tuple[str, float] | None:
    """(task, minutes) of the most frequent interval job of the schedule."""
    every = [
        (e.task, float(e.schedule.minutes))
        for e in build_schedule(settings).values()
        if isinstance(e.schedule, Every)
    ]
    return min(every, key=lambda t: t[1]) if every else None


def _shortest_text(shortest: tuple[str, float] | None) -> str:
    if shortest is None:
        return ""
    return f"; {shortest[0]} alone runs every {shortest[1]:g} min"


# --------------------------------------------------------------------------- map fields
def _check_map_fields(
    db: Session, settings: Settings, now: datetime
) -> tuple[MapFieldsHealthOut, list[Problem]]:
    from app.models.forecast_engine import MapField

    mode = map_fields_mode(settings)
    if mode == "off":
        return MapFieldsHealthOut(mode=mode, status="off"), []
    # The map index's own limit (app.services.map_fields.field_index), so the data-health
    # page and the map's "out of date" badge agree.
    limit = settings.map_field_stale_after_hours
    served = served_run(db)
    if served is None:
        return MapFieldsHealthOut(mode=mode, status="missing", stale_after_hours=limit), [
            Problem(
                "map_fields_missing",
                "degraded",
                "Map ocean layers: no complete NOAA field set is stored yet, so the map "
                "shows no wave or wind layer.",
            )
        ]
    run = db.get(ForecastRun, served.run_id)
    last_valid = db.scalar(
        select(func.max(MapField.valid_time)).where(MapField.run_id == served.run_id)
    )
    age = now - served.cycle_time
    status: MapFieldsStatus = "stale" if age > timedelta(hours=limit) else "ok"
    out = MapFieldsHealthOut(
        mode=mode,
        status=status,
        run_key=run.run_key if run else None,
        cycle_time=served.cycle_time,
        age_hours=_hours(age),
        stale_after_hours=limit,
        complete_valid_times=served.complete_times,
        last_valid_time=last_valid,
    )
    problems = []
    if status == "stale":
        problems.append(
            Problem(
                "map_fields_stale",
                "degraded",
                f"Map ocean layers: the served GFS cycle {stamp(served.cycle_time)} is "
                f"{age.total_seconds() / 3600:.1f} h old (stale after {limit:.1f} h).",
            )
        )
    return out, problems


# --------------------------------------------------------------------------- observations
def observation_stale_hours(settings: Settings) -> float:
    if settings.data_health_observation_stale_hours is not None:
        return float(settings.data_health_observation_stale_hours)
    return float(settings.observation_lookback_hours)


def _check_observations(
    db: Session, settings: Settings, now: datetime
) -> tuple[ObservationsHealthOut, list[Problem]]:
    if not settings.observations_enabled:
        return ObservationsHealthOut(enabled=False), []
    limit = observation_stale_hours(settings)
    # Newest observation per station through the (station_id, observed_at) unique index,
    # then the newest per provider: no scan of the whole observation archive.
    newest = (
        select(BuoyObservation.observed_at)
        .where(BuoyObservation.station_id == BuoyStation.id)
        .order_by(BuoyObservation.observed_at.desc())
        .limit(1)
        .correlate(BuoyStation)
        .lateral("newest")
    )
    rows = db.execute(
        select(
            BuoyStation.provider,
            func.count().filter(BuoyStation.is_active),
            func.max(newest.c.observed_at),
        )
        .select_from(BuoyStation)
        .outerjoin(newest, true())
        .group_by(BuoyStation.provider)
    ).all()
    by_provider: dict[str, tuple[int, datetime | None]] = {p: (n, t) for p, n, t in rows}
    providers: list[ObservationProviderHealthOut] = []
    problems: list[Problem] = []
    for name in settings.observation_provider_list:
        stations, observed = by_provider.get(name, (0, None))
        status: Freshness
        if observed is None:
            status = "missing"
        elif now - observed > timedelta(hours=limit):
            status = "stale"
        else:
            status = "ok"
        providers.append(
            ObservationProviderHealthOut(
                provider=name,
                status=status,
                active_stations=stations,
                newest_observed_at=observed,
                age_hours=_hours(now - observed) if observed else None,
                stale_after_hours=limit,
            )
        )
        if status == "missing":
            problems.append(
                Problem(
                    f"observations_missing:{name}",
                    "degraded",
                    f"Buoy observations ({name}): none stored yet.",
                )
            )
        elif status == "stale" and observed is not None:
            problems.append(
                Problem(
                    f"observations_stale:{name}",
                    "degraded",
                    f"Buoy observations ({name}): the newest is from {stamp(observed)}, "
                    f"{ago(now - observed)} ago (stale after {limit:g} h).",
                )
            )
    return ObservationsHealthOut(enabled=True, providers=providers), problems


# --------------------------------------------------------------------------- storage
def _check_storage(db: Session, settings: Settings) -> tuple[StorageOut, list[Problem]]:
    """Database size against DATA_HEALTH_DATABASE_MAX_GB (no limit when unset).

    PostgreSQL cannot see the free space of its disk; size the limit from the disk and the
    growth figures in docs/operations.md, leaving room for backups and a day of growth.
    """
    size = int(db.scalar(select(func.pg_database_size(func.current_database()))) or 0)
    limit = settings.data_health_database_max_gb
    redis_out, problems = _check_redis(settings)
    out = StorageOut(database_bytes=size, database_max_gb=limit, redis=redis_out)
    if limit is not None and size > limit * 1024**3:
        problems.append(
            Problem(
                "database_size",
                "degraded",
                f"The database holds {size / 1024**3:.1f} GB, more than "
                f"DATA_HEALTH_DATABASE_MAX_GB ({limit:g} GB): check the retention settings and "
                "the free disk space.",
            )
        )
    return out, problems


def _check_redis(settings: Settings) -> tuple[RedisHealthOut, list[Problem]]:
    """Redis memory (INFO memory) against its cap and the eviction policy."""
    alert = settings.data_health_redis_max_fraction
    try:
        info = redis_memory()
    except redis.RedisError:
        return RedisHealthOut(available=False, alert_fraction=alert), [
            Problem(
                "redis_unavailable",
                "degraded",
                "Redis does not answer: background jobs, locks and call budgets depend on it.",
            )
        ]
    used, maxmemory = int(info["used_memory"]), int(info["maxmemory"])
    policy = str(info.get("maxmemory_policy") or "") or None
    cap = maxmemory or parse_memory_size(settings.redis_maxmemory) or None
    fraction = round(used / cap, 4) if cap else None
    out = RedisHealthOut(
        available=True,
        used_memory_bytes=used,
        maxmemory_bytes=maxmemory or None,
        maxmemory_policy=policy,
        cap_bytes=cap,
        cap_source=None if cap is None else "redis" if maxmemory else "setting",
        used_fraction=fraction,
        alert_fraction=alert,
    )
    problems: list[Problem] = []
    if fraction is not None and cap is not None and fraction > alert:
        problems.append(
            Problem(
                "redis_memory",
                "degraded",
                f"Redis memory: {used / 1024**2:.0f} MB used, {fraction:.0%} of the "
                f"{cap / 1024**2:.0f} MB cap (alert above {alert:.0%}). A full Redis refuses "
                "new jobs: check the queue lengths and the provider response cache.",
            )
        )
    if policy is not None and policy != "noeviction" and settings.runs_celery:
        # Only a Celery broker loses queued jobs to eviction; the daemon keeps no queue in
        # Redis (hosted Redis such as Upstash sets its own policy).
        problems.append(
            Problem(
                "redis_eviction_policy",
                "degraded",
                f"Redis maxmemory-policy is {policy}: under memory pressure it deletes keys, "
                "including queued jobs. Run it with --maxmemory-policy noeviction.",
            )
        )
    return out, problems


# --------------------------------------------------------------------------- sync
ACQUISITION_KINDS = ("spots", "ensemble", "map")


def _stored_runs(row: SyncRun, names: dict[str, str]) -> list[SyncStoredRunOut]:
    """The runs a sync stored: the acquisition stages' components with outcome "stored"
    (sync_runs.stages), with the plan's cycle of that run (else the run key's)."""
    cycles: dict[tuple[str, str | None], Any] = {}
    plan = row.plan if isinstance(row.plan, dict) else {}
    for comp in plan.get("components") or []:
        if isinstance(comp, dict) and comp.get("available_cycle"):
            cycles[(str(comp.get("source_code")), comp.get("available_run_key"))] = comp[
                "available_cycle"
            ]
    out: list[SyncStoredRunOut] = []
    for entry in row.stages or []:
        if not isinstance(entry, dict) or entry.get("name") not in ACQUISITION_KINDS:
            continue
        detail = entry.get("detail")
        for comp in (detail.get("components") if isinstance(detail, dict) else None) or []:
            if not isinstance(comp, dict) or comp.get("outcome") != "stored":
                continue
            source, run_key = str(comp.get("source_code")), comp.get("run_key")
            run_key = str(run_key) if run_key else None
            try:
                out.append(
                    SyncStoredRunOut(
                        kind=entry["name"],
                        source=source,
                        source_name=names.get(source),
                        run_key=run_key,
                        cycle=cycles.get((source, run_key)) or newest_cycle(run_key),
                    )
                )
            except ValidationError:
                continue
    return out


def _sync_run_out(row: SyncRun, names: dict[str, str] | None = None) -> SyncRunOut:
    """A sync_runs row for the public report: no error text, no stage details; the runs
    it stored, by component."""
    stages: list[SyncStageOut] = []
    for entry in row.stages or []:
        if not isinstance(entry, dict):
            continue
        try:
            cpu = entry.get("cpu_s")
            stages.append(
                SyncStageOut(
                    name=str(entry["name"]),
                    status=entry["status"],
                    duration_ms=int(entry.get("duration_ms") or 0),
                    cpu_s=float(cpu) if cpu is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            continue
    return SyncRunOut(
        id=row.id,
        trigger=row.trigger,  # type: ignore[arg-type]  # ck_sync_runs_trigger
        status=row.status,  # type: ignore[arg-type]  # ck_sync_runs_status
        started_at=row.started_at,
        finished_at=row.finished_at,
        published_at=row.published_at,
        anchor_cycle=row.anchor_cycle,
        stages=stages,
        stored=_stored_runs(row, names or {}),
    )


def source_names(db: Session, codes: Iterable[str]) -> dict[str, str]:
    """Source code -> name: forecast_sources.name, else KNOWN_SOURCE_NAMES (one query)."""
    wanted = {c for c in codes if c}
    if not wanted:
        return {}
    stored = dict(
        db.execute(
            select(ForecastSource.code, ForecastSource.name).where(ForecastSource.code.in_(wanted))
        ).all()
    )
    return {
        c: stored.get(c) or KNOWN_SOURCE_NAMES[c]
        for c in wanted
        if stored.get(c) or c in KNOWN_SOURCE_NAMES
    }


def components_since_check(
    raw_components: list[dict[str, Any]],
    checked_at: datetime | None,
    syncs: Iterable[SyncRun | None],
) -> list[dict[str, Any]]:
    """The daemon's last check (its heartbeat components) with what a sync that ended after
    it stored: a sync of another trigger (the CLI, GitHub Actions) while the daemon waits
    for its next check. A component whose newest available run such a sync stored, or
    found stored, is current. The daemon's own syncs are settled in its heartbeat already
    (app.services.sync.detect.settle_plan); a sync still running changes nothing (what it
    stored is not published before its publish stage)."""
    if checked_at is None:
        return raw_components
    stored: dict[tuple[str, str], tuple[int, str]] = {}
    for row in syncs:
        if row is None or row.status == "running" or row.finished_at is None:
            continue
        if row.finished_at <= checked_at:
            continue
        for entry in row.stages or []:
            detail = entry.get("detail") if isinstance(entry, dict) else None
            comps = detail.get("components") if isinstance(detail, dict) else None
            for comp in comps or []:
                if not isinstance(comp, dict) or comp.get("outcome") not in ("stored", "current"):
                    continue
                if comp.get("source_code") and comp.get("run_key"):
                    key = (str(comp["source_code"]), str(comp["run_key"]))
                    if key not in stored or comp["outcome"] == "stored":
                        stored[key] = (row.id, str(comp["outcome"]))
    if not stored:
        return raw_components
    out: list[dict[str, Any]] = []
    for raw in raw_components:
        if isinstance(raw, dict) and raw.get("state") not in ("current", "disabled"):
            hit = stored.get((str(raw.get("source_code")), str(raw.get("available_run_key"))))
            if hit is not None:
                sync_id, outcome = hit
                raw = {
                    **raw,
                    "state": "current",
                    "stored_run_key": raw["available_run_key"],
                    "detail": f"stored by sync {sync_id}" if outcome == "stored" else None,
                    "retry_after_s": None,
                    "attempts": 0,
                }
        out.append(raw)
    return out


def _component_out(
    raw: dict[str, Any], names: dict[str, str] | None = None
) -> SyncComponentOut | None:
    """One component of the daemon's last check (ComponentStatus.as_dict()). The detail of
    an "unknown" component is a provider's error text, and a failed one's after a sync ends
    with the stage's exception text: both are replaced by a plain statement."""
    try:
        component_state = raw["state"]
        detail = raw.get("detail")
        if detail and component_state == "unknown":
            detail = UNKNOWN_COMPONENT_DETAIL
        elif detail and component_state == "failed":
            failed = SYNC_FAILURE_DETAIL.match(str(detail))
            if failed:
                detail = SYNC_FAILURE_PUBLIC.format(sync=failed.group(1))
        source = str(raw["source_code"])
        return SyncComponentOut(
            kind=raw["kind"],
            source=source,
            source_name=(names or {}).get(source),
            provider=str(raw["provider"]),
            state=component_state,
            stored_run_key=raw.get("stored_run_key"),
            available_run_key=raw.get("available_run_key"),
            available_cycle=raw.get("available_cycle"),
            detail=redact(str(detail))[:DETAIL_MAX_CHARS] if detail else None,
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return None


def month_start(now: datetime) -> datetime:
    """00:00 UTC on the first day of ``now``'s UTC month."""
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def logged_cpu_seconds(db: Session, now: datetime) -> float:
    """CPU seconds in the job logs started this UTC month (``details.cpu_s``).

    Every sync stage is a job log of its own, so the stage CPU in ``sync_runs.stages`` is
    not added again. A Celery ``sync.tick`` that ran a sync is left out: its stages and its
    ``sync.run`` row count that CPU (the check's own planning seconds are not counted).
    Only the rows RETENTION_JOB_LOG_DAYS keeps are seen.
    """
    cpu = BackgroundJobLog.details["cpu_s"]
    ran_a_sync = and_(
        BackgroundJobLog.job_name == SYNC_TICK_JOB,
        BackgroundJobLog.details["sync_run_id"].astext.is_not(None),
    )
    total = db.scalar(
        select(func.coalesce(func.sum(cpu.astext.cast(Float)), 0.0)).where(
            BackgroundJobLog.started_at >= month_start(now),
            func.jsonb_typeof(cpu) == "number",
            ~ran_a_sync,
        )
    )
    return float(total or 0.0)


def combine_cpu_hours(
    settings: Settings,
    now: datetime,
    *,
    counted_s: float | None,
    logged_s: float,
    heartbeat: state.Heartbeat | None,
) -> float:
    """CPU-hours the job runner used this UTC month: a lower bound, never an estimate.

    Jobs: the larger of the job runner's Redis counter (``counted_s``, app.jobs.runner:
    every job that is not nested in another; lost if Redis loses its data; None when Redis
    did not answer) and the job logs (``logged_s``, :func:`logged_cpu_seconds`: only the
    retained rows). Both undercount, so the larger is the better bound. Under
    JOB_RUNNER=daemon the heartbeat of the same month adds the daemon's CPU outside jobs
    (checks, kick polls, start-up). Jobs of every runner count (the daemon, the CLI, the
    daily watchdog), so on a hosted deployment it can exceed the daemon's own use.
    """
    jobs = max(counted_s or 0.0, logged_s)
    outside = 0.0
    if (
        not settings.runs_celery
        and heartbeat is not None
        and heartbeat.cpu_s_month is not None
        and month_start(heartbeat.at) == month_start(now)
    ):
        outside = max(0.0, heartbeat.cpu_s_month)
    return round((jobs + outside) / 3600.0, 3)


def cpu_budget_problem(used_hours: float, budget_hours: float | None) -> Problem | None:
    """Degraded above CPU_BUDGET_ALERT_FRACTION of SYNC_CPU_BUDGET_HOURS (None: no budget)."""
    if budget_hours is None or used_hours <= CPU_BUDGET_ALERT_FRACTION * budget_hours:
        return None
    return Problem(
        "sync_cpu_budget",
        "degraded",
        f"Background jobs used at least {used_hours:.1f} CPU-hours this month (UTC), "
        f"{used_hours / budget_hours:.0%} of SYNC_CPU_BUDGET_HOURS ({budget_hours:g} h; alert "
        f"above {CPU_BUDGET_ALERT_FRACTION:.0%}). Once the runner's CPU quota is used up (a "
        "Vercel Daemon's monthly Active CPU), it stops until the next month.",
    )


def heartbeat_limit_minutes(settings: Settings) -> int:
    """How old the daemon's heartbeat may be: HEARTBEAT_CHECKS check intervals, or
    DATA_HEALTH_HEARTBEAT_MINUTES when that is longer."""
    return max(
        HEARTBEAT_CHECKS * settings.sync_check_minutes, settings.data_health_heartbeat_minutes
    )


def heartbeat_problem(
    settings: Settings,
    heartbeat: state.Heartbeat | None,
    now: datetime,
    *,
    sync_running: bool,
) -> Problem | None:
    """JOB_RUNNER=daemon: failing when the heartbeat is missing or older than
    :func:`heartbeat_limit_minutes`, unless a sync with a valid lease is running (a long
    sync holds up the checks; its lease, renewed after every stage, shows the daemon is
    alive). Only call it when Redis answered: otherwise the heartbeat cannot be read."""
    limit = heartbeat_limit_minutes(settings)
    if sync_running or (heartbeat is not None and now - heartbeat.at <= timedelta(minutes=limit)):
        return None
    if heartbeat is None:
        text = (
            "The sync daemon has written no heartbeat (none in Redis, or older than 6 h): it "
            "is not running"
        )
    else:
        text = (
            f"The sync daemon's last check was at {stamp(heartbeat.at)}, "
            f"{ago(now - heartbeat.at)} ago (limit {limit} min): it has stopped"
        )
    return Problem(
        "sync_heartbeat_stale",
        "failing",
        text + ", so new model runs are not synced and no periodic job runs. Check the daemon "
        "(on Vercel: the project's Daemons, and its CPU quota).",
    )


def failed_syncs_problem(finished: Sequence[SyncRun]) -> Problem | None:
    """Degraded when the newest SYNC_FAILURES_IN_A_ROW finished syncs (newest first) all
    failed or were abandoned. The error text goes to the admin email only."""
    newest = list(finished[:SYNC_FAILURES_IN_A_ROW])
    if len(newest) < SYNC_FAILURES_IN_A_ROW or any(
        r.status not in FAILED_SYNC_STATUSES for r in newest
    ):
        return None
    latest = newest[0]
    return Problem(
        "sync_failed",
        "degraded",
        f"The last {SYNC_FAILURES_IN_A_ROW} syncs failed (the newest, sync {latest.id}, started "
        f"at {stamp(latest.started_at)} and ended {latest.status}): new model runs are not "
        "being published.",
        detail=latest.error_message,
    )


def _sync_running(db: Session, now: datetime) -> bool:
    """A sync is in progress with a valid lease (renewed after every stage)."""
    row = db.scalar(
        select(SyncRun.id)
        .where(SyncRun.status == "running", SyncRun.lease_expires_at > now)
        .limit(1)
    )
    return row is not None


def _check_sync(
    db: Session,
    settings: Settings,
    now: datetime,
    jobs: list[JobHealthOut],
    redis_available: bool,
    heartbeat: state.Heartbeat | None,
) -> tuple[SyncHealthOut, list[Problem]]:
    """The sync block: runner liveness, the newest syncs, components and the CPU budget.
    ``heartbeat``: the daemon's (:func:`_read_heartbeat`)."""
    problems: list[Problem] = []
    last_check_at: datetime | None = None
    stale = False
    components: list[SyncComponentOut] = []
    if settings.runs_celery:
        last_check_at = db.scalar(
            select(BackgroundJobLog.started_at)
            .where(
                BackgroundJobLog.job_name == SYNC_TICK_JOB,
                BackgroundJobLog.status.in_((*REAL_RESULTS, JobStatus.RUNNING)),
            )
            .order_by(BackgroundJobLog.started_at.desc())
            .limit(1)
        )
        # Its overdue and stuck problems come from the job check (job_overdue:sync.tick).
        tick = next((j for j in jobs if j.job == SYNC_TICK_JOB), None)
        stale = tick is not None and tick.status in ("overdue", "stuck")
    else:
        if heartbeat is not None:
            last_check_at = heartbeat.at
        if redis_available:  # otherwise redis_unavailable explains it
            problem = heartbeat_problem(
                settings, heartbeat, now, sync_running=_sync_running(db, now)
            )
            if problem is not None:
                stale = True
                problems.append(problem)

    newest = db.scalar(
        select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)
    )
    published = latest_publication(db)
    raw_components = components_since_check(
        heartbeat.components if heartbeat is not None else [],
        heartbeat.at if heartbeat is not None else None,
        (newest, published),
    )
    codes = {
        str(raw["source_code"])
        for raw in raw_components
        if isinstance(raw, dict) and raw.get("source_code")
    }
    for row in (newest, published):
        for entry in (row.stages or []) if row is not None else []:
            detail = entry.get("detail") if isinstance(entry, dict) else None
            for comp in (detail or {}).get("components") or []:
                if isinstance(comp, dict) and comp.get("source_code"):
                    codes.add(str(comp["source_code"]))
    names = source_names(db, codes)
    components = [
        c for c in (_component_out(raw, names) for raw in raw_components) if c is not None
    ]
    finished = db.scalars(
        select(SyncRun)
        .where(SyncRun.status != "running")
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(SYNC_FAILURES_IN_A_ROW)
    ).all()
    failed = failed_syncs_problem(finished)
    if failed is not None:
        problems.append(failed)

    budget = settings.sync_cpu_budget_hours
    used = combine_cpu_hours(
        settings,
        now,
        counted_s=cpu_seconds_this_month(now) if redis_available else None,
        logged_s=logged_cpu_seconds(db, now),
        heartbeat=heartbeat,
    )
    over = cpu_budget_problem(used, budget)
    if over is not None:
        problems.append(over)
    out = SyncHealthOut(
        runner=settings.job_runner,
        check_minutes=settings.sync_check_minutes,
        last_check_at=last_check_at,
        heartbeat_stale=stale,
        last_run=_sync_run_out(newest, names) if newest is not None else None,
        last_published=_sync_run_out(published, names) if published is not None else None,
        components=components,
        cpu_hours_month=used,
        cpu_budget_hours=budget,
        cpu_alert_fraction=CPU_BUDGET_ALERT_FRACTION,
    )
    return out, problems


def _read_heartbeat(settings: Settings, redis_available: bool) -> state.Heartbeat | None:
    """The daemon's heartbeat under JOB_RUNNER=daemon (one billed Redis command per report
    on Upstash); under Celery a key left by an earlier daemon run says nothing about this
    runner, so it is not read."""
    if not redis_available or settings.runs_celery:
        return None
    return state.read_heartbeat()


# --------------------------------------------------------------------------- tides
def _check_tides(
    db: Session, settings: Settings, now: datetime, heartbeat: state.Heartbeat | None
) -> list[Problem]:
    """app.services.tides.health.tide_freshness. The constants file is read here when this
    process has it, and always under JOB_RUNNER=celery (the classic images ship data/);
    otherwise (the Vercel API function) the daemon's check from its heartbeat stands in."""
    local = settings.runs_celery or constants_file_present()
    raw = tide_freshness(db, now, check_constants=local)
    if not local and heartbeat is not None:
        raw = [*(heartbeat.file_problems or []), *raw]
    problems: list[Problem] = []
    for p in raw:
        severity = p.get("severity")
        if severity not in ("degraded", "failing") or not p.get("key"):
            continue
        detail = p.get("detail")
        problems.append(Problem(str(p["key"]), severity, str(p.get("message") or p["key"]), detail))
    return problems


# --------------------------------------------------------------------------- licence
def check_licence(settings: Settings) -> list[Problem]:
    """COMMERCIAL_USE with the free Open-Meteo API (an Open-Meteo source configured and no
    OPEN_METEO_API_KEY): failing, as the provider refuses to run."""
    configured = [t for t in settings.forecast_provider_list if t in OPEN_METEO_PROVIDER_TOKENS]
    if not configured or not free_api_refused(settings):
        return []
    return [
        Problem(
            "licence",
            "failing",
            "COMMERCIAL_USE=true, but the Open-Meteo sources ("
            + ", ".join(configured)
            + ") have no OPEN_METEO_API_KEY: the free Open-Meteo API is for non-commercial "
            "use only, so these sources are not fetched. Set OPEN_METEO_API_KEY (an "
            "Open-Meteo API subscription) or remove them from FORECAST_PROVIDERS.",
        )
    ]


# --------------------------------------------------------------------------- report
def overall_status(problems: Iterable[Problem]) -> OverallStatus:
    severities = {p.severity for p in problems}
    if "failing" in severities:
        return "failing"
    return "degraded" if severities else "ok"


def data_health(
    db: Session, now: datetime | None = None, settings: Settings | None = None
) -> HealthReport:
    """The data-health report (read-only; a handful of indexed queries)."""
    settings = settings or get_settings()
    now = now or utcnow()
    sources, source_problems = _check_sources(db, settings, now, building=_blend_building(db, now))
    jobs, heartbeat, job_problems = _check_jobs(db, settings, now)
    map_fields, map_problems = _check_map_fields(db, settings, now)
    observations, obs_problems = _check_observations(db, settings, now)
    storage, storage_problems = _check_storage(db, settings)
    redis_available = storage.redis is not None and storage.redis.available
    daemon_beat = _read_heartbeat(settings, redis_available)
    sync, sync_problems = _check_sync(db, settings, now, jobs, redis_available, daemon_beat)
    licence_problems = check_licence(settings)
    tide_problems = _check_tides(db, settings, now, daemon_beat)
    problems = [
        *source_problems,
        *job_problems,
        *map_problems,
        *obs_problems,
        *storage_problems,
        *sync_problems,
        *licence_problems,
        *tide_problems,
    ]
    problems.sort(key=lambda p: (p.severity != "failing", p.key))
    out = DataHealthOut(
        status=overall_status(problems),
        checked_at=now,
        reasons=[p.message for p in problems],
        problems=[
            HealthProblemOut(key=p.key, severity=p.severity, message=p.message) for p in problems
        ],
        forecast_sources=sources,
        jobs=jobs,
        map_fields=map_fields,
        observations=observations,
        heartbeat=heartbeat,
        storage=storage,
        sync=sync,
    )
    return HealthReport(out=out, problems=problems)


def main() -> int:
    """``python -m app.services.health``: print the report; exit 0 ok, 1 degraded, 2 failing."""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        report = data_health(db)
    print(report.out.model_dump_json(indent=2))
    return {"ok": 0, "degraded": 1, "failing": 2}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
