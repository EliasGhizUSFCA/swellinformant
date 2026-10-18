"""The periodic jobs, the pipeline chain and the queues, in plain types (no Celery).

:func:`build_schedule` lists every periodic job for a settings object as :class:`JobEntry`
values with an :data:`Schedule` (:class:`Every` N minutes or :class:`DailyAt` fixed UTC
times). Celery beat gets them through the adapter in app.workers.schedule; the data-health
report (app.services.health) reads them here, so the API never imports Celery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatchcase

from app.core.config import Settings

# Hard and soft per-task time limits (celery_app.conf). A job log still "running" well
# after the hard limit belongs to a worker process that was killed mid-job.
TASK_TIME_LIMIT_SECONDS = 1800
TASK_SOFT_TIME_LIMIT_SECONDS = 1500

# Each successful stage queues the next one (app.jobs.runner.run_job, ``on_success``).
NEXT_STAGE = {
    "forecasts.acquire": "forecasts.blend",
    "forecasts.blend": "quality.predict",
    "quality.predict": "detection.detect",
    "detection.detect": "matching.match",
    "matching.match": "flights.discover",
    "flights.discover": "alerts.generate",
    "alerts.generate": "notifications.deliver",
}

DATA_HEALTH_TASK = "ops.data_health"

# Queues. Long downloads and bulk data jobs go to "bulk", so account emails, alert delivery
# and the per-user stages never wait behind a multi-minute acquisition. The default worker
# consumes both (WORKER_QUEUES=celery,bulk, docker-entrypoint.sh); a second worker can take
# "bulk" alone (docker-compose.yml, profile "bulk-worker").
DEFAULT_QUEUE = "celery"
BULK_QUEUE = "bulk"
# Every queue a task can be routed to: a worker started without -Q consumes all of them.
QUEUES = (DEFAULT_QUEUE, BULK_QUEUE)
BULK_TASK_PATTERNS = ("forecasts.*", "observations.*", "verification.*", "tides.*")
# GEFS-Wave mean/spread files of a cycle are complete about 7 h after it (ensemble.py).
GEFS_JOB_DELAY = timedelta(hours=7, minutes=40)


@dataclass(frozen=True)
class Every:
    """Every ``minutes`` minutes (the first run one interval after the scheduler starts)."""

    minutes: float


@dataclass(frozen=True)
class DailyAt:
    """Every day at ``hours`` (UTC) and ``minute``."""

    hours: tuple[int, ...]
    minute: int


Schedule = Every | DailyAt


@dataclass(frozen=True)
class JobEntry:
    """One periodic job: the entry name (e.g. "data-health"), the task and its schedule."""

    name: str
    task: str
    schedule: Schedule


def task_routes() -> dict[str, dict[str, str]]:
    """celery_app.conf.task_routes: glob pattern -> queue (anything else: DEFAULT_QUEUE)."""
    return {pattern: {"queue": BULK_QUEUE} for pattern in BULK_TASK_PATTERNS}


def queue_for(task_name: str) -> str:
    """The queue a task is routed to (same rule as :func:`task_routes`)."""
    if any(fnmatchcase(task_name, p) for p in BULK_TASK_PATTERNS):
        return BULK_QUEUE
    return DEFAULT_QUEUE


def map_fields_standalone(settings: Settings) -> bool:
    # Imported here: app.services.map_fields pulls in the models.
    from app.services.map_fields import map_fields_mode

    return map_fields_mode(settings) == "standalone"


def optional_jobs(settings: Settings) -> dict[str, JobEntry]:
    """Jobs that only run when their feature setting is on (most download extra data)."""
    jobs: list[JobEntry] = []
    if map_fields_standalone(settings):
        # Same interval as forecast-acquisition (every 3 h by default), so both look for a
        # new GFS cycle at the same moments; a cycle already stored is skipped cheaply.
        jobs.append(
            JobEntry("map-fields", "forecasts.map_fields", Every(settings.forecast_refresh_minutes))
        )
    if settings.gefs_enabled:
        # GEFS-Wave output is complete ~7 h after the cycle: one run per GEFS_CYCLES cycle
        # (00/06/12/18Z by default, i.e. 01:40, 07:40, 13:40 and 19:40 UTC).
        delay = int(GEFS_JOB_DELAY.total_seconds() // 60)
        starts = sorted((h * 60 + delay) % 1440 for h in settings.gefs_cycle_hours)
        jobs.append(
            JobEntry(
                "ensemble-acquisition",
                "forecasts.acquire_ensemble",
                DailyAt(tuple(m // 60 for m in starts), delay % 60),
            )
        )
    if settings.tide_provider_list:
        # Tide predictions change slowly: one daily request (or local computation) per station.
        jobs.append(JobEntry("tide-acquisition", "tides.acquire", DailyAt((2,), 20)))
    if settings.observations_enabled:
        jobs.append(JobEntry("observation-ingest", "observations.ingest", Every(60)))
    if settings.verification_enabled:
        jobs.append(
            JobEntry(
                "point-forecast-archive",
                "verification.collect_point_forecasts",
                DailyAt((0, 6, 12, 18), 50),
            )
        )
        jobs.append(JobEntry("verification", "verification.run", DailyAt((4,), 10)))
    return {job.name: job for job in jobs}


def build_schedule(settings: Settings) -> dict[str, JobEntry]:
    """Every periodic job for these settings (entry name -> job)."""
    jobs = [
        JobEntry(
            "forecast-acquisition", "forecasts.acquire", Every(settings.forecast_refresh_minutes)
        ),
        JobEntry("quality-prediction-safety-net", "quality.predict", Every(30)),
        JobEntry("swell-detection", "detection.detect", Every(60)),
        JobEntry("user-matching", "matching.match", Every(30)),
        JobEntry("flight-discovery", "flights.discover", Every(60)),
        JobEntry("alert-generation", "alerts.generate", Every(15)),
        JobEntry("notification-delivery", "notifications.deliver", Every(2)),
        JobEntry("forecast-staleness-check", "forecasts.check_staleness", Every(60)),
        JobEntry("data-health", DATA_HEALTH_TASK, Every(settings.data_health_check_minutes)),
        JobEntry("maintenance-cleanup", "maintenance.cleanup", DailyAt((3,), 30)),
    ]
    return {job.name: job for job in jobs} | optional_jobs(settings)


def max_interval(schedule: Schedule) -> timedelta:
    """Longest gap between two consecutive runs: the interval, or for fixed daily times the
    largest gap between consecutive times of a day (24 h for one time a day)."""
    if isinstance(schedule, Every):
        return timedelta(minutes=schedule.minutes)
    if isinstance(schedule, DailyAt):
        times = sorted(h * 60 + schedule.minute for h in set(schedule.hours))
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        gaps.append(times[0] + 24 * 60 - times[-1])
        return timedelta(minutes=max(gaps))
    raise TypeError(f"unsupported schedule {schedule!r}")


def describe_schedule(schedule: Schedule) -> str:
    """Plain description of a schedule (times in UTC)."""
    if isinstance(schedule, Every):
        minutes = schedule.minutes
        if minutes >= 60 and minutes % 60 == 0:
            hours = int(minutes // 60)
            return "every hour" if hours == 1 else f"every {hours} h"
        return f"every {minutes:g} min"
    if isinstance(schedule, DailyAt):
        times = ", ".join(f"{h:02d}:{schedule.minute:02d}" for h in sorted(set(schedule.hours)))
        return f"daily at {times} UTC"
    raise TypeError(f"unsupported schedule {schedule!r}")
