"""Wiring of the tide job and hooks: beat entry, Celery task, cleanup and data health."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from celery.schedules import crontab
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.services import health
from app.services.maintenance import cleanup
from app.workers import tasks
from app.workers.celery_app import celery_app
from app.workers.schedule import build_beat_schedule

pytestmark = pytest.mark.integration


def test_tide_acquisition_is_scheduled_only_when_tide_providers_are_set() -> None:
    entry = build_beat_schedule(Settings(tide_providers="noaa_coops"))["tide-acquisition"]
    assert entry["task"] == "tides.acquire" and entry["task"] in celery_app.tasks
    sched = entry["schedule"]
    assert isinstance(sched, crontab) and sched.hour == {2} and sched.minute == {20}
    assert "tide-acquisition" in build_beat_schedule(Settings())  # the default providers
    assert "tide-acquisition" not in build_beat_schedule(Settings(tide_providers=""))


def test_tides_acquire_task_runs_the_ingest_and_logs_it(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime | None] = []

    def fake(db: Session, now: datetime | None = None) -> dict[str, Any]:
        calls.append(now)
        return {"stations": 0}

    monkeypatch.setattr("app.services.tides.ingest.acquire_tides", fake)
    result = tasks.acquire_tides_task.delay().get()
    assert result == {"status": "success", "stations": 0} and calls == [None]
    db.expire_all()
    row = db.scalars(
        select(BackgroundJobLog).where(BackgroundJobLog.job_name == "tides.acquire")
    ).one()
    assert row.status == JobStatus.SUCCESS and row.details == {"stations": 0}


def test_cleanup_prunes_tide_predictions(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.maintenance.prune_tide_predictions", lambda db, now: 7)
    assert cleanup(db)["tide_predictions"] == 7


def test_data_health_reports_tide_problems(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    problem = {"key": "tides_stale:test", "severity": "degraded", "message": "Tides are old."}
    monkeypatch.setattr(health, "tide_freshness", lambda db, now: [problem])
    report = health.data_health(db)
    assert "Tides are old." in report.out.reasons
    assert any(p.key == "tides_stale:test" for p in report.problems)
