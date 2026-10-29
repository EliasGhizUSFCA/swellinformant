"""The hosted profile without its optional daemon (Swell 2.0 design D8; review r1-backend-F3,
r1-spec-SPEC-2).

On Vercel Hobby the sync daemon only refreshes the GRIB map layers (SYNC_DAEMON_KINDS=map)
and may not run at all: the refresh endpoint refreshes the spot forecasts, and with
JOB_RUNNER=daemon it also takes the API's kicks (an explicit "Re-check flights", a new saved
search to match) and retries a due notification; the daily cron runs the periodic jobs no
daemon ran on time (app.services.sync.standin). Data health then reports no stopped runner.

Users, searches, swells and fares are SYNTHETIC test data (demo providers).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.internal import CRON_DAILY_PATH, REFRESH_PATH
from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import get_redis
from app.jobs.schedule import DATA_HEALTH_TASK
from app.models import BackgroundJobLog, FlightOffer, FlightSearch, Notification, OpportunityMatch
from app.models.enums import JobStatus, NotificationStatus
from app.services.health import STANDIN_GAP, data_health, expected_jobs
from app.services.pipeline import run_pipeline
from app.services.sync import orchestrator, standin
from app.services.sync import state as sync_state
from tests.conftest import ApiClient, search_payload
from tests.integration.test_flight_targeting import CallLog, calls, swell  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("only_simulated_swells")]

SECRET = "s3cret-token-for-the-daemonless-tests"
AUTH = {"Authorization": f"Bearer {SECRET}"}
DELIVER = standin.DELIVER_JOB


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The hosted profile's runner settings, no daemon running (no heartbeat in Redis)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "job_runner", "daemon")
    monkeypatch.setattr(settings, "sync_daemon_kinds", "map")
    monkeypatch.setattr(settings, "map_fields_enabled", False)
    monkeypatch.setattr(settings, "cron_secret", SECRET)
    monkeypatch.setattr(settings, "tide_providers", "harmonic_model")  # local, no request
    get_redis().delete(sync_state.HEARTBEAT_KEY)
    # The data-health check just ran (as after the daily cron): the tests of the other
    # background work do not see it run as well; test_the_admins_hear_of_failed_refreshes...
    # deletes the key.
    standin.note_data_health(utcnow())
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


def job_rows(db: Session, name: str) -> int:
    db.expire_all()
    return int(db.scalar(select(func.count()).where(BackgroundJobLog.job_name == name)) or 0)


def test_an_explicit_flight_request_is_run_by_the_next_refresh_call(
    db: Session,
    few_spots: list[str],
    calls: CallLog,  # noqa: F811
    make_client: Callable[[], ApiClient],
    client: TestClient,
    hosted: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_client()
    owner.register("daemonless@example.com")
    assert owner.post("/api/searches", search_payload()).status_code == 201
    swell(db)
    monkeypatch.setattr(get_settings(), "job_runner", "celery")  # the pipeline, as before
    run_pipeline(db, acquire=False)
    monkeypatch.setattr(get_settings(), "job_runner", "daemon")
    # The new search's own kick (matching.match) is covered by the test below.
    assert set(sync_state.pop_kicks()) == {"matching.match"}
    (match,) = db.scalars(select(OpportunityMatch)).all()
    past = utcnow() - timedelta(minutes=1)  # every quote and cache entry has expired
    db.execute(update(FlightOffer).values(offer_expires_at=past))
    db.execute(update(FlightSearch).values(expires_at=past))
    db.commit()
    before = len(calls.searches)
    refreshed = owner.post(f"/api/opportunities/{match.id}/refresh-flights")
    assert refreshed.status_code == 200, refreshed.text
    assert len(calls.searches) == before  # only recorded: no daemon takes it
    # The next scheduler call takes the kick (the forecast refresh itself is switched off
    # here so that its own flight stage cannot be what searched: the kill switch stops
    # syncs, not the kicks).
    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    out = client.post(REFRESH_PATH, headers=AUTH)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["status"] == "disabled"
    kicks = body["background"]["kicks"]
    assert kicks["kicks"] == {"flights.discover": 1}
    assert [j["job"] for j in kicks["jobs"]][:1] == ["flights.discover"]
    assert kicks["jobs"][0]["status"] == "success"
    assert len(calls.searches) > before
    fresh = owner.get(f"/api/opportunities/{match.id}").json()
    assert fresh["best_offer_expired"] is False
    # Taken once: the next call finds no kick.
    again = client.post(REFRESH_PATH, headers=AUTH).json()
    assert "background" not in again


def test_a_new_search_is_matched_by_the_next_refresh_call(
    db: Session, few_spots: list[str], hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_state.request_kick("matching.match")
    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    out = orchestrator.refresh_check(trigger="http")
    jobs = [j["job"] for j in out["background"]["kicks"]["jobs"]]
    # matching.match and, as the daemon chains them, the stages after it.
    assert jobs == ["matching.match", "flights.discover", "alerts.generate", DELIVER]
    assert job_rows(db, "matching.match") == 1
    assert sync_state.pop_kicks() == {}


def test_a_due_delivery_retry_is_run_by_the_refresh_call(
    db: Session, app_client: ApiClient, hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models import User

    app_client.register("retry@example.com")
    user = db.scalar(select(User).where(User.email == "retry@example.com"))
    assert user is not None
    n = Notification(
        user_id=user.id,
        channel="email",
        kind="new_opportunity",
        status=NotificationStatus.QUEUED,
        dedup_key="synthetic-retry",
        recipient=user.email,
        subject="Synthetic alert",
        body_text="Synthetic alert body",
        snapshot={},
        events=[],
        attempts=1,
        next_attempt_at=utcnow() + timedelta(minutes=10),  # a temporary failure's backoff
    )
    db.add(n)
    db.commit()
    due = standin.note_next_delivery(db)
    assert due is not None and not standin.delivery_due(utcnow())
    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    assert "background" not in orchestrator.refresh_check(trigger="http")
    # The backoff has passed.
    db.execute(update(Notification).values(next_attempt_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    standin.note_next_delivery(db)
    out = orchestrator.refresh_check(trigger="http")
    assert out["background"]["delivery"]["jobs"] == [{"job": DELIVER, "status": "success"}]
    db.expire_all()
    assert db.get(Notification, n.id).status != NotificationStatus.QUEUED  # type: ignore[union-attr]
    assert get_redis().get(standin.DELIVERY_DUE_KEY) is None  # nothing queued any more


def test_the_daily_cron_runs_the_jobs_no_daemon_ran_once(
    db: Session,
    few_spots: list[str],
    client: TestClient,
    hosted: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.tides import TidePrediction

    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    db.execute(BackgroundJobLog.__table__.delete())
    db.commit()
    out = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert out.status_code == 200, out.text
    jobs = out.json()["jobs"]
    ran = [j["job"] for j in jobs["jobs"]]
    db.expire_all()
    logged = {
        name: status.value
        for name, status in db.execute(
            select(BackgroundJobLog.job_name, BackgroundJobLog.status).where(
                BackgroundJobLog.job_name != DATA_HEALTH_TASK
            )
        )
    }
    for name in (
        "maintenance.cleanup",
        "matching.match",
        "tides.acquire",
        "alerts.generate",
        DELIVER,
        "forecasts.check_staleness",
    ):
        assert name in ran and logged.get(name) == "success", (name, jobs, logged)
    assert DATA_HEALTH_TASK not in ran  # the watchdog's own job ran before
    assert jobs["deferred"] == []
    db.expire_all()
    assert (db.scalar(select(func.count()).select_from(TidePrediction)) or 0) > 0
    # Nothing is run twice: a second call finds every job recent.
    second = client.get(CRON_DAILY_PATH, headers=AUTH).json()["jobs"]
    assert second["overdue"] == [] and second["jobs"] == []
    assert job_rows(db, "maintenance.cleanup") == 1


@pytest.mark.parametrize("earlier", [timedelta(0), timedelta(minutes=50)])
def test_the_daily_jobs_run_on_every_daily_cron(
    db: Session, hosted: None, earlier: timedelta
) -> None:
    """Review r2-backend-N1: each job of a daily cron's run starts after the run's ``now``
    (the watchdog and the jobs before it first), so with a strict 24 h interval the next
    day's cron at the same time, or up to an hour earlier (Vercel Hobby fires within the
    hour), skipped the 24 h jobs, which then ran every other day. SYNTHETIC job-log rows:
    day 1's run began at 03:00:03; the jobs started 0-12 s after it."""
    from datetime import UTC, datetime

    db.execute(BackgroundJobLog.__table__.delete())
    start = datetime(2026, 10, 27, 3, 0, 3, tzinfo=UTC)
    offsets = {
        "maintenance.cleanup": 0,
        "matching.match": 4,
        "flights.discover": 5,
        "alerts.generate": 6,
        DELIVER: 7,
        "tides.acquire": 9,
        "forecasts.check_staleness": 12,
    }
    for name, seconds in offsets.items():
        at = start + timedelta(seconds=seconds)
        db.add(
            BackgroundJobLog(
                job_name=name,
                status=JobStatus.SUCCESS,
                started_at=at,
                finished_at=at + timedelta(seconds=1),
                duration_ms=1000,
                details={},
            )
        )
    db.commit()
    due = standin.overdue_jobs(db, start + timedelta(days=1) - earlier, get_settings())
    for name in ("maintenance.cleanup", "matching.match", "tides.acquire"):
        assert name in due, (name, due)
    # The slack is not a second run the same day: an hour later nothing daily is due.
    later = standin.overdue_jobs(db, start + timedelta(hours=1), get_settings())
    assert not {"maintenance.cleanup", "matching.match", "tides.acquire"} & set(later), later


