"""Celery application (the beat schedule itself is built in app.workers.schedule).

Start a worker:     celery -A app.workers.celery_app worker -Q celery,bulk --loglevel=INFO
Start the scheduler: celery -A app.workers.celery_app beat --loglevel=INFO

Queues: data acquisition (forecasts.*, observations.*, verification.*, tides.*) runs on the
"bulk" queue, everything else (account emails, alert delivery, matching, ...) on the default
"celery" queue (app.workers.schedule.task_routes). A worker must consume both: the default
WORKER_QUEUES=celery,bulk does (docker-entrypoint.sh), and so does a worker started without
-Q (both queues are declared in task_queues). Beat messages expire after their own
interval (schedule.celery_beat_schedule).

Reliability settings: tasks are acknowledged only after they finish (acks_late) and are
re-queued if the worker process dies (reject_on_worker_lost); every task is idempotent
and serialised by a Redis lock, so a redelivered task is harmless.
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import (
    after_setup_logger,
    after_setup_task_logger,
    beat_init,
    worker_process_init,
)
from kombu import Queue

from app.core.config import get_settings
from app.core.logging import configure_logging, install_redaction
from app.workers.schedule import (
    DEFAULT_QUEUE,
    QUEUES,
    TASK_SOFT_TIME_LIMIT_SECONDS,
    TASK_TIME_LIMIT_SECONDS,
    celery_beat_schedule,
    map_fields_standalone,
    optional_schedule,
    task_routes,
)

settings = get_settings()


def _optional_schedule() -> dict[str, dict[str, object]]:
    """The feature-flagged jobs for this module's settings (schedule.optional_schedule)."""
    return optional_schedule(settings)


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
    task_time_limit=TASK_TIME_LIMIT_SECONDS,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
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
    task_default_queue=DEFAULT_QUEUE,
    # Declared so a worker started without -Q (README, local development) consumes every
    # queue; WORKER_QUEUES / -Q narrows it (docker-entrypoint.sh).
    task_queues=tuple(Queue(name) for name in QUEUES),
    task_routes=task_routes(),
    beat_schedule=celery_beat_schedule(settings),
)


@after_setup_logger.connect
def _redact_celery_logs(logger: logging.Logger, **_: object) -> None:
    configure_logging(add_handler=False)


@after_setup_task_logger.connect
def _redact_task_logs(logger: logging.Logger, **_: object) -> None:
    install_redaction(logger)


@beat_init.connect
def _kick_off_pipeline(**_: object) -> None:
    """Interval schedules first fire one interval after beat starts; on a fresh stack that
    would mean hours without forecasts. Queue one acquisition (which chains through every
    stage) as soon as the scheduler starts, the map-field job in standalone mode and the
    daily tide job (otherwise a deployment that gains it would wait until 02:20 UTC, and
    the data-health check would meanwhile report it as never run). The job lock makes a
    duplicate run harmless."""
    if get_settings().beat_run_on_start:
        celery_app.send_task("forecasts.acquire")
        if map_fields_standalone(get_settings()):
            celery_app.send_task("forecasts.map_fields")
        if get_settings().tide_provider_list:
            celery_app.send_task("tides.acquire")


@worker_process_init.connect
def _init_worker(**_: object) -> None:
    """Each forked worker process opens its own database and Redis connections."""
    from app.core.database import reset_engine
    from app.core.redis import reset_redis

    configure_logging(add_handler=False)
    reset_engine()
    reset_redis()
