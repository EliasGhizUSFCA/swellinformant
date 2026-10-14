"""Data-health report, GET /api/health/data and the ops.data_health alert job.

Every forecast run, job log, map field and buoy observation below is synthetic test data
written by the test itself (labelled "synthetic" where it has a name); nothing here is a
real forecast or observation.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    BackgroundJobLog,
    BuoyObservation,
    BuoyStation,
    ForecastRun,
    ForecastSource,
    User,
)
from app.models.enums import ForecastRunStatus, JobStatus
from app.models.forecast_engine import CORE_MAP_FIELD_VARIABLES, MapField
from app.services.health import data_health, expected_jobs
from app.services.notifications.ops_alerts import run_data_health_check
from app.workers import tasks
from app.workers.celery_app import celery_app
from app.workers.schedule import DATA_HEALTH_TASK
from tests.conftest import ApiClient, outbox_messages

pytestmark = pytest.mark.integration


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    get_settings.cache_clear()


def set_env(mp: pytest.MonkeyPatch, **values: str) -> None:
    for k, v in values.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


# --------------------------------------------------------------------------- fixtures
FAMILIES = {"demo": "demo", "noaa_gfs_direct": "ncep_gfs", "ecmwf_ifs_open": "ecmwf_ifs"}


def source(db: Session, code: str, *, kind: str = "deterministic") -> ForecastSource:
    row = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
    if row is None:
        row = ForecastSource(
            code=code,
            name=f"synthetic {code}",
            provider="test",
            is_demo=code == "demo",
            family=FAMILIES.get(code, "ncep_gefs" if kind == "ensemble" else "unknown"),
            kind=kind,
        )
        db.add(row)
        db.flush()
    return row


def run(
    db: Session,
    code: str,
    age_hours: float,
    *,
    status: ForecastRunStatus = ForecastRunStatus.SUCCESS,
    now: datetime | None = None,
    kind: str = "deterministic",
    started_age_hours: float | None = None,
) -> ForecastRun:
    now = now or utcnow()
    issued = now - timedelta(hours=age_hours)
    row = ForecastRun(
        source_id=source(db, code, kind=kind).id,
        run_key=f"{code}:synthetic-{issued:%Y%m%d%H%M}-{status.value}",
        issued_at=issued,
        started_at=now - timedelta(hours=started_age_hours if started_age_hours else age_hours),
        status=status,
        details={},
    )
    db.add(row)
    db.commit()
    return row


def job_log(
    db: Session,
    name: str,
    status: JobStatus,
    minutes_ago: float,
    *,
    now: datetime | None = None,
    error: str | None = None,
) -> BackgroundJobLog:
    now = now or utcnow()
    started = now - timedelta(minutes=minutes_ago)
    row = BackgroundJobLog(
        job_name=name,
        status=status,
        started_at=started,
        finished_at=None if status == JobStatus.RUNNING else started + timedelta(seconds=5),
        duration_ms=None if status == JobStatus.RUNNING else 5000,
        details={},
        error_message=error,
    )
    db.add(row)
    db.commit()
    return row


def heartbeat(db: Session, now: datetime | None = None) -> None:
    job_log(db, "notifications.deliver", JobStatus.SUCCESS, 1, now=now)


def map_fields(db: Session, run_row: ForecastRun, cycle: datetime, steps: int = 2) -> None:
    blob = zlib.compress(bytes([10]))
    for i in range(steps):
        for variable in CORE_MAP_FIELD_VARIABLES:
            db.add(
                MapField(
                    run_id=run_row.id,
                    source_code="noaa_gfs_fields",
                    run_key=run_row.run_key,
                    cycle_time=cycle,
                    valid_time=cycle + timedelta(hours=3 * i),
                    variable=variable,
                    nx=1,
                    ny=1,
                    lat0=0.0,
                    lon0=-180.0,
                    dlat=0.5,
                    dlon=0.5,
                    scale=0.1,
                    offset=0.0,
                    data=blob,
                )
            )
    db.commit()


def keys(db: Session, now: datetime | None = None) -> set[str]:
    return {p.key for p in data_health(db, now=now).problems}


def by_job(report_jobs: list, name: str):  # type: ignore[no-untyped-def,type-arg]
    return next(j for j in report_jobs if j.job == name)


# --------------------------------------------------------------------------- report
def test_fresh_demo_forecast_with_running_jobs_is_ok(db: Session) -> None:
    run(db, "demo", 2)
    heartbeat(db)
    report = data_health(db)
    assert report.status == "ok", report.out.reasons
    assert report.out.reasons == [] and report.out.problems == []
    (demo,) = report.out.forecast_sources
    assert demo.status == "ok" and demo.role == "forecast" and demo.stale_after_hours == 18
    assert demo.age_hours == pytest.approx(2, abs=0.01)
    assert report.out.map_fields.status == "off" and not report.out.observations.enabled
    assert report.out.heartbeat.age_minutes is not None and report.out.heartbeat.limit_minutes == 30


def test_only_source_stale_is_failing_then_fresh_run_recovers(db: Session) -> None:
    heartbeat(db)
    run(db, "demo", 20)  # stale after FORECAST_STALE_HOURS = 18
    report = data_health(db)
    assert report.status == "failing"
    assert [p.key for p in report.problems] == ["forecasts_unavailable", "forecast_stale:demo"]
    assert "issued 20.0 h ago (stale after 18.0 h)" in report.out.reasons[1]
    run(db, "demo", 1)
    assert data_health(db).status == "ok"


def test_never_ingested_source_and_failed_latest_attempt(db: Session) -> None:
    heartbeat(db)
    assert keys(db) == {"forecast_missing:demo", "forecasts_unavailable"}
    run(db, "demo", 20)
    run(db, "demo", 0.5, status=ForecastRunStatus.FAILED, started_age_hours=0.4)
    report = data_health(db)
    assert report.out.forecast_sources[0].latest_attempt_status == "failed"
    assert "the latest attempt" in report.out.reasons[1] and "failed" in report.out.reasons[1]


def test_live_sources_blend_and_ensemble(db: Session, env: pytest.MonkeyPatch) -> None:
    set_env(env, FORECAST_PROVIDERS="noaa_gfs,ecmwf_ifs", GEFS_ENABLED="true")
    heartbeat(db)
    run(db, "noaa_gfs_direct", 17)  # stale after 6 + 5 + 3 + 2 = 16 h
    run(db, "ecmwf_ifs_open", 24)  # fresh until 12 + 8 + 3 + 2 = 25 h
    report = data_health(db)
    codes = [(s.code, s.role, s.status) for s in report.out.forecast_sources]
    assert codes == [
        ("blend", "blend", "missing"),
        ("noaa_gfs_direct", "forecast", "stale"),
        ("ecmwf_ifs_open", "forecast", "ok"),
        ("noaa_gefs_wave", "ensemble", "missing"),
    ]
    # One fresh family keeps the product working: degraded, not failing. The missing
    # blend is explained by the stale member, so it is not a problem of its own.
    assert report.status == "degraded"
    assert {p.key for p in report.problems} == {
        "forecast_stale:noaa_gfs_direct",
        "ensemble_missing",
        "map_fields_missing",  # with_noaa mode
    }
    run(db, "noaa_gfs_direct", 3)
    run(db, "noaa_gefs_wave", 19, kind="ensemble")  # stale after 6 + 7.5 + 3 + 2 = 18.5 h
    problems = keys(db)
    assert "blend_missing" in problems and "ensemble_stale" in problems
    assert not any(k.startswith("forecast_") for k in problems)
    # While an acquisition (or the blend stage) runs, the chain is about to rebuild the
    # blend: not a problem of its own. A run "running" past the time limit does not count.
    acquire = job_log(db, "forecasts.acquire", JobStatus.RUNNING, 5)
    assert "blend_missing" not in keys(db)
    assert data_health(db).out.forecast_sources[0].status == "missing"  # still shown as is
    acquire.started_at = utcnow() - timedelta(minutes=50)
    db.commit()
    assert "blend_missing" in keys(db)
    job_log(db, "forecasts.blend", JobStatus.RUNNING, 1)
    assert "blend_missing" not in keys(db)


def test_job_failed_overdue_stuck_running_and_never_run(db: Session) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    for minutes in (65, 50, 35):
        job_log(db, "detection.detect", JobStatus.SUCCESS, minutes + 60)
    job_log(db, "detection.detect", JobStatus.FAILED, 20, error="RuntimeError: boom at host-x")
    job_log(db, "detection.detect", JobStatus.SKIPPED, 10)  # lock held: not a result
    job_log(db, "detection.detect", JobStatus.FAILED, 5, error="RuntimeError: boom at host-x")
    job_log(db, "matching.match", JobStatus.SUCCESS, 120)  # every 30 min: overdue after 90
    job_log(db, "quality.predict", JobStatus.SUCCESS, 100)
    job_log(db, "quality.predict", JobStatus.RUNNING, 50)  # hard time limit 30 min + 15
    job_log(db, "alerts.generate", JobStatus.SUCCESS, 30)
    job_log(db, "alerts.generate", JobStatus.RUNNING, 1)
    job_log(db, "flights.discover", JobStatus.SUCCESS, 60 * 5)  # hourly: overdue after 150 min
    job_log(db, "flights.discover", JobStatus.RUNNING, 2)  # ... but it is running right now
    job_log(db, "forecasts.check_staleness", JobStatus.SUCCESS, 10)
    job_log(db, DATA_HEALTH_TASK, JobStatus.SUCCESS, 5)  # 15-min job, overdue after 60
    report = data_health(db)
    jobs = report.out.jobs
    detect = by_job(jobs, "detection.detect")
    assert detect.status == "failed" and detect.consecutive_failures == 2
    assert detect.last_status == "failed" and detect.last_success_at is not None
    assert by_job(jobs, "matching.match").status == "overdue"
    assert by_job(jobs, "quality.predict").status == "stuck"
    assert by_job(jobs, "alerts.generate").status == "running"
    assert by_job(jobs, "flights.discover").status == "running"  # started: not overdue
    assert by_job(jobs, "maintenance.cleanup").status == "never_run"
    assert by_job(jobs, "forecasts.blend").schedule == "after forecasts.acquire"
    assert by_job(jobs, "forecasts.blend").overdue_after_minutes is None
    problems = {p.key: p for p in report.problems}
    assert set(problems) == {
        "job_failed:detection.detect",
        "job_overdue:matching.match",
        "job_stuck:quality.predict",
    }
    failed = problems["job_failed:detection.detect"]
    assert "2 runs in a row" in failed.message
    # The raw error goes to admin emails only, never into the public message.
    assert failed.detail == "RuntimeError: boom at host-x" and "host-x" not in failed.message
    assert report.status == "degraded"


def test_job_that_never_ran_becomes_overdue_once_monitoring_is_old_enough(db: Session) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    assert by_job(data_health(db).out.jobs, "maintenance.cleanup").status == "never_run"
    job_log(db, "notifications.deliver", JobStatus.SUCCESS, 60 * 49)  # 2 x 24 h + 30 min ago
    report = data_health(db)
    assert by_job(report.out.jobs, "maintenance.cleanup").status == "overdue"
    assert "job_overdue:maintenance.cleanup" in {p.key for p in report.problems}


def test_skipped_duplicates_do_not_hide_a_run_in_progress(db: Session) -> None:
    """Seen live after a 13.5 h outage: beat's catch-up and BEAT_RUN_ON_START both queued
    forecasts.acquire; the second copy was logged "skipped" (lock held) while the first was
    still downloading, and the check called the running job overdue."""
    run(db, "demo", 1)
    heartbeat(db)
    job_log(db, "forecasts.acquire", JobStatus.SUCCESS, 60 * 13.5)  # before the outage
    job_log(db, "forecasts.acquire", JobStatus.RUNNING, 1)  # the catch-up run
    job_log(db, "forecasts.acquire", JobStatus.SKIPPED, 0.5)  # its duplicate
    report = data_health(db)
    acquire = by_job(report.out.jobs, "forecasts.acquire")
    assert acquire.status == "running" and acquire.last_status == "skipped"
    assert "job_overdue:forecasts.acquire" not in {p.key for p in report.problems}

    # A worker killed mid-job leaves its row "running"; the duplicates beat sends while
    # the dead run's lock lingers are skipped. That is still a stuck job.
    job_log(db, "quality.predict", JobStatus.SUCCESS, 120)
    job_log(db, "quality.predict", JobStatus.RUNNING, 50)  # hard time limit 30 min + 15
    for minutes in (40, 10):
        job_log(db, "quality.predict", JobStatus.SKIPPED, minutes)
    report = data_health(db)
    assert by_job(report.out.jobs, "quality.predict").status == "stuck"
    assert "job_stuck:quality.predict" in {p.key for p in report.problems}

    # A "running" row older than the newest result was left by a killed process earlier
    # and says nothing about now.
    job_log(db, "quality.predict", JobStatus.SUCCESS, 5)
    assert by_job(data_health(db).out.jobs, "quality.predict").status == "ok"


def test_a_window_of_skipped_runs_looks_back_to_the_last_result(db: Session) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    job_log(db, "matching.match", JobStatus.FAILED, 80, error="RuntimeError: synthetic")
    for i in range(30):  # more skipped rows than the window of 25 rows read per job
        job_log(db, "matching.match", JobStatus.SKIPPED, 60 - i)
    match = by_job(data_health(db).out.jobs, "matching.match")
    assert match.status == "failed" and match.consecutive_failures == 1
    assert match.last_status == "skipped"


def test_times_in_messages_are_utc_whatever_the_session_time_zone(db: Session) -> None:
    now = utcnow().replace(second=0, microsecond=0)
    run(db, "demo", 1, now=now)
    heartbeat(db, now=now)
    job_log(db, "detection.detect", JobStatus.FAILED, 30, now=now, error="synthetic")
    db.expire_all()  # read the rows back from the database, not from the session
    db.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
    try:
        failed = next(p for p in data_health(db, now=now).problems if p.key.startswith("job_"))
    finally:
        db.rollback()
    when = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M UTC")
    assert f"failed at {when}" in failed.message


def test_heartbeat_detects_a_dead_worker_or_scheduler(db: Session) -> None:
    now = utcnow()  # one clock for the rows and the report: the age is exact on any machine
    run(db, "demo", 1, now=now)
    assert "jobs_never_ran" in keys(db, now)
    job_log(db, "notifications.deliver", JobStatus.SUCCESS, 45, now=now)
    job_log(db, DATA_HEALTH_TASK, JobStatus.SUCCESS, 1, now=now)  # its own runs do not count
    report = data_health(db, now=now)
    assert report.status == "failing"
    assert report.problems[0].key == "jobs_not_running"
    assert report.out.heartbeat.age_minutes == pytest.approx(45 - 5 / 60, abs=0.1)


def test_map_fields_freshness_in_standalone_mode(db: Session, env: pytest.MonkeyPatch) -> None:
    set_env(env, MAP_FIELDS_ENABLED="true")
    run(db, "demo", 1)
    heartbeat(db)
    report = data_health(db)
    assert report.out.map_fields.mode == "standalone"
    assert report.out.map_fields.status == "missing" and "map_fields_missing" in keys(db)
    now = utcnow()
    cycle = (now - timedelta(hours=17)).replace(minute=0, second=0, microsecond=0)
    fields_run = run(db, "noaa_gfs_fields", 17, kind="fields")
    map_fields(db, fields_run, cycle)
    out = data_health(db).out.map_fields
    assert out.status == "stale" and out.complete_valid_times == 2
    assert out.last_valid_time == cycle + timedelta(hours=3)
    fresh_cycle = cycle + timedelta(hours=12)
    fresh = run(db, "noaa_gfs_fields", 5, kind="fields")
    map_fields(db, fresh, fresh_cycle)
    out = data_health(db).out.map_fields
    assert out.status == "ok" and out.cycle_time == fresh_cycle and out.run_key == fresh.run_key


def test_observation_freshness_per_provider(db: Session, env: pytest.MonkeyPatch) -> None:
    set_env(env, OBSERVATIONS_ENABLED="true", OBSERVATION_PROVIDERS="aodn,ndbc,metoffice")
    run(db, "demo", 1)
    heartbeat(db)
    now = utcnow()
    for code, provider, age in (("synthetic-a", "aodn", 3), ("synthetic-n", "ndbc", 60)):
        station = BuoyStation(
            code=code, provider=provider, name=f"synthetic {provider}", latitude=0, longitude=0
        )
        db.add(station)
        db.flush()
        for hours in (age, age + 1):
            db.add(
                BuoyObservation(
                    station_id=station.id,
                    observed_at=now - timedelta(hours=hours),
                    hs_m=1.0,
                    dataset="synthetic",
                )
            )
    db.commit()
    report = data_health(db)
    providers = {p.provider: p for p in report.out.observations.providers}
    assert providers["aodn"].status == "ok" and providers["aodn"].active_stations == 1
    assert providers["ndbc"].status == "stale" and providers["ndbc"].stale_after_hours == 48
    assert providers["metoffice"].status == "missing"
    assert {p.key for p in report.problems} == {
        "observations_stale:ndbc",
        "observations_missing:metoffice",
    }
    set_env(env, DATA_HEALTH_OBSERVATION_STALE_HOURS="72")
    assert "observations_stale:ndbc" not in keys(db)


def test_database_size_limit(db: Session, env: pytest.MonkeyPatch) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    report = data_health(db)
    assert report.status == "ok" and report.out.storage.database_bytes > 0
    assert report.out.storage.database_max_gb is None
    set_env(env, DATA_HEALTH_DATABASE_MAX_GB="0.000001")  # about 1 kB: always exceeded
    report = data_health(db)
    assert [p.key for p in report.problems] == ["database_size"]
    assert "DATA_HEALTH_DATABASE_MAX_GB" in report.out.reasons[0]


# --------------------------------------------------------------------------- API
def test_api_is_public_and_strict_mode_answers_503_when_failing(
    db: Session, app_client: ApiClient
) -> None:
    app_client.register("private.person@example.com")
    heartbeat(db)
    run(db, "demo", 20)
    job_log(db, "detection.detect", JobStatus.FAILED, 5, error="OperationalError at db-host-7")
    resp = app_client.get("/api/health/data")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.text.startswith('{"status":"failing"')
    body = resp.json()
    assert body["reasons"][0].startswith("No forecast source has a fresh run")
    assert "private.person" not in resp.text and "@" not in resp.text
    assert "db-host-7" not in resp.text  # raw job errors are not public
    assert app_client.get("/api/health/data?strict=true").status_code == 503
    run(db, "demo", 1)
    resp = app_client.get("/api/health/data?strict=true")
    assert resp.status_code == 200 and resp.json()["status"] == "degraded"  # the failed job


# --------------------------------------------------------------------------- alert job
def make_user(db: Session, email: str, *, admin: bool, verified: bool, active: bool = True) -> None:
    db.add(
        User(
            email=email,
            password_hash="synthetic-hash",
            full_name="Synthetic Person",
            is_admin=admin,
            is_active=active,
            email_verified_at=utcnow() if verified else None,
        )
    )
    db.commit()


def ops_emails() -> list[dict[str, object]]:
    return [m for m in outbox_messages() if m.get("tag") == "ops_alert"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Simulated time for the job runner, so job logs carry the test's ``now``."""
    return monkeypatch


