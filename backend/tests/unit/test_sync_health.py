"""The synced model in data health (app.services.health), without a database.

Heartbeats, sync rows, components and CPU figures below are synthetic test values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.config import Settings
from app.models.sync import SyncRun
from app.services import health
from app.services.sync.detect import settle_plan
from app.services.sync.state import Heartbeat
from app.services.sync.types import ComponentStatus, StageResult, SyncPlan, SyncResult

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def beat(age_minutes: float, *, cpu_s_month: float | None = None, at: datetime | None = None):  # type: ignore[no-untyped-def]
    return Heartbeat(
        at=at or NOW - timedelta(minutes=age_minutes),
        runner="daemon",
        due=False,
        cpu_s_month=cpu_s_month,
    )


def sync_row(row_id: int, status: str, minutes_ago: float, error: str | None = None) -> SyncRun:
    return SyncRun(
        id=row_id,
        trigger="daemon",
        status=status,
        started_at=NOW - timedelta(minutes=minutes_ago),
        finished_at=NOW - timedelta(minutes=minutes_ago - 1),
        plan={},
        stages=[],
        error_message=error,
    )


# --------------------------------------------------------------------------- heartbeat
def test_a_fresh_daemon_heartbeat_is_fine_and_a_stale_one_is_failing() -> None:
    s = settings(job_runner="daemon")
    assert health.heartbeat_limit_minutes(s) == 30  # max(3 x 10 min, 30 min)
    assert health.heartbeat_problem(s, beat(5), NOW, sync_running=False) is None
    assert health.heartbeat_problem(s, beat(30), NOW, sync_running=False) is None
    stale = health.heartbeat_problem(s, beat(31), NOW, sync_running=False)
    assert stale is not None and stale.key == "sync_heartbeat_stale"
    assert stale.severity == "failing"
    assert "2026-10-21 11:29 UTC" in stale.message and "limit 30 min" in stale.message


def test_no_heartbeat_at_all_is_failing() -> None:
    problem = health.heartbeat_problem(settings(job_runner="daemon"), None, NOW, sync_running=False)
    assert problem is not None and problem.severity == "failing"
    assert "no heartbeat" in problem.message


def test_a_running_sync_with_a_valid_lease_keeps_the_daemon_alive() -> None:
    """A long sync holds up the daemon's checks (one map-layer sync takes minutes)."""
    s = settings(job_runner="daemon")
    assert health.heartbeat_problem(s, beat(50), NOW, sync_running=True) is None
    assert health.heartbeat_problem(s, None, NOW, sync_running=True) is None


def test_the_heartbeat_limit_follows_the_check_interval() -> None:
    s = settings(job_runner="daemon", sync_check_minutes=20)
    assert health.heartbeat_limit_minutes(s) == 60
    assert health.heartbeat_problem(s, beat(59), NOW, sync_running=False) is None
    assert health.heartbeat_problem(s, beat(61), NOW, sync_running=False) is not None
    longer = settings(job_runner="daemon", data_health_heartbeat_minutes=90)
    assert health.heartbeat_limit_minutes(longer) == 90


# --------------------------------------------------------------------------- failed syncs
def test_two_failed_syncs_in_a_row_are_degraded() -> None:
    rows = [
        sync_row(12, "failed", 10, error="stage spots: OperationalError at db-host-7"),
        sync_row(11, "failed", 70),
    ]
    problem = health.failed_syncs_problem(rows)
    assert problem is not None and problem.key == "sync_failed"
    assert problem.severity == "degraded" and "sync 12" in problem.message
    # The error text goes to admin emails only.
    assert problem.detail == "stage spots: OperationalError at db-host-7"
    assert "db-host-7" not in problem.message


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["failed", "abandoned"], True),
        (["abandoned", "abandoned"], True),
        (["failed", "success"], False),
        (["success", "failed"], False),
        (["failed", "partial"], False),
        (["failed", "skipped"], False),
        (["failed"], False),
        ([], False),
    ],
)
def test_only_two_failures_in_a_row_count(statuses: list[str], expected: bool) -> None:
    rows = [sync_row(10 - i, s, 10 + 60 * i) for i, s in enumerate(statuses)]
    assert (health.failed_syncs_problem(rows) is not None) is expected


# --------------------------------------------------------------------------- CPU budget
@pytest.mark.parametrize(
    ("used", "alert"),
    [(7.9, False), (8.0, False), (8.1, True), (12.0, True), (0.0, False)],
)
def test_the_cpu_budget_warns_above_80_percent(used: float, alert: bool) -> None:
    problem = health.cpu_budget_problem(used, 10.0)
    assert (problem is not None) is alert
    if problem is not None:
        assert problem.key == "sync_cpu_budget" and problem.severity == "degraded"
        assert f"{used / 10:.0%} of SYNC_CPU_BUDGET_HOURS (10 h" in problem.message


