"""The synced job schedule (D14): the sync-tick entry and JOB_INTERVAL_FLOOR_MINUTES.

app.jobs.schedule.build_schedule is read by Celery beat (through app.workers.schedule), the
sync daemon (every entry but sync.tick) and the data-health report.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.profiles import profile_env
from app.jobs.schedule import (
    SYNC_TICK_ENTRY,
    DailyAt,
    Every,
    apply_floor,
    build_schedule,
    describe_schedule,
    max_interval,
)
from app.services.sync.types import SYNC_TICK_JOB
from app.workers import schedule as beat

# Tasks a sync runs as its stages; none of them may be on a timer of its own.
SYNC_STAGE_TASKS = frozenset(
    {
        "forecasts.acquire",
        "forecasts.acquire_ensemble",
        "forecasts.map_fields",
        "quality.predict",
        "detection.detect",
    }
)


def settings(**values: object) -> Settings:
    base = {
        "gefs_enabled": True,
        "observations_enabled": True,
        "verification_enabled": True,
        "map_fields_enabled": True,
        "tide_providers": "noaa_coops",
    }
    return Settings(_env_file=None, **(base | values))  # type: ignore[arg-type]


@pytest.mark.parametrize("floor", [0, 5, 30, 60, 180])
def test_the_floor_raises_every_interval_and_keeps_daily_times(floor: int) -> None:
    unfloored = build_schedule(settings())
    floored = build_schedule(settings(job_interval_floor_minutes=floor))
    assert list(floored) == list(unfloored)  # same entries, same order
    for name, entry in floored.items():
        before = unfloored[name].schedule
        assert entry.task == unfloored[name].task
        if isinstance(before, Every):
            assert isinstance(entry.schedule, Every)
            assert entry.schedule.minutes == max(before.minutes, floor), name
            assert max_interval(entry.schedule) >= timedelta(minutes=floor)
        else:
            assert entry.schedule == before, name  # DailyAt is never moved


def test_floor_zero_keeps_the_listed_intervals() -> None:
    plain = build_schedule(settings())
    assert plain["notification-delivery"].schedule == Every(2)
    assert plain["alert-generation"].schedule == Every(15)
    assert plain["user-matching"].schedule == Every(30)
    assert plain["flight-discovery"].schedule == Every(60)


def test_the_hosted_profile_batches_the_periodic_jobs_hourly() -> None:
    hosted = Settings(_env_file=None, **{k.lower(): v for k, v in profile_env("hosted").items()})  # type: ignore[arg-type]
    assert hosted.job_interval_floor_minutes == 60
    entries = build_schedule(hosted)
    every = {n: e.schedule for n, e in entries.items() if isinstance(e.schedule, Every)}
    assert every, "the hosted schedule has interval jobs"
    assert all(s.minutes == 60 for s in every.values()), every
    # The daemon checks the providers every SYNC_CHECK_MINUTES on its own; the floored
    # sync-tick entry is what Celery beat would use, and the daemon skips it.
    assert entries[SYNC_TICK_ENTRY].task == SYNC_TICK_JOB
    assert describe_schedule(entries["notification-delivery"].schedule) == "every hour"


def test_apply_floor() -> None:
    assert apply_floor(Every(2), 60) == Every(60)
    assert apply_floor(Every(90), 60) == Every(90)
    assert apply_floor(Every(2), 0) == Every(2)
    assert apply_floor(DailyAt((3,), 30), 600) == DailyAt((3,), 30)


@pytest.mark.parametrize("runner", ["celery", "daemon"])
def test_no_forecast_stage_is_on_a_timer_and_sync_tick_is_listed(runner: str) -> None:
    entries = build_schedule(settings(job_runner=runner))
    assert not SYNC_STAGE_TASKS & {e.task for e in entries.values()}
    tick = entries[SYNC_TICK_ENTRY]
    assert tick.task == SYNC_TICK_JOB and tick.schedule == Every(10)
    assert [e.name for e in entries.values() if e.task == SYNC_TICK_JOB] == [SYNC_TICK_ENTRY]


def test_the_sync_check_interval_is_floored_too() -> None:
    entries = build_schedule(settings(sync_check_minutes=15, job_interval_floor_minutes=60))
    assert entries[SYNC_TICK_ENTRY].schedule == Every(60)


def test_beat_gets_the_floored_intervals_and_matching_expiry() -> None:
    s = settings(job_interval_floor_minutes=60)
    beat_entries = beat.celery_beat_schedule(s)
    assert beat_entries["notification-delivery"]["schedule"] == timedelta(hours=1)
    assert beat_entries["notification-delivery"]["options"]["expires"] == 3600.0
    assert beat_entries[SYNC_TICK_ENTRY]["task"] == SYNC_TICK_JOB
    assert beat_entries["user-matching"]["schedule"] == timedelta(hours=1)
    assert beat_entries["maintenance-cleanup"]["options"]["expires"] == 24 * 3600.0


@pytest.mark.parametrize("runner", ["celery", "daemon"])
def test_the_system_status_lists_every_job_a_runner_logs(runner: str) -> None:
    """/api/system/status shows the last run of JOB_NAMES: every scheduled task, the sync's
    own log row (sync.run, under every runner) and each stage a sync runs."""
    from app.api.system import JOB_NAMES
    from app.services.sync.types import STAGE_JOB_NAMES, SYNC_RUN_JOB

    scheduled = {e.task for e in build_schedule(settings(job_runner=runner)).values()}
    listed = set(JOB_NAMES)
    assert scheduled <= listed, scheduled - listed
    assert set(STAGE_JOB_NAMES.values()) <= listed, set(STAGE_JOB_NAMES.values()) - listed
    assert {SYNC_RUN_JOB, SYNC_TICK_JOB} <= listed
    assert len(JOB_NAMES) == len(listed)  # no duplicates
