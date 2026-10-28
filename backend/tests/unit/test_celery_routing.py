"""Celery queues, beat expiry, the synced schedule (D14) and the acquisition retry rules
(app.workers, app.jobs.schedule)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from celery.exceptions import Retry

from app.core.config import Settings
from app.jobs.registry import JOB_SPECS
from app.jobs.runner import judge_acquisition, judge_single
from app.jobs.schedule import (
    DATA_HEALTH_TASK,
    SYNC_TICK_SOFT_TIME_LIMIT_SECONDS,
    SYNC_TICK_TIME_LIMIT_SECONDS,
    DailyAt,
    Every,
    JobEntry,
    build_schedule,
    max_interval,
    optional_jobs,
)
from app.models.enums import JobStatus
from app.workers import tasks
from app.workers.celery_app import celery_app
from app.workers.schedule import (
    BULK_QUEUE,
    DEFAULT_QUEUE,
    build_beat_schedule,
    celery_beat_schedule,
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
    "task",
    ["forecasts.acquire", "observations.ingest", "tides.acquire", "verification.run", "sync.tick"],
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
    names |= {"forecasts.blend", "notifications.send_account_email"}
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
    plain = build_schedule(every_job_settings())
    assert set(schedule) == set(build_beat_schedule(every_job_settings())) == set(plain)
    for name, entry in schedule.items():
        expires = entry["options"]["expires"]
        assert expires == max_interval(plain[name].schedule).total_seconds() > 0, name
    assert schedule["notification-delivery"]["options"]["expires"] == 120.0
    # The configured app carries the same options.
    assert all("expires" in e["options"] for e in celery_app.conf.beat_schedule.values())


def test_ensemble_and_map_layers_have_no_beat_entry_of_their_own() -> None:
    """D14: GEFS-Wave and the world map layers are acquired by the sync when their provider
    publishes a new run (was: GEFS at cycle + 7 h 40 min, map fields every 3 h)."""
    for settings in (
        every_job_settings(),
        Settings(_env_file=None, gefs_enabled=True, gefs_cycles="00,12"),  # type: ignore[call-arg]
    ):
        tasks_ = {entry["task"] for entry in build_beat_schedule(settings).values()}
        assert not tasks_ & {"forecasts.acquire_ensemble", "forecasts.map_fields"}, tasks_


# --------------------------------------------------------------------------- job judging
def outcome(status: str, **kw: Any) -> dict[str, Any]:
    return {"source": "x", "status": status, "message": kw.pop("message", ""), **kw}


def test_an_all_failed_acquisition_is_a_failed_job() -> None:
    status, error = judge_acquisition(
        {"outcomes": [outcome("failed", message="quota"), outcome("failed", message="503")]}
    )
    assert status == JobStatus.FAILED and error is not None and "quota" in error
    assert judge_acquisition({"outcomes": [outcome("failed"), outcome("success")]}) == (
        JobStatus.SUCCESS,
        None,
    )
    assert judge_acquisition({"outcomes": [outcome("skipped")]})[0] == JobStatus.SUCCESS
    assert judge_single({"status": "failed", "message": "x"})[0] == JobStatus.FAILED
    assert judge_single({"skipped": "mode is off"})[0] == JobStatus.SUCCESS


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
# app.jobs.schedule is what beat (through app.workers.schedule), the sync daemon and the
# data-health report read. These pin the entries of the synced model (D14) and the job
# options.
def every_job_settings_and_tides() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        gefs_enabled=True,
        observations_enabled=True,
        verification_enabled=True,
        map_fields_enabled=True,
        tide_providers="noaa_coops",
    )


def test_the_schedule_has_the_synced_entries() -> None:
    """D14: one sync-tick entry replaces forecast-acquisition, map-fields,
    ensemble-acquisition, quality-prediction-safety-net and swell-detection."""
    plain = build_schedule(every_job_settings_and_tides())
    assert all(isinstance(e, JobEntry) and e.name == name for name, e in plain.items())
    assert {name: e.task for name, e in plain.items()} == {
        "sync-tick": "sync.tick",
        # Design D15: daily matching housekeeping; flights.discover only runs in a sync or
        # on a kick (never on a timer).
        "matching-housekeeping": "matching.match",
        "alert-generation": "alerts.generate",
        "notification-delivery": "notifications.deliver",
        "forecast-staleness-check": "forecasts.check_staleness",
        "data-health": DATA_HEALTH_TASK,
        "maintenance-cleanup": "maintenance.cleanup",
        "tide-acquisition": "tides.acquire",
        "observation-ingest": "observations.ingest",
        "point-forecast-archive": "verification.collect_point_forecasts",
        "verification": "verification.run",
    }
    assert list(plain)[0] == "sync-tick"
    assert plain["sync-tick"].schedule == Every(10)  # SYNC_CHECK_MINUTES
    assert plain["maintenance-cleanup"].schedule == DailyAt((3,), 30)
    assert plain["notification-delivery"].schedule == Every(2)
    acquisition = {"forecasts.acquire", "forecasts.acquire_ensemble", "forecasts.map_fields"}
    assert not acquisition & {e.task for e in plain.values()}
    assert not {"quality.predict", "detection.detect"} & {e.task for e in plain.values()}
    checks = Settings(_env_file=None, sync_check_minutes=5)  # type: ignore[call-arg]
    assert build_schedule(checks)["sync-tick"].schedule == Every(5)


def test_the_sync_tick_task_has_its_own_time_limits() -> None:
    task = celery_app.tasks["sync.tick"]
    assert task.time_limit == SYNC_TICK_TIME_LIMIT_SECONDS == 3600
    assert task.soft_time_limit == SYNC_TICK_SOFT_TIME_LIMIT_SECONDS == 3300
    # Every other task keeps the app-wide limits (30 / 25 min).
    assert celery_app.conf.task_time_limit == 1800
    assert celery_app.tasks["forecasts.acquire"].time_limit is None
    # A sync still running is never redelivered to a second worker.
    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert visibility > SYNC_TICK_TIME_LIMIT_SECONDS


@pytest.mark.parametrize(
    "name",
    ["notifications.send_account_email", "notifications.send_sms"],
)
def test_messaging_tasks_ignore_their_result(name: str) -> None:
    """Without a result, .delay() does not subscribe to the result backend (the 19 s
    RuntimeError with Redis refused, D14)."""
    assert celery_app.tasks[name].ignore_result is True


def test_broker_and_result_backend_sockets_time_out() -> None:
    from app.core.config import get_settings

    conf, settings = celery_app.conf, get_settings()
    assert conf.broker_connection_timeout == 2.0
    options = conf.broker_transport_options
    assert options["socket_connect_timeout"] == settings.redis_connect_timeout_seconds
    assert options["socket_timeout"] == settings.redis_socket_timeout_seconds
    assert conf.redis_socket_connect_timeout == settings.redis_connect_timeout_seconds
    assert conf.redis_socket_timeout == settings.redis_socket_timeout_seconds


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
def test_beat_never_runs_the_map_field_job_on_a_timer() -> None:
    """D14: in standalone mode the sync acquires the map layers with the spot forecasts of
    the same GFS cycle (was: a beat entry every FORECAST_REFRESH_MINUTES); with NOAA the
    acquisition stores them. The task stays callable by hand."""
    from app.workers import celery_app as module

    assert "forecasts.map_fields" in module.celery_app.tasks
    base = module.settings
    for update in (
        {},
        {"map_fields_enabled": True},
        {"map_fields_enabled": True, "forecast_providers": "noaa_gfs"},
    ):
        optional = optional_jobs(base.model_copy(update=update))
        assert "map-fields" not in optional, update
        assert not any(e.task == "forecasts.map_fields" for e in optional.values())
    standalone = base.model_copy(update={"map_fields_enabled": True})
    assert build_beat_schedule(standalone)["sync-tick"]["schedule"] == timedelta(
        minutes=standalone.sync_check_minutes
    )