def test_no_budget_no_cpu_problem() -> None:
    assert health.cpu_budget_problem(500.0, None) is None


def test_cpu_hours_take_the_larger_lower_bound_and_the_daemon_heartbeat() -> None:
    daemon = settings(job_runner="daemon")
    celery = settings(job_runner="celery")
    hb = beat(1, cpu_s_month=360.0)
    # Redis counter 7,200 s vs. retained job logs 3,600 s: the counter (2 h), plus 0.1 h.
    used = health.combine_cpu_hours(daemon, NOW, counted_s=7200.0, logged_s=3600.0, heartbeat=hb)
    assert used == pytest.approx(2.1)
    # Redis lost its data: the job logs still count.
    used = health.combine_cpu_hours(daemon, NOW, counted_s=0.0, logged_s=3600.0, heartbeat=None)
    assert used == pytest.approx(1.0)
    used = health.combine_cpu_hours(daemon, NOW, counted_s=None, logged_s=1800.0, heartbeat=None)
    assert used == pytest.approx(0.5)
    # The Celery runner has no daemon heartbeat to add.
    assert health.combine_cpu_hours(
        celery, NOW, counted_s=3600.0, logged_s=0.0, heartbeat=hb
    ) == pytest.approx(1.0)
    # A heartbeat written last month says nothing about this month.
    old = beat(0, cpu_s_month=36000.0, at=datetime(2026, 9, 30, 23, 50, tzinfo=UTC))
    assert health.combine_cpu_hours(
        daemon, NOW, counted_s=0.0, logged_s=0.0, heartbeat=old
    ) == pytest.approx(0.0)