def check(db: Session, clock: pytest.MonkeyPatch, now: datetime) -> dict[str, object]:
    """One ops.data_health run at ``now`` through the job runner (so its state persists),
    with every other scheduled job freshly logged as successful (a healthy scheduler)."""
    for job in expected_jobs(get_settings()):
        if job.name != DATA_HEALTH_TASK:
            job_log(db, job.name, JobStatus.SUCCESS, 1, now=now)
    clock.setattr(tasks, "utcnow", lambda: now)
    return tasks.run_job(DATA_HEALTH_TASK, lambda s: run_data_health_check(s, now=now), chain=False)


def test_alert_lifecycle_dedup_reminder_and_recovery(
    db: Session, clock: pytest.MonkeyPatch
) -> None:
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    make_user(db, "unverified-admin@example.com", admin=True, verified=False)
    make_user(db, "former-admin@example.com", admin=True, verified=True, active=False)
    make_user(db, "surfer@example.com", admin=False, verified=True)
    t0 = utcnow()
    run(db, "demo", 20, now=t0)

    first = check(db, clock, t0)
    assert first["status"] == "failing"
    assert first["alerts"] == ["new:forecasts_unavailable", "new:forecast_stale:demo"]
    (mail,) = ops_emails()
    assert mail["to"] == "ops-admin@example.com"  # verified, active admins only
    assert "Data health FAILING: 2 problems" in str(mail["subject"])
    assert "No forecast source has a fresh run" in str(mail["text"])

    for minutes in range(15, 12 * 60, 15):  # every beat run until the repeat interval
        result = check(db, clock, t0 + timedelta(minutes=minutes))
        assert result["alerts"] == [], minutes
    assert len(ops_emails()) == 1  # de-duplicated

    reminder = check(db, clock, t0 + timedelta(hours=12))
    assert reminder["alerts"] == ["reminder:forecasts_unavailable", "reminder:forecast_stale:demo"]
    assert len(ops_emails()) == 2 and "still unresolved" in str(ops_emails()[0]["text"])

    t1 = t0 + timedelta(hours=12, minutes=15)
    run(db, "demo", 1, now=t1)
    recovered = check(db, clock, t1)
    assert recovered["status"] == "ok"
    assert sorted(recovered["alerts"]) == [  # type: ignore[type-var]
        "recovered:forecast_stale:demo",
        "recovered:forecasts_unavailable",
    ]
    latest = ops_emails()[0]
    assert "recovered: all checks ok" in str(latest["subject"]) and "Resolved:" in str(
        latest["text"]
    )
    kept = recovered["alert_state"]  # kept until 12 h after the last alert, then dropped
    assert all(v["recovered_at"] and v["resolved_at"] for v in kept.values())  # type: ignore[union-attr]
    assert check(db, clock, t1 + timedelta(minutes=15))["alerts"] == []
    assert len(ops_emails()) == 3
    later = check(db, clock, t1 + timedelta(hours=12))["alert_state"]
    assert not set(kept) & set(later)  # type: ignore[arg-type]


