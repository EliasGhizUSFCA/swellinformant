"""Data health: is the stack still producing fresh data, unattended?

:func:`data_health` builds the report served by ``GET /api/health/data`` and read by the
``ops.data_health`` job (app.services.notifications.ops_alerts), which emails admin users.
It checks, with plain reasons:

* **Forecast sources** (every FORECAST_PROVIDERS source, the blend when two independent
  model families are configured, GEFS-Wave when GEFS_ENABLED): the newest successful run,
  its age and the source's own stale limit (``queries.stale_after_hours``, ADR-015). A
  stale blend is only a problem of its own when every member is fresh and no acquisition
  or blend run is in progress (the chain rebuilds it right after the acquisition).
* **Scheduled jobs** (the beat schedule of the current settings, plus the chained blend
  stage): the last result from ``background_job_logs``, consecutive failures, overdue
  runs (no run started for ``2 x longest interval + 30 min``; a run in progress counts)
  and stuck runs (still "running" 15 min after the hard task time limit: the worker
  process was killed).
* **Map fields** (unless the mode is "off"): the served run's model cycle against the
  NOAA GFS stale limit, since the fields come from the same 6-hourly cycles.
* **Observations** (when OBSERVATIONS_ENABLED): the newest buoy observation per provider
  against DATA_HEALTH_OBSERVATION_STALE_HOURS (default: the ingestion look-back window).
* **Heartbeat**: the newest finished job of any kind (this check's own job excluded).
* **Storage**: the database size, against DATA_HEALTH_DATABASE_MAX_GB when that is set.

Overall status: ``failing`` when the product cannot serve fresh forecasts (no forecast
source is fresh) or no background job has finished for DATA_HEALTH_HEARTBEAT_MINUTES
(worker or scheduler down); ``degraded`` for any other problem; ``ok`` otherwise.

Assumptions: the limits above are operational heuristics (how late a run may be before
someone should look), not statements about forecast accuracy. A worker that is down
cannot send its own alert: poll this endpoint from an external monitor as well
(docs/operations.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import BackgroundJobLog, BuoyObservation, BuoyStation, ForecastRun, ForecastSource
from app.models.enums import JobStatus
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
    Severity,
    SourceHealthOut,
    SourceRole,
    StorageOut,
)
from app.services.forecasts.queries import USABLE, is_stale, stale_after_hours
from app.services.forecasts.registry import (
    GEFS_WAVE_CODE,
    NOAA_GFS_CODE,
    configured_source_codes,
)
from app.services.map_fields import map_fields_mode, servable_runs
from app.workers.schedule import (
    DATA_HEALTH_TASK,
    NEXT_STAGE,
    TASK_TIME_LIMIT_SECONDS,
    build_beat_schedule,
    describe_schedule,
    max_interval,
)

BLEND_CODE = "blend"  # app.services.forecasts.blend.BLEND_CODE (not imported: ecCodes)
# Jobs that (re)build the blend: forecasts.acquire, then the chained forecasts.blend stage.
BLEND_BUILDERS = frozenset({"forecasts.acquire", "forecasts.blend"})
OVERDUE_FACTOR = 2  # missed runs tolerated before a job counts as overdue
OVERDUE_GRACE = timedelta(minutes=30)  # queueing behind long jobs (2 worker slots)
STUCK_GRACE = timedelta(minutes=15)
RECENT_LOGS = 25  # job log rows read per job (newest first)


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
    return when.strftime("%Y-%m-%d %H:%M UTC")


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


def expected_jobs(settings: Settings) -> list[ExpectedJob]:
    """The beat schedule's jobs for these settings plus the chained-only stages."""
    jobs: dict[str, ExpectedJob] = {}
    for entry in build_beat_schedule(settings).values():
        name, sched = str(entry["task"]), entry["schedule"]
        gap = max_interval(sched)
        known = jobs.get(name)
        if known is None or (known.max_gap is not None and gap < known.max_gap):
            jobs[name] = ExpectedJob(name, describe_schedule(sched), gap)
    for parent, stage in NEXT_STAGE.items():
        if stage in jobs or (stage == "forecasts.blend" and not settings.forecast_blend_enabled):
            continue
        jobs[stage] = ExpectedJob(stage, f"after {parent}", None)
    return sorted(jobs.values(), key=lambda j: j.name)