def test_the_admins_hear_of_failed_refreshes_without_a_daemon_or_the_daily_cron(
    db: Session, few_spots: list[str], hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2-spec-SPEC-3: without the daemon, ops.data_health (every spec-16 admin
    alert) ran only from the daily cron. The refresh endpoint now runs it once
    DATA_HEALTH_CHECK_MINUTES have passed and at once after a failed refresh: three failed
    refreshes in a row (SYNTHETIC provider outage) email refresh_failures to the admins
    before any daily cron; a check with nothing due runs no check."""
    from app.models import User
    from app.services.forecasts.demo import DemoForecastProvider
    from app.services.http import TransientProviderError
    from tests.conftest import outbox_messages

    db.execute(BackgroundJobLog.__table__.delete())
    db.add(
        User(
            email="standin-admin@example.com",
            password_hash="synthetic-hash",
            full_name="Synthetic Admin",
            is_admin=True,
            is_active=True,
            email_verified_at=utcnow(),
        )
    )
    db.commit()
    get_redis().delete(standin.DATA_HEALTH_AT_KEY)

    def outage(self: object, *args: object, **kwargs: object) -> object:
        raise TransientProviderError("synthetic outage: HTTP 503 from the forecast provider")

    monkeypatch.setattr(DemoForecastProvider, "fetch", outage)
    retry = timedelta(minutes=get_settings().sync_retry_minutes + 1)
    start = utcnow()
    for i in range(3):
        out = orchestrator.refresh_check(trigger="http", now=start + i * retry)
        assert out["status"] == "failed", out
        assert out["background"]["data_health"]["jobs"][0]["job"] == DATA_HEALTH_TASK
    sent = [m for m in outbox_messages() if m.get("tag") == "ops_alert"]
    assert any("forecast refreshes failed" in str(m["text"]) for m in sent), sent
    assert {m["to"] for m in sent} == {"standin-admin@example.com"}
    assert job_rows(db, DATA_HEALTH_TASK) == 3
    # No failure (the refresh switched off, as when nothing is due): no check until
    # DATA_HEALTH_CHECK_MINUTES have passed.
    monkeypatch.setattr(get_settings(), "forecast_refresh_enabled", False)
    quiet = orchestrator.refresh_check(trigger="http", now=start + 2 * retry + timedelta(minutes=1))
    assert quiet["status"] == "disabled" and "data_health" not in quiet.get("background", {})
    assert job_rows(db, DATA_HEALTH_TASK) == 3
    hourly = start + 2 * retry + timedelta(minutes=get_settings().data_health_check_minutes + 1)
    due = orchestrator.refresh_check(trigger="http", now=hourly)
    assert due["background"]["data_health"]["jobs"][0]["job"] == DATA_HEALTH_TASK, due
    assert job_rows(db, DATA_HEALTH_TASK) == 4
    # While a daemon runs, it runs the check on its own timer.
    get_redis().delete(standin.DATA_HEALTH_AT_KEY)
    get_redis().set(sync_state.HEARTBEAT_KEY, "{}", ex=60)
    assert standin.run_due_data_health(hourly, get_settings(), urgent=True) is None


def test_data_health_reports_no_stopped_runner_without_the_optional_daemon(
    db: Session, hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_state.write_refresh_check({"caller": "refresh", "trigger": "http", "status": "not_due"})
    report = data_health(db)
    keys = {p.key: p.severity for p in report.problems}
    assert "sync_heartbeat_stale" not in keys
    assert report.out.sync is not None and report.out.sync.last_check_at is not None
    assert not report.out.sync.heartbeat_stale
    # The periodic jobs are expected daily (the cron), not on the daemon's timers.
    gaps = {j.name: j.max_gap for j in expected_jobs(get_settings())}
    assert gaps["alerts.generate"] == STANDIN_GAP and gaps[DELIVER] == STANDIN_GAP
    # With map layers to sync (standalone map-field mode) a missing daemon only degrades.
    from app.services import health

    monkeypatch.setattr(health, "map_fields_mode", lambda settings: "standalone")
    keys = {p.key: p.severity for p in data_health(db).problems}
    assert keys.get("sync_heartbeat_stale") == "degraded"
    # A daemon that syncs the spots (the classic daemon) is still required.
    monkeypatch.setattr(get_settings(), "sync_daemon_kinds", "spots,ensemble,map")
    keys = {p.key: p.severity for p in data_health(db).problems}
    assert keys.get("sync_heartbeat_stale") == "failing"
