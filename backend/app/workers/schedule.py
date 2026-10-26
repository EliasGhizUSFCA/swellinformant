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

from celery.schedules import crontab

from app.core.config import Settings
from app.jobs import schedule as jobs
from app.jobs.schedule import (
    BULK_QUEUE,
    DATA_HEALTH_TASK,
    DEFAULT_QUEUE,
    QUEUES,
    SYNC_TICK_TIME_LIMIT_SECONDS,
    TASK_SOFT_TIME_LIMIT_SECONDS,
    TASK_TIME_LIMIT_SECONDS,
    DailyAt,
    Every,
    Schedule,
    queue_for,
    task_routes,
)

__all__ = [
    "BULK_QUEUE",
    "DATA_HEALTH_TASK",
    "DEFAULT_QUEUE",
    "QUEUES",
    "SYNC_TICK_TIME_LIMIT_SECONDS",
    "TASK_SOFT_TIME_LIMIT_SECONDS",
    "TASK_TIME_LIMIT_SECONDS",
    "build_beat_schedule",
    "celery_beat_schedule",
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


def build_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every periodic job for these settings (entry name -> task and schedule)."""
    return {
        name: {"task": entry.task, "schedule": to_celery(entry.schedule)}
        for name, entry in jobs.build_schedule(settings).items()
    }


def celery_beat_schedule(settings: Settings) -> dict[str, dict[str, Any]]:
    """:func:`build_beat_schedule` as handed to Celery: each entry's message expires after
    its own interval (``options["expires"]``, :func:`app.jobs.schedule.max_interval`).

    When the worker is down, beat keeps sending (notifications.deliver every 2 min); without
    an expiry the queue would grow without bound and replay every missed run on restart.
    """
    return {
        name: {
            "task": entry.task,
            "schedule": to_celery(entry.schedule),
            "options": {"expires": jobs.max_interval(entry.schedule).total_seconds()},
        }
        for name, entry in jobs.build_schedule(settings).items()
    }
