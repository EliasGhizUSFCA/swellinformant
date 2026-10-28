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

**Admin problems** (design D12; :func:`admin_problems`, ``data_health(admin=True)``): the
checks below are computed only for the ``ops.data_health`` job and the admin freshness
dashboard (GET /api/admin/freshness), never for the public report, whose payload and cost
stay as they were. ops_alerts emails them to verified admins with the same
de-duplication, reminders and recovery notices as every other problem:

* ``model_run_late:<component>``: no newer run of the wave or atmosphere model is known
  (published, staged, or seen by the daemon's last check) although the next cycle's
  expected availability has passed: the newest run's init time + the model's update
  interval (Open-Meteo meta.json ``update_interval_seconds`` where the version recorded it,
  else 6 h) + its typical lag measured on the recent versions (median of
  ``available_at - init_time`` of the runs whose domains share one cycle) + a grace of
  max(MODEL_RUN_LATE_GRACE, the measured spread of the lag). No measured lag, no
  judgement: never a fixed wall-clock guess.
* ``forecast_stale_spots``: active spots of the published version whose forecast is stale
  (their refresh failed, or a component is older than its model's interval plus
  FORECAST_STALE_THRESHOLD_MINUTES; app.services.forecasts.versions.spot_freshness).
* ``refresh_failures``: the newest REFRESH_FAILURES_IN_A_ROW (3) or more finished spot
  refresh syncs all failed or were abandoned (map-only syncs and skipped checks ignored).
* ``provider_credentials:<provider>``: the provider refused the key (HTTP 401/403, Windy's
  "Invalid API key") or answered with a Windy TESTING key, in the newest attempt of its
  source or in the recorded provider health (the providers package).
* ``provider_unavailable:<source>``: the provider's run metadata has been unreadable for
  PROVIDER_UNAVAILABLE_AFTER (30 min): new runs cannot be detected. When the metadata was
  first seen unknown is kept in Redis (METADATA_UNKNOWN_KEY) by these checks.
* ``provider_fallback:<provider>``: Open-Meteo stands in for Windy (the provider manager's
  recorded fallback, or a published version with ``fallback_from``).
* ``quota_near:<counter>``: QUOTA_ALERT_FRACTION (0.8) of the Open-Meteo day or month
  window, Windy's daily requests or the flight provider's FLIGHT_DAILY_QUOTA is used
  (app.services.usage).
* ``refresh_scheduler_silent``: no refresh check (the refresh endpoint or the daily
  backstop, a daemon that checks spots, Celery's sync.tick, or a sync they started) for
  REFRESH_SCHEDULER_EXPECTED_MINUTES (30; 0 disables it), at least 3 x SYNC_CHECK_MINUTES
  when Celery or such a daemon is the runner, and not while a sync they started runs under
  a valid lease. Not checked while FORECAST_REFRESH_ENABLED=false.

Assumptions: the limits above are operational heuristics (how late a run may be before
someone should look), not statements about forecast accuracy. A worker that is down
cannot send its own alert: poll this endpoint from an external monitor as well
(docs/operations.md).
"""

from __future__ import annotations

import importlib
import logging
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import redis
from pydantic import ValidationError
from sqlalchemy import Float, String, and_, column, func, select, true, values
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session, defer

from app.core.config import OPEN_METEO_PROVIDER_TOKENS, Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.core.redis import get_redis, parse_memory_size, redis_memory
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
from app.models import (
    BackgroundJobLog,
    BuoyObservation,
    BuoyStation,
    ForecastRun,
    ForecastSource,
    SurfSpot,
)
from app.models.enums import ForecastRunStatus, JobStatus
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
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
from app.services.forecasts import versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.open_meteo import free_api_refused
from app.services.forecasts.queries import BLEND_CODE, USABLE, is_stale, stale_after_hours
from app.services.forecasts.registry import (
    GEFS_WAVE_CODE,
    KNOWN_SOURCE_NAMES,
    SOURCE_CODES,
    configured_source_codes,
)
from app.services.map_fields import map_fields_mode, served_run
from app.services.sync import state
from app.services.sync.detect import newest_cycle
from app.services.sync.published import latest_publication
from app.services.sync.types import STAGE_JOB_NAMES, SYNC_OWNED_TASKS, SYNC_TICK_JOB
from app.services.tides.health import constants_file_present, tide_freshness
from app.services.usage import RECENT_RUNS, usage_report

logger = logging.getLogger(__name__)

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
    """``out`` and ``problems``: the public report. ``admin_problems``: the admin-only
    problems (design D12), computed with ``data_health(admin=True)`` only and never put
    into ``out``."""

    out: DataHealthOut
    problems: list[Problem]
    admin_problems: list[Problem] = field(default_factory=list)

    @property
    def status(self) -> OverallStatus:
        return self.out.status

    @property
    def all_problems(self) -> list[Problem]:
        """The public and the admin problems, the most severe first (what admins are
        alerted about)."""
        return sorted(
            [*self.problems, *self.admin_problems], key=lambda p: (p.severity != "failing", p.key)
        )

    @property
    def alert_status(self) -> OverallStatus:
        """The overall status of :attr:`all_problems` (the admin emails' status)."""
        return overall_status(self.all_problems)


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


# --------------------------------------------------------------------------- admin problems
# Design D12, see the module docstring. The grace past a model's expected availability
# covers Open-Meteo's servers disagreeing about the same run (62 min apart for one GFS 0.25
# run, measured 2026-10-26), the 10-minute servable wait and one refresh check interval.
MODEL_RUN_LATE_GRACE = timedelta(minutes=90)
VERSION_HISTORY = 30  # newest forecast versions read (by id): lags, detection, timeline
REFRESH_HISTORY = 20  # newest sync_runs read
REFRESH_FAILURES_IN_A_ROW = 3
PROVIDER_UNAVAILABLE_AFTER = timedelta(minutes=30)
# A check older than this says nothing about the provider now (the heartbeat's own TTL).
METADATA_OBSERVATION_MAX_AGE = timedelta(hours=6)
# Redis hash: source (or provider) -> ISO time its run metadata was first seen unknown.
METADATA_UNKNOWN_KEY = "ops:metadata-unknown:v1"
METADATA_UNKNOWN_TTL_SECONDS = 7 * 86400
STALE_SPOTS_NAMED = 8
# Keys (or key prefixes before ":") of the admin problems, and the one reported when their
# checks could not run (ops_alerts then keeps their previous state instead of recovering).
ADMIN_PROBLEM_KEYS = frozenset(
    {
        "model_run_late",
        "forecast_stale_spots",
        "refresh_failures",
        "provider_credentials",
        "provider_unavailable",
        "provider_fallback",
        "quota_near",
        "refresh_scheduler_silent",
    }
)
ADMIN_CHECKS_FAILED = "admin_checks_failed"
# Sync triggers that a scheduler starts (the refresh endpoint, the daemon, Celery's tick);
# a CLI or GitHub Actions sync proves nothing about the scheduler.
SCHEDULER_TRIGGERS = frozenset({"http", "daemon", "celery"})
CREDENTIAL_STATUSES = ("401", "403")
CREDENTIAL_TEXT = re.compile(
    r"refused the key|invalid api key|windy_testing_key|testing-key|unauthori[sz]ed|"
    r"forbidden|\bHTTP 40[13]\b",
    re.IGNORECASE,
)
_TOKEN_OF_SOURCE = {code: token for token, code in SOURCE_CODES.items()}


def _provider_manager() -> ModuleType | None:
    """``app.services.forecasts.provider_manager`` (the providers package, design D4), or
    None in a build without it: then the selection comes from the settings alone and no
    provider health is recorded."""
    try:
        return importlib.import_module("app.services.forecasts.provider_manager")
    except ImportError:
        return None


def _seconds(delta: timedelta) -> float:
    return delta.total_seconds()


def _minutes_text(seconds: float) -> str:
    return ago(timedelta(seconds=seconds))


@dataclass(frozen=True)
class ComponentTiming:
    """One component's model run timing (the dashboard's atmosphere / wave block and
    ``model_run_late``).

    ``run`` / ``available_at`` / ``retrieved_at`` describe the run the published version
    serves; ``newest_run`` is the newest init time known at all (any version, staged or
    failed included, or the daemon's last check). ``typical_lag_s`` is the median of
    ``available_at - init_time`` over the distinct runs of the recent versions whose
    domains share one cycle (:func:`_lag_sample`);
    ``expected_next_at`` = ``newest_run`` + interval + typical lag, and the run is late
    after ``late_after`` (+ max(MODEL_RUN_LATE_GRACE, measured spread))."""

    component: str
    model: str | None = None
    run_key: str | None = None
    run: datetime | None = None
    estimated: bool = False
    available_at: datetime | None = None
    retrieved_at: datetime | None = None
    changed: bool | None = None
    carried_from_run_id: int | None = None
    last_detected_at: datetime | None = None
    newest_run: datetime | None = None
    interval_s: int = versions.DEFAULT_UPDATE_INTERVAL_SECONDS
    interval_recorded: bool = False
    typical_lag_s: float | None = None
    lag_spread_s: float | None = None
    lag_samples: int = 0
    expected_next_at: datetime | None = None
    late_after: datetime | None = None
    late: bool = False


def _detected_runs(heartbeat: state.Heartbeat | None, now: datetime) -> dict[str, datetime]:
    """{component: newest available cycle} the daemon's last check saw for the spot
    sources (a component-less entry stands for both components)."""
    out: dict[str, datetime] = {}
    if heartbeat is None or now - heartbeat.at > METADATA_OBSERVATION_MAX_AGE:
        return out
    for raw in heartbeat.components or []:
        if not isinstance(raw, dict) or raw.get("kind") != "spots":
            continue
        try:
            cycle = datetime.fromisoformat(str(raw.get("available_cycle")))
        except ValueError:
            continue
        comps = [str(raw["component"])] if raw.get("component") else [ATMOSPHERE, WAVE]
        for comp in comps:
            if comp not in out or cycle > out[comp]:
                out[comp] = cycle
    return out


def _attempted_runs(db: Session, codes: Iterable[str]) -> dict[str, datetime]:
    """{component: newest model run} of the newest forecast runs (any status, the
    ``usage.RECENT_RUNS`` newest by id) of ``codes``: a run a refresh tried to fetch was
    detected, even when the attempt failed or is not published yet."""
    wanted = sorted({c for c in codes if c})
    if not wanted:
        return {}
    rows = db.execute(
        select(ForecastRun.wave_model_run_at, ForecastRun.atmosphere_model_run_at)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.code.in_(wanted))
        .order_by(ForecastRun.id.desc())
        .limit(RECENT_RUNS)
    ).all()
    out: dict[str, datetime] = {}
    for wave_at, wind_at in rows:
        for comp, at in ((WAVE, wave_at), (ATMOSPHERE, wind_at)):
            if at is not None and (comp not in out or at > out[comp]):
                out[comp] = at
    return out


def _lag_sample(run: versions.ComponentRun) -> float | None:
    """``available_at - init_time`` (seconds) of a run whose domains all hold the same
    cycle, else None. A run that mixes cycles (GFS 0.13 of 06Z with GFS 0.25 of 00Z while
    the 0.25 run is not out yet: a component is refreshed as soon as one domain advances)
    pairs its oldest domain's init time with its newest domain's availability, which
    would add a whole cycle to the measured lag."""
    if run.init_time is None or run.available_at is None or run.available_at < run.init_time:
        return None
    if any(d.init_time is not None and d.init_time != run.init_time for d in run.domains):
        return None
    return _seconds(run.available_at - run.init_time)


def component_timings(
    history: Sequence[ForecastVersion],
    current: ForecastVersion | None,
    now: datetime,
    settings: Settings,
    detected: Mapping[str, datetime] | None = None,
) -> dict[str, ComponentTiming]:
    """The atmosphere and wave :class:`ComponentTiming` from the recent versions
    (``history``, newest first) and the published version."""
    out: dict[str, ComponentTiming] = {}
    detected = detected or {}
    for comp in (ATMOSPHERE, WAVE):
        runs: dict[datetime, versions.ComponentRun] = {}
        lag_by_init: dict[datetime, float] = {}
        first_detected: dict[datetime, datetime] = {}
        for version in history:
            run = versions.component_runs_of(version).get(comp)
            if run is None or run.init_time is None:
                continue
            runs.setdefault(run.init_time, run)
            lag = _lag_sample(run)
            if lag is not None:
                lag_by_init.setdefault(run.init_time, lag)
            if version.detected_at is not None and run.changed:
                seen = first_detected.get(run.init_time)
                first_detected[run.init_time] = min(
                    seen or version.detected_at, version.detected_at
                )
        lags = sorted(lag_by_init.values())
        served = versions.component_runs_of(current).get(comp) if current is not None else None
        known = [*runs, *([detected[comp]] if comp in detected else [])]
        newest = max(known) if known else None
        reference = (runs.get(newest) if newest is not None else None) or served
        interval_recorded = bool(reference and reference.update_interval_seconds)
        interval = (
            int(reference.update_interval_seconds)
            if reference is not None and reference.update_interval_seconds
            else versions.DEFAULT_UPDATE_INTERVAL_SECONDS
        )
        typical = statistics.median(lags) if lags else None
        spread = (lags[-1] - typical) if typical is not None else None
        expected = late_after = None
        if typical is not None and newest is not None:
            expected = newest + timedelta(seconds=interval + typical)
            grace = max(MODEL_RUN_LATE_GRACE, timedelta(seconds=spread or 0.0))
            late_after = expected + grace
        out[comp] = ComponentTiming(
            component=comp,
            model=served.model if served else (reference.model if reference else None),
            run_key=served.run_key if served else None,
            run=served.init_time if served else None,
            estimated=bool(served and served.estimated),
            available_at=served.available_at if served else None,
            retrieved_at=served.retrieved_at if served else None,
            changed=served.changed if served else None,
            carried_from_run_id=served.carried_from_run_id if served else None,
            last_detected_at=(
                first_detected.get(served.init_time) if served and served.init_time else None
            ),
            newest_run=newest,
            interval_s=interval,
            interval_recorded=interval_recorded,
            typical_lag_s=typical,
            lag_spread_s=spread,
            lag_samples=len(lags),
            expected_next_at=expected,
            late_after=late_after,
            late=late_after is not None and now > late_after,
        )
    return out


def model_run_late_problems(
    timings: Mapping[str, ComponentTiming], now: datetime, settings: Settings
) -> list[Problem]:
    """``model_run_late:<component>`` for every late component (not while
    FORECAST_REFRESH_ENABLED=false: nothing is detected then)."""
    if not settings.forecast_refresh_enabled:
        return []
    problems: list[Problem] = []
    for comp, t in timings.items():
        if (
            not t.late
            or t.newest_run is None
            or t.typical_lag_s is None
            or t.expected_next_at is None
        ):
            continue
        interval = timedelta(seconds=t.interval_s)
        due = t.newest_run + interval
        missed = int((now - t.expected_next_at) / interval) + 1 if interval else 1
        more = f" (and {missed - 1} later cycle{'s' if missed > 2 else ''})" if missed > 1 else ""
        source = "meta.json update_interval_seconds" if t.interval_recorded else "assumed"
        problems.append(
            Problem(
                f"model_run_late:{comp}",
                "degraded",
                f"The {comp} model run of {stamp(due)}{more} is late ({t.model or 'model'}): its "
                f"runs usually become available {_minutes_text(t.typical_lag_s)} after their "
                f"cycle (median of {t.lag_samples} run{'s' if t.lag_samples != 1 else ''}; "
                f"updates every {_minutes_text(t.interval_s)}, {source}), so it was expected "
                f"by {stamp(t.expected_next_at)}; nothing newer than {stamp(t.newest_run)} has "
                f"been detected or published by {stamp(now)}. The provider is late, or the "
                "refresh cannot see or fetch it (check provider_unavailable and "
                "refresh_failures).",
            )
        )
    return problems


@dataclass(frozen=True)
class StaleSpot:
    spot_id: int
    slug: str | None
    name: str | None
    last_updated_at: datetime | None
    reason: str | None
    error: str | None = None


def stale_spots(
    db: Session, current: ForecastVersion | None, now: datetime, settings: Settings
) -> list[StaleSpot]:
    """The published version's stale active spots (versions.spot_freshness), oldest
    update first."""
    if current is None:
        return []
    freshness = versions.spot_freshness(current, now, settings)
    stale_ids = [sid for sid, f in freshness.items() if f.stale]
    if not stale_ids:
        return []
    names = {
        sid: (slug, name)
        for sid, slug, name in db.execute(
            select(SurfSpot.id, SurfSpot.slug, SurfSpot.name).where(
                SurfSpot.id.in_(stale_ids), SurfSpot.is_active.is_(True)
            )
        )
    }
    entries = versions.spot_runs_of(current)
    out = [
        StaleSpot(
            spot_id=sid,
            slug=names[sid][0],
            name=names[sid][1],
            last_updated_at=freshness[sid].last_updated_at,
            reason=freshness[sid].reason,
            error=entries[sid].error if sid in entries else None,
        )
        for sid in stale_ids
        if sid in names
    ]
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(out, key=lambda s: (s.last_updated_at or epoch, s.slug or ""))


def stale_spots_problem(
    spots: Sequence[StaleSpot], current: ForecastVersion | None
) -> Problem | None:
    if not spots or current is None:
        return None
    reasons = _count_text(s.reason or "stale" for s in spots)
    named = ", ".join(s.slug or str(s.spot_id) for s in spots[:STALE_SPOTS_NAMED])
    if len(spots) > STALE_SPOTS_NAMED:
        named += f" (+{len(spots) - STALE_SPOTS_NAMED} more)"
    errors = [f"{s.slug}: {s.error}" for s in spots if s.error][:5]
    return Problem(
        "forecast_stale_spots",
        "degraded",
        f"{len(spots)} of {current.spots_total} spots in published version {current.id} serve a "
        f"stale forecast ({reasons}): {named}. They are labelled stale on the site and keep "
        "their previous forecast until a refresh updates them.",
        detail="; ".join(errors)[:1000] or None,
    )


def _count_text(reasons: Iterable[str]) -> str:
    """'2 refresh failed, 1 wave age'."""
    counts: dict[str, int] = {}
    for r in reasons:
        counts[r] = counts.get(r, 0) + 1
    return ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in sorted(counts.items()))


def is_refresh_sync(row: SyncRun) -> bool:
    """A sync that refreshed (or tried to refresh) the spot forecasts: one the refresh
    endpoint started, one whose plan made spots due, or one with a spots stage. A map- or
    ensemble-only sync is not."""
    if row.trigger == "http":
        return True
    plan = row.plan if isinstance(row.plan, dict) else {}
    if "spots" in (plan.get("due_kinds") or []):
        return True
    stages = [s.get("name") for s in row.stages or [] if isinstance(s, dict)]
    if "spots" in stages:
        return True
    # Neither plan nor stages (a sync that failed before planning): counted.
    return not plan.get("due_kinds") and not stages


def refresh_failures_problem(syncs: Sequence[SyncRun]) -> Problem | None:
    """``refresh_failures``: the newest REFRESH_FAILURES_IN_A_ROW or more finished refresh
    syncs (``syncs`` newest first; skipped and running rows ignored) failed or were
    abandoned. The newest error goes to the admin email only."""
    finished = [r for r in syncs if r.status not in ("running", "skipped") and is_refresh_sync(r)]
    streak: list[SyncRun] = []
    for row in finished:
        if row.status not in FAILED_SYNC_STATUSES:
            break
        streak.append(row)
    if len(streak) < REFRESH_FAILURES_IN_A_ROW:
        return None
    newest, oldest = streak[0], streak[-1]
    # Every refresh read failed and older rows exist: the streak may be longer.
    more = "+" if len(streak) == len(finished) and len(syncs) >= REFRESH_HISTORY else ""
    return Problem(
        "refresh_failures",
        "degraded",
        f"The last {len(streak)}{more} forecast "
        f"refreshes failed (syncs {oldest.id}-{newest.id}, since {stamp(oldest.started_at)}; "
        f"the newest ended {newest.status}): new model runs are not being published and "
        "spots go stale.",
        detail=newest.error_message,
    )


@dataclass(frozen=True)
class CredentialFailure:
    provider: str  # FORECAST_PROVIDERS token (or the source code when it has none)
    source: str | None
    at: datetime | None
    kind: str  # credentials | testing_key
    detail: str | None


def _attempt_failures(db: Session, codes: Iterable[str]) -> list[CredentialFailure]:
    """Credential failures in the newest attempt of each source (``forecast_runs``): HTTP
    401/403 responses or a refused key / testing key in the error text."""
    wanted = sorted({c for c in codes if c})
    if not wanted:
        return []
    run = ForecastRun
    rows = db.execute(
        select(
            ForecastSource.code,
            run.status,
            run.started_at,
            run.error_message,
            run.details["stats"],
        )
        .join(ForecastSource, ForecastSource.id == run.source_id)
        .where(ForecastSource.code.in_(wanted))
        .order_by(run.source_id, run.started_at.desc(), run.id.desc())
        .ext(distinct_on(run.source_id))
    ).all()
    out: list[CredentialFailure] = []
    for code, status, started, error, stats in rows:
        if status != ForecastRunStatus.FAILED:
            continue
        counts = stats.get("http_status_counts") if isinstance(stats, dict) else None
        refused = [s for s in CREDENTIAL_STATUSES if isinstance(counts, dict) and counts.get(s)]
        text = str(error or "")
        if not refused and not CREDENTIAL_TEXT.search(text):
            continue
        testing = "testing" in text.lower() or "trial" in text.lower()
        out.append(
            CredentialFailure(
                provider=_TOKEN_OF_SOURCE.get(str(code), str(code)),
                source=str(code),
                at=started,
                kind="testing_key" if testing else "credentials",
                detail=redact(text)[:500] or (f"HTTP {', '.join(refused)}" if refused else None),
            )
        )
    return out


def _health_failures(health: Mapping[str, Mapping[str, Any]]) -> list[CredentialFailure]:
    """Credential and testing-key failures in the recorded provider health (the providers
    package) that no later success cleared."""
    out: list[CredentialFailure] = []
    for provider, h in health.items():
        kind = h.get("last_failure_kind")
        if kind not in ("credentials", "testing_key"):
            continue
        failed, ok = as_datetime(h.get("last_failure_at")), as_datetime(h.get("last_success_at"))
        if failed is None or (ok is not None and ok >= failed):
            continue
        out.append(
            CredentialFailure(
                provider=str(provider),
                source=None,
                at=failed,
                kind=str(kind),
                detail=_redacted(h.get("last_failure_detail")),
            )
        )
    return out


def _redacted(text: Any) -> str | None:
    """Recorded error text (another package's record) redacted and shortened."""
    return redact(str(text))[:DETAIL_MAX_CHARS] if text else None


def as_datetime(value: Any) -> datetime | None:
    """A datetime or ISO text as an aware datetime (None when empty or unreadable). A
    time without an offset (a record written by other code) is read as UTC, so it compares
    with the aware times here instead of raising."""
    if isinstance(value, datetime):
        parsed = value
    elif not value:
        return None
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def credential_problems(
    failures: Sequence[CredentialFailure], selection: Mapping[str, Any]
) -> list[Problem]:
    """``provider_credentials:<provider>``: failing, or degraded while a fallback stands in
    for that provider (the site still gets data, from the fallback)."""
    by_provider: dict[str, CredentialFailure] = {}
    for f in failures:
        known = by_provider.get(f.provider)
        if known is None or (f.at and (known.at is None or f.at > known.at)):
            by_provider[f.provider] = f
    standing_in = selection.get("fallback_from")
    problems: list[Problem] = []
    for provider, f in sorted(by_provider.items()):
        what = (
            "answered with a TESTING key (trial data are shuffled and are never stored or "
            "published)"
            if f.kind == "testing_key"
            else "refused the API key (invalid, expired or without the needed permission)"
        )
        fallback = (
            f"; {selection.get('fallback') or 'the fallback'} stands in meanwhile"
            if standing_in == provider
            else ""
        )
        problems.append(
            Problem(
                f"provider_credentials:{provider}",
                "degraded" if standing_in == provider else "failing",
                f"Forecast provider {provider} {what}"
                + (f" at {stamp(f.at)}" if f.at else "")
                + f"{fallback}. Check its key in the deployment's environment "
                "(docs/API_SETUP_REQUIRED.md; Windy: docs/WINDY_API_SETUP.md).",
                detail=f.detail,
            )
        )
    return problems


@dataclass(frozen=True)
class MetadataState:
    """Whether a provider's run metadata could be read at its newest check."""

    key: str  # the source code (":<component>" when the check named one) or provider token
    unknown: bool
    observed_at: datetime
    unknown_since: datetime | None = None
    detail: str | None = None


def metadata_observations(
    heartbeat: state.Heartbeat | None,
    refresh_check: Mapping[str, Any] | None,
    health: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> dict[str, MetadataState]:
    """The newest observation of each provider's metadata: the daemon's last check, the
    refresh endpoint's last check (when it records its components) and the recorded
    provider health (a transient failure after the last success = unknown)."""
    out: dict[str, MetadataState] = {}

    def note(key: str, unknown: bool, at: datetime | None, detail: str | None) -> None:
        if at is None or now - at > METADATA_OBSERVATION_MAX_AGE:
            return
        known = out.get(key)
        if known is None or at >= known.observed_at:
            out[key] = MetadataState(key, unknown, at, detail=detail)

    checks: list[tuple[datetime | None, list[Any]]] = []
    if heartbeat is not None:
        checks.append((heartbeat.at, list(heartbeat.components or [])))
    if refresh_check:
        checks.append(
            (as_datetime(refresh_check.get("at")), list(refresh_check.get("components") or []))
        )
    for at, components in checks:
        for raw in components:
            if not isinstance(raw, dict) or not raw.get("source_code"):
                continue
            if raw.get("state") in ("disabled", None):
                continue
            key = str(raw["source_code"]) + (f":{raw['component']}" if raw.get("component") else "")
            unknown = raw.get("state") == "unknown"
            detail = (
                redact(str(raw.get("detail")))[:DETAIL_MAX_CHARS] if raw.get("detail") else None
            )
            note(key, unknown, at, detail if unknown else None)
    for provider, h in health.items():
        failed, ok = as_datetime(h.get("last_failure_at")), as_datetime(h.get("last_success_at"))
        transient = h.get("last_failure_kind") == "unavailable"
        if transient and failed is not None and (ok is None or failed > ok):
            note(str(provider), True, failed, _redacted(h.get("last_failure_detail")))
        elif ok is not None:
            note(str(provider), False, ok, None)
    return out


def track_metadata(
    observed: Mapping[str, MetadataState], *, write: bool = True
) -> dict[str, MetadataState]:
    """``observed`` with ``unknown_since``: when each unknown provider was first seen
    unknown (kept in Redis across checks; a known one is forgotten). Without Redis,
    ``unknown_since`` is None (the duration is not known, so no problem is raised)."""
    try:
        client = get_redis()
        stored = {str(k): str(v) for k, v in (client.hgetall(METADATA_UNKNOWN_KEY) or {}).items()}
    except redis.RedisError as exc:
        logger.warning("Provider metadata state unavailable (Redis: %s)", type(exc).__name__)
        return dict(observed)
    out: dict[str, MetadataState] = {}
    new: dict[str, str] = {}
    gone: list[str] = []
    for key, obs in observed.items():
        if obs.unknown:
            since = as_datetime(stored.get(key))
            if since is None or since > obs.observed_at:
                since = obs.observed_at
                new[key] = since.isoformat()
            out[key] = MetadataState(key, True, obs.observed_at, since, obs.detail)
        else:
            out[key] = obs
            if key in stored:
                gone.append(key)
    if write and (new or gone):
        try:
            pipe = client.pipeline(transaction=True)
            for key, since_text in new.items():
                pipe.hset(METADATA_UNKNOWN_KEY, key, since_text)
            if gone:
                pipe.hdel(METADATA_UNKNOWN_KEY, *gone)
            pipe.expire(METADATA_UNKNOWN_KEY, METADATA_UNKNOWN_TTL_SECONDS)
            pipe.execute()
        except redis.RedisError as exc:
            logger.warning("Provider metadata state not stored (Redis: %s)", type(exc).__name__)
    return out


def provider_unavailable_problems(
    states: Mapping[str, MetadataState], now: datetime, names: Mapping[str, str] | None = None
) -> list[Problem]:
    """``provider_unavailable:<source>``: metadata unknown for PROVIDER_UNAVAILABLE_AFTER."""
    problems: list[Problem] = []
    for key, s in sorted(states.items()):
        if not s.unknown or s.unknown_since is None:
            continue
        if now - s.unknown_since < PROVIDER_UNAVAILABLE_AFTER:
            continue
        source = key.split(":", 1)[0]
        name = (names or {}).get(source) or source
        problems.append(
            Problem(
                f"provider_unavailable:{key}",
                "degraded",
                f"{name}: the provider's run metadata could not be read since "
                f"{stamp(s.unknown_since)} ({ago(now - s.unknown_since)}; last check "
                f"{stamp(s.observed_at)}): new model runs cannot be detected meanwhile.",
                detail=s.detail,
            )
        )
    return problems


def provider_fallback_problems(
    selection: Mapping[str, Any], current: ForecastVersion | None
) -> list[Problem]:
    """``provider_fallback:<provider>``: a fallback stands in for the configured primary
    (the provider manager's recorded fallback, or the published version's
    ``fallback_from``)."""
    replaced: dict[str, str] = {}
    if selection.get("fallback_from"):
        until = as_datetime(selection.get("fallback_until"))
        reasons = [str(r) for r in selection.get("reasons") or [] if "stands in" in str(r)]
        replaced[str(selection["fallback_from"])] = (
            (f"until {stamp(until)}" if until else "") + (f" ({reasons[-1]})" if reasons else "")
        ).strip()
    if current is not None and current.fallback_from:
        replaced.setdefault(
            str(current.fallback_from),
            f"published version {current.id} came from {current.provider}",
        )
    problems = []
    for provider, why in sorted(replaced.items()):
        problems.append(
            Problem(
                f"provider_fallback:{provider}",
                "degraded",
                f"{selection.get('fallback') or 'The fallback provider'} stands in for "
                f"{provider}" + (f": {why}" if why else "") + ". The site keeps updating "
                f"from the fallback; {provider} is tried again automatically.",
            )
        )
    return problems


def quota_near_problems(usage: Mapping[str, Any], settings: Settings) -> list[Problem]:
    """``quota_near:<counter>`` at QUOTA_ALERT_FRACTION of a provider allowance: the
    Open-Meteo day and month windows, Windy's day, the flight provider's daily quota."""
    alert = settings.quota_alert_fraction
    counters: list[tuple[str, str, Any, Any, str]] = []
    om = usage.get("open_meteo") or {}
    limits = om.get("limits") or {}
    resets = om.get("resets_at") or {}
    for window in ("day", "month"):
        counters.append(
            (
                f"open_meteo_{window}",
                f"Open-Meteo weighted calls this {window} (OPEN_METEO_CALLS_PER_{window.upper()})",
                om.get(window),
                limits.get(window),
                str(resets.get(window) or ""),
            )
        )
    windy = usage.get("windy") or {}
    if windy.get("counted", True):
        counters.append(
            (
                "windy_day",
                "Windy requests today (WINDY_DAILY_REQUEST_LIMIT)",
                windy.get("day"),
                windy.get("limit"),
                str(windy.get("resets_at") or ""),
            )
        )
    flights = usage.get("flights") or {}
    fq = flights.get("quota")
    if isinstance(fq, Mapping):
        counters.append(
            (
                f"flights_{flights.get('provider')}_day",
                f"{flights.get('provider')} flight requests today (FLIGHT_DAILY_QUOTA)",
                fq.get("used"),
                fq.get("limit"),
                str(fq.get("resets_at") or ""),
            )
        )
    problems = []
    for key, label, used, limit, resets_at in counters:
        if not isinstance(used, int | float) or not isinstance(limit, int | float) or limit <= 0:
            continue
        share = used / limit
        if share < alert:
            continue
        reset = as_datetime(resets_at)
        problems.append(
            Problem(
                f"quota_near:{key}",
                "degraded",
                f"{label}: {used:g} of {limit:g} used ({share:.0%}; alert at {alert:.0%}"
                f", QUOTA_ALERT_FRACTION)"
                + (f"; it resets at {stamp(reset)}" if reset else "")
                + ". Requests stop once the limit is reached.",
            )
        )
    return problems


@dataclass(frozen=True)
class SchedulerState:
    """When a refresh check last ran (``refresh_scheduler_silent``)."""

    expected_minutes: int
    enabled: bool  # FORECAST_REFRESH_ENABLED and REFRESH_SCHEDULER_EXPECTED_MINUTES > 0
    last_check_at: datetime | None
    last_check_by: str | None
    checks: dict[str, datetime | None]
    monitoring_since: datetime | None
    silent: bool
    # The silence limit applied (:func:`scheduler_limit_minutes`).
    limit_minutes: int = 0
    # A sync a scheduler started is running under a valid lease (a long one is no silence).
    sync_running: bool = False


def scheduler_limit_minutes(settings: Settings) -> int:
    """How long no refresh check may be seen: REFRESH_SCHEDULER_EXPECTED_MINUTES, and at
    least HEARTBEAT_CHECKS x SYNC_CHECK_MINUTES (as for the daemon's heartbeat) when the
    configured runner checks on its own timer (Celery's sync.tick, a daemon whose
    SYNC_DAEMON_KINDS include spots): a slower SYNC_CHECK_MINUTES is no silence."""
    expected = settings.refresh_scheduler_expected_minutes
    timed = settings.runs_celery or (
        settings.job_runner == "daemon" and "spots" in settings.sync_daemon_kind_list
    )
    if expected <= 0 or not timed:
        return expected
    return max(expected, HEARTBEAT_CHECKS * settings.sync_check_minutes)


def scheduler_state(
    db: Session,
    settings: Settings,
    now: datetime,
    *,
    heartbeat: state.Heartbeat | None,
    refresh_check: Mapping[str, Any] | None,
    syncs: Sequence[SyncRun],
) -> SchedulerState:
    """The newest refresh check of any scheduler: the refresh endpoint and the daily
    backstop (their Redis record), a daemon whose SYNC_DAEMON_KINDS include spots (its
    heartbeat), Celery's sync.tick (its job log) and the newest sync one of them started."""
    checks: dict[str, datetime | None] = {}
    if refresh_check:
        caller = str(refresh_check.get("caller") or "refresh")
        checks[f"refresh endpoint ({caller})"] = as_datetime(refresh_check.get("at"))
    if heartbeat is not None and "spots" in settings.sync_daemon_kind_list:
        checks["daemon"] = heartbeat.at
    if settings.runs_celery:
        checks["celery sync.tick"] = db.scalar(
            select(BackgroundJobLog.started_at)
            .where(BackgroundJobLog.job_name == SYNC_TICK_JOB)
            .order_by(BackgroundJobLog.started_at.desc())
            .limit(1)
        )
    started = next(
        (r for r in syncs if r.trigger in SCHEDULER_TRIGGERS and is_refresh_sync(r)), None
    )
    if started is not None:
        checks[f"sync {started.id} ({started.trigger})"] = started.started_at
    known = [(at, by) for by, at in checks.items() if at is not None]
    last_at, last_by = max(known) if known else (None, None)
    expected = settings.refresh_scheduler_expected_minutes
    limit = scheduler_limit_minutes(settings)
    enabled = expected > 0 and settings.forecast_refresh_enabled
    running = any(
        r.status == "running"
        and r.trigger in SCHEDULER_TRIGGERS
        and r.lease_expires_at is not None
        and r.lease_expires_at > now
        for r in syncs
    )
    # When monitoring began matters only before any check was ever recorded.
    since = (
        db.scalar(select(func.min(BackgroundJobLog.started_at)))
        if enabled and last_at is None
        else None
    )
    reference = last_at or since
    silent = (
        enabled
        and not running
        and reference is not None
        and now - reference > timedelta(minutes=limit)
    )
    return SchedulerState(
        expected_minutes=expected,
        enabled=enabled,
        last_check_at=last_at,
        last_check_by=last_by,
        checks=checks,
        monitoring_since=since,
        silent=bool(silent),
        limit_minutes=limit,
        sync_running=running,
    )


def scheduler_silent_problem(s: SchedulerState, now: datetime) -> Problem | None:
    if not s.silent:
        return None
    if s.last_check_at is None:
        assert s.monitoring_since is not None
        text = (
            f"No forecast refresh check has been recorded, although background jobs have been "
            f"logged for {ago(now - s.monitoring_since)}"
        )
    else:
        text = (
            f"The last forecast refresh check was at {stamp(s.last_check_at)} "
            f"({s.last_check_by}), {ago(now - s.last_check_at)} ago"
        )
    return Problem(
        "refresh_scheduler_silent",
        "degraded",
        text + f" (expected at least every {s.limit_minutes or s.expected_minutes} min, "
        "REFRESH_SCHEDULER_EXPECTED_MINUTES): new model runs are not picked up. Check the "
        "scheduler that calls /api/internal/refresh (QStash or cron-job.org, docs/sync.md), "
        "the daemon or Celery beat; set REFRESH_SCHEDULER_EXPECTED_MINUTES=0 if no scheduler "
        "is meant to run.",
    )


def provider_selection_state(settings: Settings, now: datetime) -> dict[str, Any]:
    """The active provider and why: the provider manager's view when the providers
    package provides it (recorded health and fallback included), else the settings
    selection with no fallback recorded. Keys: providers, primary, configured, source,
    weather_provider, windy_eligible, fallback, fallback_from, fallback_until, reasons,
    health ({provider: recorded health})."""
    manager = _provider_manager()
    if manager is not None:
        try:
            data = dict(manager.active_provider(settings, now).as_dict(now))
        except Exception as exc:  # never let the dashboard or the alert job fail on it
            logger.warning("Provider manager unavailable: %s", type(exc).__name__)
        else:
            data.setdefault("health", {})
            return data
    selection = settings.provider_selection()
    return {
        **selection.as_dict(),
        "configured": list(selection.providers),
        "fallback_from": None,
        "fallback_until": None,
        "health": {},
    }


def read_refresh_check() -> dict[str, Any] | None:
    """The refresh endpoint's last check (``state.read_refresh_check``, the refresh
    package), or None in a build without it."""
    reader = getattr(state, "read_refresh_check", None)
    if not callable(reader):
        return None
    data = reader()
    return dict(data) if isinstance(data, Mapping) else None


@dataclass
class AdminInputs:
    """What the admin problems and the freshness dashboard are computed from (one read
    per source; :func:`admin_inputs`)."""

    now: datetime
    history: list[ForecastVersion]
    current: ForecastVersion | None
    timings: dict[str, ComponentTiming]
    stale: list[StaleSpot]
    syncs: list[SyncRun]
    usage: dict[str, Any]
    selection: dict[str, Any]
    scheduler: SchedulerState
    metadata: dict[str, MetadataState]
    credential_failures: list[CredentialFailure]
    source_names: dict[str, str] = field(default_factory=dict)


def admin_inputs(
    db: Session,
    settings: Settings,
    now: datetime,
    *,
    heartbeat: state.Heartbeat | None = None,
    track: bool = True,
) -> AdminInputs:
    """Read everything the admin problems need: the newest VERSION_HISTORY versions
    (without their spot maps) and the published one, the newest REFRESH_HISTORY syncs,
    the usage counters, the provider selection and health, the refresh checks. ``track``:
    keep the provider-metadata state in Redis (:func:`track_metadata`)."""
    history = list(
        db.scalars(
            select(ForecastVersion)
            .options(defer(ForecastVersion.spot_runs))
            .order_by(ForecastVersion.id.desc())
            .limit(VERSION_HISTORY)
        ).all()
    )
    current = versions.current_version(db)
    syncs = list(
        db.scalars(
            select(SyncRun)
            .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
            .limit(REFRESH_HISTORY)
        ).all()
    )
    refresh_check = read_refresh_check()
    selection = provider_selection_state(settings, now)
    health = {
        str(k): v for k, v in (selection.get("health") or {}).items() if isinstance(v, Mapping)
    }
    codes = set(configured_source_codes(settings))
    if current is not None and current.source_code:
        codes.add(current.source_code)
    detected = _detected_runs(heartbeat, now)
    for comp, at in _attempted_runs(db, codes).items():
        if comp not in detected or at > detected[comp]:
            detected[comp] = at
    timings = component_timings(history, current, now, settings, detected)
    observed = metadata_observations(heartbeat, refresh_check, health, now)
    return AdminInputs(
        now=now,
        history=history,
        current=current,
        timings=timings,
        stale=stale_spots(db, current, now, settings),
        syncs=syncs,
        usage=usage_report(db, now, settings),
        selection=selection,
        scheduler=scheduler_state(
            db, settings, now, heartbeat=heartbeat, refresh_check=refresh_check, syncs=syncs
        ),
        metadata=track_metadata(observed, write=track),
        credential_failures=[*_attempt_failures(db, codes), *_health_failures(health)],
        source_names=source_names(db, {k.split(":", 1)[0] for k in observed} | codes),
    )


def evaluate_admin(inputs: AdminInputs, settings: Settings) -> list[Problem]:
    """The admin problems of :func:`admin_inputs`' data, the most severe first."""
    now = inputs.now
    problems: list[Problem] = [
        *model_run_late_problems(inputs.timings, now, settings),
        *credential_problems(inputs.credential_failures, inputs.selection),
        *provider_unavailable_problems(inputs.metadata, now, inputs.source_names),
        *provider_fallback_problems(inputs.selection, inputs.current),
        *quota_near_problems(inputs.usage, settings),
    ]
    for one in (
        stale_spots_problem(inputs.stale, inputs.current),
        refresh_failures_problem(inputs.syncs),
        scheduler_silent_problem(inputs.scheduler, now),
    ):
        if one is not None:
            problems.append(one)
    problems.sort(key=lambda p: (p.severity != "failing", p.key))
    return problems


def admin_problems(
    db: Session,
    now: datetime | None = None,
    settings: Settings | None = None,
    *,
    heartbeat: state.Heartbeat | None = None,
) -> list[Problem]:
    """The admin-only problems (design D12) at ``now``."""
    settings = settings or get_settings()
    now = now or utcnow()
    return evaluate_admin(admin_inputs(db, settings, now, heartbeat=heartbeat), settings)


def is_admin_problem_key(key: str) -> bool:
    """Whether ``key`` names one of the admin-only problems (design D12)."""
    return key.split(":", 1)[0] in ADMIN_PROBLEM_KEYS


# --------------------------------------------------------------------------- report
def overall_status(problems: Iterable[Problem]) -> OverallStatus:
    severities = {p.severity for p in problems}
    if "failing" in severities:
        return "failing"
    return "degraded" if severities else "ok"


def data_health(
    db: Session,
    now: datetime | None = None,
    settings: Settings | None = None,
    *,
    admin: bool = False,
) -> HealthReport:
    """The data-health report (read-only; a handful of indexed queries). ``admin``: also
    the admin-only problems (``HealthReport.admin_problems``; for the ops.data_health job
    and the admin dashboard, never the public report)."""
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
    extra: list[Problem] = []
    if admin:
        try:
            extra = admin_problems(db, now, settings, heartbeat=daemon_beat)
        except Exception as exc:  # the public checks and their alerts must still go out
            db.rollback()
            logger.exception("Admin data-health checks failed")
            extra = [
                Problem(
                    ADMIN_CHECKS_FAILED,
                    "degraded",
                    f"The admin checks (model runs, spots, refreshes, providers, quotas, "
                    f"scheduler) could not run ({type(exc).__name__}); see the API logs.",
                )
            ]
    return HealthReport(out=out, problems=problems, admin_problems=extra)


def main() -> int:
    """``python -m app.services.health``: print the report; exit 0 ok, 1 degraded, 2 failing."""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        report = data_health(db)
    print(report.out.model_dump_json(indent=2))
    return {"ok": 0, "degraded": 1, "failing": 2}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
