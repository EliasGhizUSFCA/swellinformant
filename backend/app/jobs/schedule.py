"""The periodic jobs, the pipeline chain and the queues, in plain types (no Celery).

:func:`build_schedule` lists every periodic job for a settings object as :class:`JobEntry`
values with an :data:`Schedule` (:class:`Every` N minutes or :class:`DailyAt` fixed UTC
times). Celery beat gets them through the adapter in app.workers.schedule, the sync daemon
runs them on its own timers (app.services.sync.daemon, every entry but ``sync.tick``), and
the data-health report (app.services.health) reads them here, so the API never imports
Celery.

The synced model (design D3, D14): forecast data is acquired when a provider publishes a
new model run, by one coordinated sync (app.services.sync), not on fixed timers. So the
schedule has no forecast-acquisition, map-field, ensemble, prediction or detection entries
any more; Celery beat runs the sync check instead (:data:`SYNC_TICK_ENTRY`, every
SYNC_CHECK_MINUTES), and the daemon checks on its own. The remaining interval jobs run at
least JOB_INTERVAL_FLOOR_MINUTES apart (0 = as listed), so a hosted deployment wakes its
database at most hourly between syncs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from fnmatch import fnmatchcase

from app.core.config import Settings
from app.services.sync.types import SYNC_TICK_JOB

# Hard and soft per-task time limits (celery_app.conf). A job log still "running" well
# after the hard limit belongs to a worker process that was killed mid-job.
TASK_TIME_LIMIT_SECONDS = 1800
TASK_SOFT_TIME_LIMIT_SECONDS = 1500
# Celery's sync.tick runs a whole sync (every stage, map layers included) in one task.
SYNC_TICK_TIME_LIMIT_SECONDS = 3600
SYNC_TICK_SOFT_TIME_LIMIT_SECONDS = 3300
# Beat's entry for the sync check (the daemon skips it: it checks on its own).
SYNC_TICK_ENTRY = "sync-tick"

# Each successful stage queues the next one (app.jobs.runner.run_job, ``on_success``) when a
# job runs on its own: a manual forecasts.acquire, or matching.match from beat or a kick. A
# sync runs its own stage order (app.services.sync.orchestrator, ``chain=False``).
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
# sync.tick runs the downloads of a sync, so it goes to "bulk" too.
BULK_TASK_PATTERNS = (
    "forecasts.*",
    "observations.*",
    "verification.*",
    "tides.*",
    SYNC_TICK_JOB,
)
# GEFS-Wave mean/spread files of a cycle are complete about 7 h after it (ensemble.py).
# No longer a beat time (the sync acquires the ensemble once it is published); kept for
# the modules and docs that quote it.
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


def time_limit_seconds(task_name: str) -> int:
    """Hard Celery time limit of a task (sync.tick has its own, longer one)."""
    return SYNC_TICK_TIME_LIMIT_SECONDS if task_name == SYNC_TICK_JOB else TASK_TIME_LIMIT_SECONDS


def map_fields_standalone(settings: Settings) -> bool:
    # Imported here: app.services.map_fields pulls in the models.
    from app.services.map_fields import map_fields_mode

    return map_fields_mode(settings) == "standalone"


def optional_jobs(settings: Settings) -> dict[str, JobEntry]:
    """Jobs that only run when their feature setting is on (most download extra data).

    The map layers (MAP_FIELDS_ENABLED) and GEFS-Wave (GEFS_ENABLED) are no longer here:
    a sync acquires them when their provider publishes a new run.
    """
    jobs: list[JobEntry] = []
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


def apply_floor(schedule: Schedule, floor_minutes: float) -> Schedule:
    """``schedule`` with an :class:`Every` interval of at least ``floor_minutes`` (0 = none).
    Fixed daily times are kept as they are."""
    if isinstance(schedule, Every) and schedule.minutes < floor_minutes:
        return Every(floor_minutes)
    return schedule


def build_schedule(settings: Settings) -> dict[str, JobEntry]:
    """Every periodic job for these settings (entry name -> job).

    The first entry, :data:`SYNC_TICK_ENTRY`, is Celery beat's sync check (task
    ``sync.tick``: plan from the providers' metadata, sync when a new model run is out).
    Every interval is at least JOB_INTERVAL_FLOOR_MINUTES.
    """
    jobs = [
        JobEntry(SYNC_TICK_ENTRY, SYNC_TICK_JOB, Every(settings.sync_check_minutes)),
        JobEntry("user-matching", "matching.match", Every(30)),
        JobEntry("flight-discovery", "flights.discover", Every(60)),
        JobEntry("alert-generation", "alerts.generate", Every(15)),
        JobEntry("notification-delivery", "notifications.deliver", Every(2)),
        JobEntry("forecast-staleness-check", "forecasts.check_staleness", Every(60)),
        JobEntry("data-health", DATA_HEALTH_TASK, Every(settings.data_health_check_minutes)),
        JobEntry("maintenance-cleanup", "maintenance.cleanup", DailyAt((3,), 30)),
    ]
    floor = settings.job_interval_floor_minutes
    every = {job.name: job for job in jobs} | optional_jobs(settings)
    return {
        name: JobEntry(job.name, job.task, apply_floor(job.schedule, floor))
        for name, job in every.items()
    }


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