def test_month_start_is_utc() -> None:
    local = datetime(2026, 11, 1, 1, 30, tzinfo=UTC).astimezone()  # any local zone
    assert health.month_start(local) == datetime(2026, 11, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- licence
def test_commercial_use_with_the_free_open_meteo_api_is_a_licence_problem() -> None:
    (problem,) = health.check_licence(
        settings(commercial_use=True, forecast_providers="open_meteo,open_meteo_ecmwf")
    )
    assert problem.key == "licence" and problem.severity == "failing"
    assert "open_meteo, open_meteo_ecmwf" in problem.message
    assert "OPEN_METEO_API_KEY" in problem.message


@pytest.mark.parametrize(
    "values",
    [
        {"commercial_use": False, "forecast_providers": "open_meteo"},
        {
            "commercial_use": True,
            "forecast_providers": "open_meteo",
            "open_meteo_api_key": "k" * 20,
        },
        {"commercial_use": True, "forecast_providers": "demo"},
        {"commercial_use": True, "forecast_providers": "noaa_gfs,ecmwf_ifs"},
    ],
)
def test_no_licence_problem_otherwise(values: dict[str, Any]) -> None:
    assert health.check_licence(settings(**values)) == []


# --------------------------------------------------------------------------- Redis policy
def test_the_eviction_policy_is_checked_for_the_celery_broker_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = {"used_memory": 1024**2, "maxmemory": 256 * 1024**2, "maxmemory_policy": "allkeys-lru"}
    monkeypatch.setattr(health, "redis_memory", lambda: info)  # SYNTHETIC INFO
    out, problems = health._check_redis(settings(job_runner="celery"))
    assert [p.key for p in problems] == ["redis_eviction_policy"]
    out, problems = health._check_redis(settings(job_runner="daemon"))
    assert problems == [] and out.maxmemory_policy == "allkeys-lru"  # still reported


# --------------------------------------------------------------------------- expected jobs
def test_the_daemon_has_no_sync_tick_job_and_stages_are_never_overdue() -> None:
    celery = {j.name: j for j in health.expected_jobs(settings(job_runner="celery"))}
    daemon = {j.name: j for j in health.expected_jobs(settings(job_runner="daemon"))}
    assert "sync.tick" in celery and celery["sync.tick"].max_gap == timedelta(minutes=10)
    assert "sync.tick" not in daemon
    for jobs in (celery, daemon):
        for name in ("forecasts.acquire", "forecasts.blend", "quality.predict", "sync.publish"):
            assert jobs[name].schedule == health.SYNC_STAGE_SCHEDULE == "in each sync", name
            assert jobs[name].max_gap is None, name
        # Scheduled stages keep their own interval (they also run between syncs).
        assert jobs["matching.match"].max_gap == timedelta(minutes=30)
        assert jobs["detection.detect"].max_gap is None


def test_switched_off_stages_are_not_expected() -> None:
    optional = {"forecasts.acquire_ensemble", "forecasts.map_fields", "maintenance.prune_pass"}
    plain = {j.name for j in health.expected_jobs(settings())}
    assert not optional & plain
    every = {
        j.name
        for j in health.expected_jobs(
            settings(gefs_enabled=True, map_fields_enabled=True, sync_prune_each_pass=True)
        )
    }
    assert optional <= every
    no_blend = {j.name for j in health.expected_jobs(settings(forecast_blend_enabled=False))}
    assert "forecasts.blend" not in no_blend


def test_the_floor_widens_the_overdue_window() -> None:
    jobs = {j.name: j for j in health.expected_jobs(settings(job_interval_floor_minutes=60))}
    assert jobs["notifications.deliver"].max_gap == timedelta(hours=1)
    assert jobs["notifications.deliver"].schedule == "every hour"


# --------------------------------------------------------------------------- public shapes
def test_components_never_carry_a_provider_error() -> None:
    raw = {
        "kind": "spots",
        "source_code": "open_meteo_noaa",
        "provider": "open_meteo",
        "state": "unknown",
        "stored_run_key": None,
        "available_run_key": None,
        "available_cycle": None,
        "detail": "ConnectError: [Errno 111] at https://synthetic.example/?apikey=SECRET",
    }
    out = health._component_out(raw)
    assert out is not None and out.state == "unknown"
    assert out.detail == health.UNKNOWN_COMPONENT_DETAIL
    waiting = health._component_out(
        raw
        | {
            "state": "waiting",
            "detail": "waiting for noaa_gfs_fields 2026-10-21 06Z until 13:30 UTC",
            "available_cycle": "2026-10-21T06:00:00+00:00",
        }
    )
    assert waiting is not None and waiting.detail is not None
    assert waiting.detail.startswith("waiting for") and waiting.available_cycle == datetime(
        2026, 10, 21, 6, tzinfo=UTC
    )
    assert health._component_out({"kind": "spots"}) is None
    assert health._component_out(raw | {"state": "nonsense"}) is None


def test_a_failed_components_error_text_stays_out_of_the_public_report() -> None:
    """settle_plan writes "sync N failed: <the stage's exception text>"; a database error
    names the host, port and role (SYNTHETIC values here). The public report keeps only
    which sync failed; the service's own failure details are shown as written."""
    plan = SyncPlan(
        checked_at=NOW,
        due=True,
        components=(
            ComponentStatus(
                kind="spots",
                source_code="open_meteo_noaa",
                provider="open_meteo",
                state="new",
                stored_run_key="gfs:2026102100",
                available_run_key="gfs:2026102106",
                available_cycle=datetime(2026, 10, 21, 6, tzinfo=UTC),
            ),
        ),
        anchor_cycle=None,
        reasons=("synthetic",),
    )
    error = (
        "OperationalError: (psycopg.OperationalError) connection failed: connection to server "
        'at "203.0.113.7", port 5432 failed: FATAL:  password authentication failed for user '
        '"synthetic_owner"'
    )
    stage = StageResult(
        "spots",
        "failed",
        NOW,
        10,
        0.1,
        {"components": [{"source_code": "open_meteo_noaa", "outcome": "failed", "message": error}]},
    )
    settled = settle_plan(plan, SyncResult(7, "failed", plan, [stage], None))
    internal = settled.components[0].as_dict()
    assert internal["state"] == "failed" and "synthetic_owner" in internal["detail"]
    out = health._component_out(internal)
    assert out is not None and out.state == "failed"
    assert out.detail == "sync 7 failed; the error text is not shown publicly"
    for leaked in ("203.0.113.7", "5432", "synthetic_owner", "port", "user"):
        assert leaked not in out.model_dump_json(), leaked
    attempts = health._component_out(
        internal | {"detail": "6 failed attempts for gfs:2026102106; waiting for the next run"}
    )
    assert attempts is not None and attempts.detail is not None
    assert attempts.detail.startswith("6 failed attempts for gfs:2026102106")


def test_sync_runs_are_served_without_error_text_or_stage_details() -> None:
    row = sync_row(7, "partial", 30, error="stage map: HTTPError at db-host-7")
    row.published_at = NOW - timedelta(minutes=29)
    row.stages = [
        {
            "name": "spots",
            "status": "success",
            "started_at": "2026-10-21T11:30:00+00:00",
            "duration_ms": 1234,
            "cpu_s": 1.5,
            "detail": {"outcomes": ["synthetic"], "error": "db-host-7"},
        },
        {"name": "map", "status": "failed", "duration_ms": 10, "cpu_s": None},
        {"status": "success"},  # malformed: skipped
        "not a stage",
    ]
    out = health._sync_run_out(row)
    assert out.id == 7 and out.status == "partial" and out.published_at == row.published_at
    assert [(s.name, s.status, s.duration_ms, s.cpu_s) for s in out.stages] == [
        ("spots", "success", 1234, 1.5),
        ("map", "failed", 10, None),
    ]
    assert "db-host-7" not in out.model_dump_json()