def test_first_check_after_an_outage_does_not_report_itself_overdue(db: Session) -> None:
    run(db, "demo", 1)
    job_log(db, DATA_HEALTH_TASK, JobStatus.SUCCESS, 60 * 5)  # last run before a 5 h outage
    job_log(db, "notifications.deliver", JobStatus.SUCCESS, 60 * 5)
    heartbeat(db)  # the worker is back
    result = tasks.run_job(DATA_HEALTH_TASK, run_data_health_check, chain=False)
    assert f"job_overdue:{DATA_HEALTH_TASK}" not in result["problems"]  # type: ignore[operator]
    assert "job_overdue:alerts.generate" in result["problems"]  # type: ignore[operator]


def test_flapping_problem_sends_one_alert_and_one_recovery_per_interval(
    db: Session, clock: pytest.MonkeyPatch
) -> None:
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    t0 = utcnow()

    def flaky(now: datetime, failed: bool) -> dict[str, object]:
        run(db, "demo", 1, now=now)  # fresh forecasts throughout: only the job flaps
        result = check(db, clock, now)  # every job logged successful at ``now`` ...
        if failed:  # ... then flights.discover fails right after (synthetic)
            job_log(db, "flights.discover", JobStatus.FAILED, 1, now=now + timedelta(seconds=1))
            clock.setattr(tasks, "utcnow", lambda: now + timedelta(seconds=2))
            result = tasks.run_job(
                DATA_HEALTH_TASK,
                lambda s: run_data_health_check(s, now=now + timedelta(seconds=2)),
                chain=False,
            )
        return result

    assert flaky(t0, failed=True)["alerts"] == ["new:job_failed:flights.discover"]
    assert flaky(t0 + timedelta(minutes=15), failed=False)["alerts"] == [
        "recovered:job_failed:flights.discover"
    ]
    for minutes in range(30, 12 * 60, 15):
        result = flaky(t0 + timedelta(minutes=minutes), failed=minutes % 30 == 0)
        assert result["alerts"] == [], minutes
    assert len(ops_emails()) == 2  # one alert and one recovery notice in 12 h
    again = flaky(t0 + timedelta(hours=12), failed=True)
    assert again["alerts"] == ["recurred:job_failed:flights.discover"]
    assert "back again after it had cleared" in str(ops_emails()[0]["text"])


