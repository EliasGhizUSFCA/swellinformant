"""Celery queues, beat expiry and the acquisition retry rules (app.workers)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry

from app.core.config import Settings
from app.jobs.registry import JOB_SPECS
from app.jobs.runner import judge_acquisition, judge_single
from app.jobs.schedule import DATA_HEALTH_TASK, DailyAt, Every, JobEntry, build_schedule
from app.models.enums import JobStatus
from app.workers import tasks
from app.workers.celery_app import celery_app
from app.workers.schedule import (
    BULK_QUEUE,
    DEFAULT_QUEUE,
    build_beat_schedule,
    celery_beat_schedule,
    max_interval,
    queue_for,
)


def routed(name: str) -> str:
    return str(celery_app.amqp.router.route({}, name)["queue"].name)


def every_job_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        gefs_enabled=True,
        observations_enabled=True,
        verification_enabled=True,
        map_fields_enabled=True,
    )


@pytest.mark.parametrize(
    "task", ["forecasts.acquire", "observations.ingest", "tides.acquire", "verification.run"]
)
def test_bulk_tasks_go_to_the_bulk_queue(task: str) -> None:
    assert routed(task) == queue_for(task) == BULK_QUEUE


@pytest.mark.parametrize(
    "task",
    [
        "notifications.send_account_email",
        "notifications.deliver",
        "matching.match",
        "ops.data_health",
    ],
)
def test_user_facing_tasks_stay_on_the_default_queue(task: str) -> None:
    """Account emails never wait behind a multi-minute download."""
    assert routed(task) == queue_for(task) == DEFAULT_QUEUE == "celery"


def test_every_scheduled_task_is_consumed_by_the_default_worker() -> None:
    settings = every_job_settings()
    consumed = set(settings.worker_queue_list)
    assert consumed == {"celery", "bulk"}
    names = {entry["task"] for entry in build_beat_schedule(settings).values()}
    names |= {"forecasts.blend", "notifications.send_account_email", "notifications.send_email"}
    for name in names:
        assert name in celery_app.tasks, name
        assert routed(name) in consumed, name


def test_a_worker_without_queue_options_consumes_both_queues() -> None:
    """README starts a development worker without -Q: it must still take bulk tasks."""
    declared = {q.name for q in celery_app.conf.task_queues}
    assert declared == {DEFAULT_QUEUE, BULK_QUEUE}
    assert set(celery_app.amqp.queues) >= declared


def test_every_beat_entry_expires_after_its_interval() -> None:
    schedule = celery_beat_schedule(every_job_settings())
    assert set(schedule) == set(build_beat_schedule(every_job_settings()))
    for name, entry in schedule.items():
        expires = entry["options"]["expires"]
        assert expires == max_interval(entry["schedule"]).total_seconds() > 0, name
    assert schedule["notification-delivery"]["options"]["expires"] == 120.0
    # The configured app carries the same options.
    assert all("expires" in e["options"] for e in celery_app.conf.beat_schedule.values())


def test_gefs_job_follows_the_configured_cycles() -> None:
    default = build_beat_schedule(every_job_settings())["ensemble-acquisition"]["schedule"]
    assert sorted(default.hour) == [1, 7, 13, 19] and sorted(default.minute) == [40]
    two = Settings(_env_file=None, gefs_enabled=True, gefs_cycles="00,12")  # type: ignore[call-arg]
    twice = build_beat_schedule(two)["ensemble-acquisition"]["schedule"]
    assert sorted(twice.hour) == [7, 19]


# --------------------------------------------------------------------------- job judging
def outcome(status: str, **kw: Any) -> dict[str, Any]:
    return {"source": "x", "status": status, "message": kw.pop("message", ""), **kw}


def test_an_all_failed_acquisition_is_a_failed_job() -> None:
    status, error = tasks.judge_acquisition(
        {"outcomes": [outcome("failed", message="quota"), outcome("failed", message="503")]}
    )
    assert status == JobStatus.FAILED and error is not None and "quota" in error
    assert tasks.judge_acquisition({"outcomes": [outcome("failed"), outcome("success")]}) == (
        JobStatus.SUCCESS,
        None,
    )
    assert tasks.judge_acquisition({"outcomes": [outcome("skipped")]})[0] == JobStatus.SUCCESS
    assert tasks.judge_single({"status": "failed", "message": "x"})[0] == JobStatus.FAILED
    assert tasks.judge_single({"skipped": "mode is off"})[0] == JobStatus.SUCCESS


def test_retry_countdown() -> None:
    assert [tasks.retry_countdown(n, []) for n in range(4)] == [120, 240, 480, 900]
    assert tasks.retry_countdown(0, [330.0, None]) == 330  # the provider's own wait
    assert tasks.retry_countdown(0, [5.0]) == 60
    assert tasks.retry_countdown(0, [7200.0]) == 3600


class FakeTask:
    name = "forecasts.acquire"

    def __init__(self, retries: int = 0, eager: bool = False) -> None:
        self.request = type("Req", (), {"is_eager": eager, "retries": retries})()
        self.retried: list[dict[str, Any]] = []

    def retry(self, **kw: Any) -> Retry:
        self.retried.append(kw)
        return Retry("retry")


def test_transient_failures_retry_with_a_countdown_and_permanent_ones_do_not() -> None:
    task = FakeTask()
    transient = {"outcomes": [outcome("failed", retryable=True, retry_after_s=None)]}
    with pytest.raises(Retry):
        tasks.retry_transient(task, transient)  # type: ignore[arg-type]
    assert task.retried == [{"countdown": 120, "max_retries": tasks.ACQUIRE_MAX_RETRIES}]
    permanent = {"outcomes": [outcome("failed", retryable=False)]}
    tasks.retry_transient(task, permanent)  # type: ignore[arg-type]
    assert len(task.retried) == 1
    exhausted = FakeTask(retries=tasks.ACQUIRE_MAX_RETRIES)
    tasks.retry_transient(exhausted, transient)  # type: ignore[arg-type]
    eager = FakeTask(eager=True)
    tasks.retry_transient(eager, transient)  # type: ignore[arg-type]
    assert exhausted.retried == eager.retried == []
    # A one-source job (map fields, ensemble) reports itself, without "outcomes".
    single = FakeTask()
    with pytest.raises(Retry):
        tasks.retry_transient(single, {"status": "failed", "retryable": True, "retry_after_s": 400})  # type: ignore[arg-type]
    assert single.retried[0]["countdown"] == 400


# ------------------------------------------------------------------ the plain job schedule
# app.jobs.schedule is what beat (through app.workers.schedule) and the data-health report
# read. These pin today's entries and job options; the synced model (D14) changes them.
def every_job_settings_and_tides() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        gefs_enabled=True,
        observations_enabled=True,
        verification_enabled=True,
        map_fields_enabled=True,
        tide_providers="noaa_coops",
    )


def test_the_schedule_has_today_s_entries() -> None:
    plain = build_schedule(every_job_settings_and_tides())
    assert all(isinstance(e, JobEntry) and e.name == name for name, e in plain.items())
    assert {name: e.task for name, e in plain.items()} == {
        "forecast-acquisition": "forecasts.acquire",
        "quality-prediction-safety-net": "quality.predict",
        "swell-detection": "detection.detect",
        "user-matching": "matching.match",
        "flight-discovery": "flights.discover",
        "alert-generation": "alerts.generate",
        "notification-delivery": "notifications.deliver",
        "forecast-staleness-check": "forecasts.check_staleness",
        "data-health": DATA_HEALTH_TASK,
        "maintenance-cleanup": "maintenance.cleanup",
        "map-fields": "forecasts.map_fields",
        "ensemble-acquisition": "forecasts.acquire_ensemble",
        "tide-acquisition": "tides.acquire",
        "observation-ingest": "observations.ingest",
        "point-forecast-archive": "verification.collect_point_forecasts",
        "verification": "verification.run",
    }
    assert plain["maintenance-cleanup"].schedule == DailyAt((3,), 30)
    assert plain["ensemble-acquisition"].schedule == DailyAt((1, 7, 13, 19), 40)
    assert plain["notification-delivery"].schedule == Every(2)
    assert plain["forecast-acquisition"].schedule == Every(180)


def test_job_specs_keep_the_task_options() -> None:
    specs = JOB_SPECS
    assert specs["forecasts.acquire"].lock_ttl == 3600
    assert specs["forecasts.acquire"].judge is judge_acquisition
    assert specs["forecasts.map_fields"].judge is judge_single
    assert specs["forecasts.map_fields"].chain is False
    assert specs["forecasts.acquire_ensemble"].judge is judge_single
    assert specs["notifications.deliver"].lock_ttl == 600
    assert specs[DATA_HEALTH_TASK].lock_ttl == 600 and specs[DATA_HEALTH_TASK].chain is False
    assert specs["flights.discover"].lock_ttl == 3600
    assert specs["sync.tick"].lock_ttl == 3600 and specs["sync.tick"].chain is False
    for name in ("maintenance.cleanup", "forecasts.check_staleness", "sync.publish"):
        assert specs[name].chain is False, name
    for name in ("forecasts.blend", "quality.predict", "detection.detect", "matching.match"):
        assert specs[name].chain is True and specs[name].lock_ttl == 1800, name


# Moved from tests/integration/test_map_fields.py: the beat entry belongs to the schedule.
def test_beat_runs_the_map_field_job_in_standalone_mode_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers import celery_app as module

    assert "forecasts.map_fields" in module.celery_app.tasks
    assert "map-fields" not in module._optional_schedule()
    base = module.settings
    monkeypatch.setattr(module, "settings", base.model_copy(update={"map_fields_enabled": True}))
    job = module._optional_schedule()["map-fields"]
    assert job["task"] == "forecasts.map_fields"
    assert job["schedule"] == timedelta(minutes=base.forecast_refresh_minutes)
    with_noaa = base.model_copy(
        update={"map_fields_enabled": True, "forecast_providers": "noaa_gfs"}
    )
    monkeypatch.setattr(module, "settings", with_noaa)
    assert "map-fields" not in module._optional_schedule()  # the NOAA acquisition does it
