"""The beat schedule and the pipeline chain, without creating the Celery application.

Kept apart from :mod:`app.workers.celery_app` so the data-health report
(app.services.health) can read the expected jobs and their intervals for any settings
object, e.g. in the API process or in tests, without importing Celery's app instance.
"""

from __future__ import annotations

from datetime import timedelta
from fnmatch import fnmatchcase
from typing import Any

from celery.schedules import crontab, schedule

from app.core.config import Settings

# Hard and soft per-task time limits (celery_app.conf). A job log still "running" well
# after the hard limit belongs to a worker process that was killed mid-job.
TASK_TIME_LIMIT_SECONDS = 1800
TASK_SOFT_TIME_LIMIT_SECONDS = 1500

# Each successful stage queues the next one (app.workers.tasks.run_job).
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
BULK_TASK_PATTERNS = ("forecasts.*", "observations.*", "verification.*", "tides.*")
# GEFS-Wave mean/spread files of a cycle are complete about 7 h after it (ensemble.py).
GEFS_JOB_DELAY = timedelta(hours=7, minutes=40)


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


def optional_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Jobs that only run when their feature setting is on (most download extra data)."""
    jobs: dict[str, dict[str, Any]] = {}
    if map_fields_standalone(settings):
        # Same interval as forecast-acquisition (every 3 h by default), so both look for a
        # new GFS cycle at the same moments; a cycle already stored is skipped cheaply.
        jobs["map-fields"] = {
            "task": "forecasts.map_fields",
            "schedule": timedelta(minutes=settings.forecast_refresh_minutes),
        }
    if settings.gefs_enabled:
        # GEFS-Wave output is complete ~7 h after the cycle: one run per GEFS_CYCLES cycle
        # (00/06/12/18Z by default, i.e. 01:40, 07:40, 13:40 and 19:40 UTC).
        delay = int(GEFS_JOB_DELAY.total_seconds() // 60)
        starts = sorted((h * 60 + delay) % 1440 for h in settings.gefs_cycle_hours)
        jobs["ensemble-acquisition"] = {
            "task": "forecasts.acquire_ensemble",
            "schedule": crontab(hour=",".join(str(m // 60) for m in starts), minute=delay % 60),
        }
    if settings.tide_provider_list:
        # Tide predictions change slowly: one daily request (or local computation) per station.
        jobs["tide-acquisition"] = {
            "task": "tides.acquire",
            "schedule": crontab(hour=2, minute=20),
        }
    if settings.observations_enabled:
        jobs["observation-ingest"] = {
            "task": "observations.ingest",
            "schedule": timedelta(hours=1),
        }
    if settings.verification_enabled:
        jobs["point-forecast-archive"] = {
            "task": "verification.collect_point_forecasts",
            "schedule": crontab(hour="0,6,12,18", minute=50),
        }
        jobs["verification"] = {
            "task": "verification.run",
            "schedule": crontab(hour=4, minute=10),
        }
    return jobs


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every periodic job for these settings (entry name -> task and schedule)."""
    return _beat_entries(settings)


def celery_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """:func:`build_beat_schedule` as handed to Celery: each entry's message expires after
    its own interval (``options["expires"]``).

    When the worker is down, beat keeps sending (notifications.deliver every 2 min); without
    an expiry the queue would grow without bound and replay every missed run on restart.
    """
    entries = build_beat_schedule(settings)
    for entry in entries.values():
        expires = max_interval(entry["schedule"]).total_seconds()
        entry["options"] = {**entry.get("options", {}), "expires": expires}
    return entries


def _beat_entries(settings: Settings) -> dict[str, dict[str, Any]]:
    return {
        "forecast-acquisition": {
            "task": "forecasts.acquire",
            "schedule": timedelta(minutes=settings.forecast_refresh_minutes),
        },
        "quality-prediction-safety-net": {
            "task": "quality.predict",
            "schedule": timedelta(minutes=30),
        },
        "swell-detection": {"task": "detection.detect", "schedule": timedelta(hours=1)},
        "user-matching": {"task": "matching.match", "schedule": timedelta(minutes=30)},
        "flight-discovery": {"task": "flights.discover", "schedule": timedelta(hours=1)},
        "alert-generation": {"task": "alerts.generate", "schedule": timedelta(minutes=15)},
        "notification-delivery": {
            "task": "notifications.deliver",
            "schedule": timedelta(minutes=2),
        },
        "forecast-staleness-check": {
            "task": "forecasts.check_staleness",
            "schedule": timedelta(hours=1),
        },
        "data-health": {
            "task": DATA_HEALTH_TASK,
            "schedule": timedelta(minutes=settings.data_health_check_minutes),
        },
        "maintenance-cleanup": {
            "task": "maintenance.cleanup",
            "schedule": crontab(hour=3, minute=30),
        },
        **optional_schedule(settings),
    }


def max_interval(entry_schedule: Any) -> timedelta:
    """Longest gap between two consecutive runs of a beat schedule entry.

    Interval schedules: the interval. Crontab schedules: the largest gap between
    consecutive firing times of a day (e.g. 24 h for a daily job, 6 h for 1,7,13,19 h),
    or 7 days for a crontab restricted to some days (not used by this app's schedule).
    """
    if isinstance(entry_schedule, timedelta):
        return entry_schedule
    if isinstance(entry_schedule, crontab):
        if (
            len(entry_schedule.day_of_week) < 7
            or len(entry_schedule.day_of_month) < 31
            or len(entry_schedule.month_of_year) < 12
        ):
            return timedelta(days=7)
        times = sorted(h * 60 + m for h in entry_schedule.hour for m in entry_schedule.minute)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        gaps.append(times[0] + 24 * 60 - times[-1])
        return timedelta(minutes=max(gaps))
    if isinstance(entry_schedule, schedule):
        return entry_schedule.run_every
    raise TypeError(f"unsupported beat schedule {entry_schedule!r}")


def describe_schedule(entry_schedule: Any) -> str:
    """Plain description of a beat schedule entry (times in UTC)."""
    if isinstance(entry_schedule, timedelta):
        minutes = entry_schedule.total_seconds() / 60
        if minutes >= 60 and minutes % 60 == 0:
            hours = int(minutes // 60)
            return "every hour" if hours == 1 else f"every {hours} h"
        return f"every {minutes:g} min"
    if isinstance(entry_schedule, crontab):
        cron_hours = sorted(entry_schedule.hour)
        cron_minutes = sorted(entry_schedule.minute)
        if len(cron_minutes) == 1:
            times = ", ".join(f"{h:02d}:{cron_minutes[0]:02d}" for h in cron_hours)
            return f"daily at {times} UTC"
        return f"cron minute {cron_minutes} hour {cron_hours} (UTC)"
    if isinstance(entry_schedule, schedule):
        return describe_schedule(entry_schedule.run_every)
    return str(entry_schedule)