def test_no_admin_recipient_keeps_the_problem_due(db: Session, clock: pytest.MonkeyPatch) -> None:
    t0 = utcnow()
    run(db, "demo", 20, now=t0)
    first = check(db, clock, t0)
    assert first["email"] == {
        "due": True,
        "skipped": "no active admin account with a verified email address",
    }
    assert ops_emails() == []
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    second = check(db, clock, t0 + timedelta(minutes=15))
    assert second["alerts"] == ["unsent:forecasts_unavailable", "unsent:forecast_stale:demo"]
    assert len(ops_emails()) == 1 and "not yet reported" in str(ops_emails()[0]["text"])


def test_provider_failure_is_retried_on_the_next_run(
    db: Session, clock: pytest.MonkeyPatch
) -> None:
    from app.services.http import TransientProviderError
    from app.services.notifications import ops_alerts

    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    t0 = utcnow()
    run(db, "demo", 20, now=t0)

    class Down:
        def send(self, message: object) -> None:
            raise TransientProviderError("smtp down")

    working = ops_alerts.build_email_provider
    clock.setattr(ops_alerts, "build_email_provider", lambda: Down())
    first = check(db, clock, t0)
    assert first["email"]["sent"] == 0 and first["email"]["failed"] == 1  # type: ignore[index]
    assert all(v["last_alert_at"] is None for v in first["alert_state"].values())  # type: ignore[union-attr]
    assert ops_emails() == []
    clock.setattr(ops_alerts, "build_email_provider", working)
    second = check(db, clock, t0 + timedelta(minutes=15))
    assert second["email"]["sent"] == 1  # type: ignore[index]
    assert len(ops_emails()) == 1