def _job_health(
    db: Session, job: ExpectedJob, now: datetime, monitoring_since: datetime | None
) -> tuple[JobHealthOut, Problem | None]:
    rows = list(
        db.scalars(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == job.name)
            .order_by(BackgroundJobLog.started_at.desc(), BackgroundJobLog.id.desc())
            .limit(RECENT_LOGS)
        ).all()
    )
    finished = [r for r in rows if r.status != JobStatus.RUNNING]
    real = [r for r in finished if r.status in (JobStatus.SUCCESS, JobStatus.FAILED)]
    success = next((r for r in real if r.status == JobStatus.SUCCESS), None)
    if success is None and rows:
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
    newest = rows[0] if rows else None
    last = finished[0] if finished else None
    stuck_after = timedelta(seconds=TASK_TIME_LIMIT_SECONDS) + STUCK_GRACE

    status: str = "ok"
    problem: Problem | None = None
    if (
        newest is not None
        and newest.status == JobStatus.RUNNING
        and now - newest.started_at > stuck_after
    ):
        status = "stuck"
        problem = Problem(
            f"job_stuck:{job.name}",
            "degraded",
            f"Job {job.name} has been 'running' since {stamp(newest.started_at)} "
            f"({ago(now - newest.started_at)}), past the {TASK_TIME_LIMIT_SECONDS // 60}-minute "
            "task time limit: the worker process was probably killed (e.g. out of memory).",
        )
    elif real and real[0].status == JobStatus.FAILED:
        status = "failed"
        since = (
            f"last success {ago(now - success.started_at)} ago"
            if success is not None
            else "it has not succeeded yet"
        )
        more = "+" if failures == len(real) and len(rows) == RECENT_LOGS else ""
        runs = f"{failures}{more} run{'' if failures == 1 and not more else 's'}"
        problem = Problem(
            f"job_failed:{job.name}",
            "degraded",
            f"Job {job.name} failed at {stamp(real[0].started_at)} ({runs} in a row; {since}).",
            detail=real[0].error_message,
        )
    elif overdue_after is not None:
        # A run in progress (and not stuck, checked above) counts as a run: the job did
        # start. This also keeps ops.data_health from calling itself overdue while it runs
        # for the first time after an outage.
        starts = [r.started_at for r in real[:1]]
        if newest is not None and newest.status == JobStatus.RUNNING:
            starts.append(newest.started_at)
        reference = max(starts) if starts else None
        if reference is not None and now - reference > overdue_after:
            status = "overdue"
            problem = Problem(
                f"job_overdue:{job.name}",
                "degraded",
                f"Job {job.name} has not run for {ago(now - reference)} (schedule: "
                f"{job.schedule}): the scheduler (beat) or the worker may be down or "
                "saturated.",
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
    elif not real:
        status = "never_run"
    if status == "ok" and newest is not None and newest.status == JobStatus.RUNNING:
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
    for job in expected_jobs(settings):
        health, problem = _job_health(db, job, now, monitoring_since)
        out.append(health)
        if problem is not None:
            problems.append(problem)

    last_finished = db.scalar(
        select(func.max(BackgroundJobLog.finished_at)).where(
            BackgroundJobLog.job_name != DATA_HEALTH_TASK
        )
    )
    limit = settings.data_health_heartbeat_minutes
    heartbeat = HeartbeatOut(
        last_job_finished_at=last_finished,
        age_minutes=round((now - last_finished).total_seconds() / 60, 1) if last_finished else None,
        limit_minutes=limit,
    )
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
                "min; notifications.deliver alone runs every 2 min): the worker or the "
                "scheduler (beat) is down.",
            )
        )
    return out, heartbeat, problems


# --------------------------------------------------------------------------- map fields
def _check_map_fields(
    db: Session, settings: Settings, now: datetime
) -> tuple[MapFieldsHealthOut, list[Problem]]:
    from app.models.forecast_engine import MapField

    mode = map_fields_mode(settings)
    if mode == "off":
        return MapFieldsHealthOut(mode=mode, status="off"), []
    limit = stale_after_hours(NOAA_GFS_CODE, settings)
    best = servable_runs(db, limit=1)
    if not best:
        return MapFieldsHealthOut(mode=mode, status="missing", stale_after_hours=limit), [
            Problem(
                "map_fields_missing",
                "degraded",
                "Map ocean layers: no complete NOAA field set is stored yet, so the map "
                "shows no wave or wind layer.",
            )
        ]
    served = best[0]
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
    out = StorageOut(database_bytes=size, database_max_gb=limit)
    if limit is None or size <= limit * 1024**3:
        return out, []
    return out, [
        Problem(
            "database_size",
            "degraded",
            f"The database holds {size / 1024**3:.1f} GB, more than DATA_HEALTH_DATABASE_MAX_GB "
            f"({limit:g} GB): check the retention settings and the free disk space.",
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
    problems = [*source_problems, *job_problems, *map_problems, *obs_problems, *storage_problems]
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
