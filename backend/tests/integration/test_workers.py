"""Celery task execution: eager chaining, locks, failure logging and a real worker."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis import redis_lock
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.workers import tasks
from app.workers.celery_app import celery_app

pytestmark = pytest.mark.integration


def logs(db: Session) -> dict[str, list[BackgroundJobLog]]:
    out: dict[str, list[BackgroundJobLog]] = {}
    db.expire_all()
    for row in db.scalars(select(BackgroundJobLog).order_by(BackgroundJobLog.id)).all():
        out.setdefault(row.job_name, []).append(row)
    return out


def test_beat_schedule_references_registered_tasks() -> None:
    registered = set(celery_app.tasks)
    for entry in celery_app.conf.beat_schedule.values():
        assert entry["task"] in registered
    assert celery_app.conf.task_acks_late and celery_app.conf.task_reject_on_worker_lost


def test_acquisition_chains_through_every_stage(db: Session, few_spots: list[str]) -> None:
    result = tasks.acquire_forecasts.delay().get()
    assert result["status"] == "success"
    by_job = logs(db)
    for job in (
        "forecasts.acquire",
        "quality.predict",
        "detection.detect",
        "matching.match",
        "flights.discover",
        "alerts.generate",
        "notifications.deliver",
    ):
        assert job in by_job, job
        assert by_job[job][-1].status == JobStatus.SUCCESS
        assert by_job[job][-1].duration_ms is not None
    assert by_job["detection.detect"][-1].details["spots"] == len(few_spots)


def test_lock_prevents_concurrent_runs(db: Session) -> None:
    with redis_lock("job:detection.detect", ttl_seconds=30):
        result = tasks.detect_swells.delay().get()
    assert result["status"] == "skipped"
    assert logs(db)["detection.detect"][-1].status == JobStatus.SKIPPED


def test_failures_are_logged_without_secrets(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.swell_detection.service as detection

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("database exploded with api_key=SECRET123")

    monkeypatch.setattr(detection, "detect_events", boom)
    with pytest.raises(RuntimeError):
        tasks.detect_swells.delay().get()
    row = logs(db)["detection.detect"][-1]
    assert row.status == JobStatus.FAILED
    assert "exploded" in (row.error_message or "") and "SECRET123" not in (row.error_message or "")


def test_lock_expires_after_a_crashed_worker(db: Session) -> None:
    from app.core.redis import get_redis

    # A worker died holding the lock: the TTL guarantees it is released.
    get_redis().set("lock:job:matching.match", "dead-worker", ex=1)
    import time

    time.sleep(1.2)
    assert tasks.match_searches.delay().get()["status"] == "success"


def test_real_worker_consumes_from_redis(db: Session, few_spots: list[str]) -> None:
    """Runs a genuine Celery worker thread against the Redis broker (not eager mode)."""
    from celery.contrib.testing.worker import start_worker

    celery_app.conf.task_always_eager = False
    try:
        with start_worker(celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=30):
            result = tasks.predict_quality.apply_async()
            assert result.get(timeout=60)["status"] == "success"
    finally:
        celery_app.conf.task_always_eager = True
    assert "quality.predict" in logs(db)