def test_alerts_can_be_switched_off(
    db: Session, env: pytest.MonkeyPatch, clock: pytest.MonkeyPatch
) -> None:
    set_env(env, DATA_HEALTH_ALERTS_ENABLED="false")
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    run(db, "demo", 20)
    result = check(db, clock, utcnow())
    assert result["email"] == {"due": True, "skipped": "DATA_HEALTH_ALERTS_ENABLED=false"}
    assert ops_emails() == []


def test_beat_runs_the_task_through_the_worker_path(db: Session) -> None:
    assert DATA_HEALTH_TASK in celery_app.tasks
    entry = celery_app.conf.beat_schedule["data-health"]
    assert entry["task"] == DATA_HEALTH_TASK
    assert entry["schedule"] == timedelta(minutes=get_settings().data_health_check_minutes)
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    run(db, "demo", 20)
    heartbeat(db)
    result = tasks.data_health_task.delay().get()
    assert result["status"] == "failing"
    db.expire_all()
    log = db.scalar(select(BackgroundJobLog).where(BackgroundJobLog.job_name == DATA_HEALTH_TASK))
    assert log is not None and log.status == JobStatus.SUCCESS
    assert set(log.details["alert_state"]) == {"forecasts_unavailable", "forecast_stale:demo"}
    assert len(ops_emails()) == 1


