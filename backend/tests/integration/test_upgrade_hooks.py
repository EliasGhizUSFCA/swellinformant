"""Wiring of the forecast upgrade: the tide job (beat entry, beat start, Celery task), the
sync check at beat start, the cleanup and data-health hooks, and the map-field route's new
names and parameter."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from celery.schedules import crontab
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.services import health
from app.services.maintenance import cleanup
from app.workers import tasks
from app.workers.celery_app import _kick_off_pipeline, celery_app
from app.workers.schedule import build_beat_schedule
from tests.conftest import ApiClient

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
    # Every job also records the CPU time it used (app.jobs.runner, "cpu_s").
    cpu_s = result["cpu_s"]
    assert isinstance(cpu_s, float) and cpu_s >= 0
    assert result == {"status": "success", "stations": 0, "cpu_s": cpu_s} and calls == [None]
    db.expire_all()
    row = db.scalars(
        select(BackgroundJobLog).where(BackgroundJobLog.job_name == "tides.acquire")
    ).one()
    assert row.status == JobStatus.SUCCESS and row.details == {"stations": 0, "cpu_s": cpu_s}


def test_cleanup_prunes_tide_predictions(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.maintenance.prune_tide_predictions", lambda db, now: 7)
    assert cleanup(db)["tide_predictions"] == 7


def test_data_health_reports_tide_problems(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    problem = {"key": "tides_stale:test", "severity": "degraded", "message": "Tides are old."}
    monkeypatch.setattr(health, "tide_freshness", lambda db, now: [problem])
    report = health.data_health(db)
    assert "Tides are old." in report.out.reasons
    assert any(p.key == "tides_stale:test" for p in report.problems)


def test_beat_start_queues_the_tide_job(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, *a, **kw: sent.append(name))
    monkeypatch.setattr(get_settings(), "beat_run_on_start", True)
    _kick_off_pipeline()
    # D14: beat start queues the sync check (was forecasts.acquire, plus forecasts.map_fields
    # in standalone mode); the sync acquires whatever has a new model run.
    assert sent.count("tides.acquire") == 1 and sent.count("sync.tick") == 1
    assert not {"forecasts.acquire", "forecasts.map_fields"} & set(sent)
    sent.clear()
    monkeypatch.setattr(get_settings(), "map_fields_enabled", True)
    _kick_off_pipeline()
    assert sent.count("sync.tick") == 1 and "forecasts.map_fields" not in sent
    sent.clear()
    monkeypatch.setattr(get_settings(), "tide_providers", "")
    _kick_off_pipeline()
    assert "tides.acquire" not in sent and sent == ["sync.tick"]
    sent.clear()
    monkeypatch.setattr(get_settings(), "beat_run_on_start", False)
    _kick_off_pipeline()
    assert sent == []


def test_a_deployment_that_gains_the_tide_job_is_not_left_overdue(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An existing deployment: background jobs have been logged for three days, and the new
    # daily tides.acquire job has never run, which data health reports as overdue.
    now = utcnow()
    db.add(
        BackgroundJobLog(
            job_name="maintenance.cleanup",
            status=JobStatus.SUCCESS,
            started_at=now - timedelta(days=3),
            finished_at=now - timedelta(days=3),
            details={},
        )
    )
    db.commit()
    assert "job_overdue:tides.acquire" in {p.key for p in health.data_health(db, now).problems}
    # Beat start queues the job (see the test above); once the worker has run it, the
    # report has nothing to say about it.
    monkeypatch.setattr("app.services.tides.ingest.acquire_tides", lambda db, now=None: {})
    tasks.acquire_tides_task.delay().get()
    db.expire_all()
    keys = {p.key for p in health.data_health(db, utcnow()).problems}
    assert not any(k.endswith(":tides.acquire") for k in keys)


def test_the_map_field_route_takes_partition_names_and_a_run(app_client: ApiClient) -> None:
    # No fields are stored here: a known variable is "not found", an unknown one invalid.
    for path in ("swell1_hs", "windsea_dir", "hs?run=noaa_gfs_fields:2026101400"):
        resp = app_client.get(f"/api/map/fields/{path}")
        assert resp.status_code == 404, (path, resp.text)
    assert app_client.get("/api/map/fields/swell3_hs").status_code == 422
    assert app_client.get("/api/map/fields/hs", params={"run": "x" * 121}).status_code == 422
