"""Celery adapter of the job schedule (the schedule itself is app.jobs.schedule).

:func:`build_beat_schedule` hands :func:`app.jobs.schedule.build_schedule` to Celery beat
in its own shapes (``timedelta`` for :class:`Every`, ``crontab`` for :class:`DailyAt`),
including the ``sync-tick`` entry (task ``sync.tick``: the sync check, which runs a sync
when a provider has published a new model run). The data-health report and the API read
app.jobs.schedule directly and never import Celery; this module is for the worker and beat
only.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery.schedules import crontab, schedule

from app.core.config import Settings
from app.jobs import schedule as jobs
from app.jobs.schedule import (
    BULK_QUEUE,
    BULK_TASK_PATTERNS,
    DATA_HEALTH_TASK,
    DEFAULT_QUEUE,
    GEFS_JOB_DELAY,
    NEXT_STAGE,
    QUEUES,
    SYNC_TICK_ENTRY,
    SYNC_TICK_SOFT_TIME_LIMIT_SECONDS,
    SYNC_TICK_TIME_LIMIT_SECONDS,
    TASK_SOFT_TIME_LIMIT_SECONDS,
    TASK_TIME_LIMIT_SECONDS,
    DailyAt,
    Every,
    JobEntry,
    Schedule,
    map_fields_standalone,
    queue_for,
    task_routes,
)

__all__ = [
    "BULK_QUEUE",
    "BULK_TASK_PATTERNS",
    "DATA_HEALTH_TASK",
    "DEFAULT_QUEUE",
    "GEFS_JOB_DELAY",
    "NEXT_STAGE",
    "QUEUES",
    "SYNC_TICK_ENTRY",
    "SYNC_TICK_SOFT_TIME_LIMIT_SECONDS",
    "SYNC_TICK_TIME_LIMIT_SECONDS",
    "TASK_SOFT_TIME_LIMIT_SECONDS",
    "TASK_TIME_LIMIT_SECONDS",
    "build_beat_schedule",
    "celery_beat_schedule",
    "describe_schedule",
    "map_fields_standalone",
    "max_interval",
    "optional_schedule",
    "queue_for",
    "task_routes",
    "to_celery",
]


def to_celery(entry_schedule: Schedule) -> timedelta | crontab:
    """A plain schedule as Celery beat takes it."""
    if isinstance(entry_schedule, Every):
        return timedelta(minutes=entry_schedule.minutes)
    if isinstance(entry_schedule, DailyAt):
        return crontab(
            hour=",".join(str(h) for h in entry_schedule.hours), minute=entry_schedule.minute
        )
    raise TypeError(f"unsupported schedule {entry_schedule!r}")


def _beat_entries(entries: dict[str, JobEntry]) -> dict[str, dict[str, Any]]:
    return {
        name: {"task": entry.task, "schedule": to_celery(entry.schedule)}
        for name, entry in entries.items()
    }


def optional_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Jobs that only run when their feature setting is on, in beat's shape."""
    return _beat_entries(jobs.optional_jobs(settings))


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every periodic job for these settings (entry name -> task and schedule)."""
    return _beat_entries(jobs.build_schedule(settings))


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


def max_interval(entry_schedule: Any) -> timedelta:
    """Longest gap between two consecutive runs of a beat schedule entry.

    Interval schedules: the interval. Crontab schedules: the largest gap between
    consecutive firing times of a day (e.g. 24 h for a daily job, 6 h for 1,7,13,19 h),
    or 7 days for a crontab restricted to some days (not used by this app's schedule).
    Plain schedules (app.jobs.schedule) are accepted too.
    """
    if isinstance(entry_schedule, Every | DailyAt):
        return jobs.max_interval(entry_schedule)
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
    if isinstance(entry_schedule, Every | DailyAt):
        return jobs.describe_schedule(entry_schedule)
    if isinstance(entry_schedule, timedelta):
        return jobs.describe_schedule(Every(entry_schedule.total_seconds() / 60))
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
