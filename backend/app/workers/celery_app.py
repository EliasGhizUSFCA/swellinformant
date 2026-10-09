"""Celery application and beat schedule.

Start a worker:     celery -A app.workers.celery_app worker --loglevel=INFO
Start the scheduler: celery -A app.workers.celery_app beat --loglevel=INFO

Reliability settings: tasks are acknowledged only after they finish (acks_late) and are
re-queued if the worker process dies (reject_on_worker_lost); every task is idempotent
and serialised by a Redis lock, so a redelivered task is harmless.
"""

from __future__ import annotations

from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery(
    "swell_travel_agent",
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=1800,
    task_soft_time_limit=1500,
    broker_connection_retry_on_startup=True,
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0.2,
        "interval_step": 0.3,
        "interval_max": 1,
    },
    broker_transport_options={"visibility_timeout": 3600},
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,
    beat_schedule={
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
        "maintenance-cleanup": {
            "task": "maintenance.cleanup",
            "schedule": crontab(hour=3, minute=30),
        },
    },
)


@worker_process_init.connect
def _init_worker(**_: object) -> None:
    """Each forked worker process opens its own database and Redis connections."""
    from app.core.database import reset_engine
    from app.core.redis import reset_redis

    configure_logging()
    reset_engine()
    reset_redis()