def test_consecutive_beat_runs_share_their_alert_state(db: Session) -> None:
    """Beat sends ops.data_health every DATA_HEALTH_CHECK_MINUTES. Each run, through the
    task as the worker runs it, reads the state the previous run left in its job log: a
    persisting problem is emailed once, its recovery once, and nothing after that."""
    make_user(db, "ops-admin@example.com", admin=True, verified=True)
    run(db, "demo", 20)
    heartbeat(db)
    first = tasks.data_health_task.delay().get()
    assert first["alerts"] == ["new:forecasts_unavailable", "new:forecast_stale:demo"]
    again = tasks.data_health_task.delay().get()
    assert again["status"] == "failing" and again["alerts"] == []
    assert len(ops_emails()) == 1

    run(db, "demo", 1)  # a fresh (synthetic) run arrives
    recovered = tasks.data_health_task.delay().get()
    assert sorted(recovered["alerts"]) == [  # type: ignore[type-var]
        "recovered:forecast_stale:demo",
        "recovered:forecasts_unavailable",
    ]
    assert tasks.data_health_task.delay().get()["alerts"] == []
    assert len(ops_emails()) == 2
    db.expire_all()
    statuses = db.scalars(
        select(BackgroundJobLog.status).where(BackgroundJobLog.job_name == DATA_HEALTH_TASK)
    ).all()
    assert statuses == [JobStatus.SUCCESS] * 4
