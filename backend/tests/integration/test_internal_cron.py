"""GET /api/internal/cron/daily, the daily watchdog. Owned by the api-serverless package.

404 without CRON_SECRET (the body of an unknown route), 401 for a wrong token, otherwise
200 ``{status, problems, alerts_sent, heartbeat_age_minutes}`` with ``Cache-Control:
private, no-store``. The watchdog runs the ``ops.data_health`` job, so admins are emailed
about problems and the alert state persists between runs; it works with Redis refusing
connections. The admin account and the stale-forecast state here are synthetic test data.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.internal import CRON_DAILY_PATH
from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import get_redis, reset_redis
from app.jobs.schedule import DATA_HEALTH_TASK
from app.models import BackgroundJobLog, User
from app.models.enums import JobStatus
from tests.conftest import outbox_messages

pytestmark = pytest.mark.integration

SECRET = "s3cret-token-for-the-watchdog"
AUTH = {"Authorization": f"Bearer {SECRET}"}
BODY_KEYS = {"status", "problems", "alerts_sent", "heartbeat_age_minutes"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(get_settings(), "cron_secret", SECRET)
    return SECRET


def make_admin(db: Session, email: str = "ops-admin@example.com") -> None:
    db.add(
        User(
            email=email,
            password_hash="synthetic-hash",
            full_name="Synthetic Admin",
            is_admin=True,
            is_active=True,
            email_verified_at=utcnow(),
        )
    )
    db.commit()


def ops_emails() -> list[dict[str, object]]:
    return [m for m in outbox_messages() if m.get("tag") == "ops_alert"]


def watchdog_logs(db: Session) -> list[BackgroundJobLog]:
    db.expire_all()
    return list(
        db.scalars(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == DATA_HEALTH_TASK)
            .order_by(BackgroundJobLog.id)
        )
    )


def test_the_path_is_the_vercel_cron_path() -> None:
    assert CRON_DAILY_PATH == "/api/internal/cron/daily"


def test_cron_route_is_absent_without_a_secret() -> None:
    from app.main import app

    client = TestClient(app)
    assert get_settings().cron_secret is None
    unknown = client.get("/api/internal/no-such-route")
    assert unknown.status_code == 404
    for headers in ({}, {"Authorization": "Bearer anything-at-all-123"}):
        r = client.get(CRON_DAILY_PATH, headers=headers)
        assert r.status_code == 404 and r.json() == unknown.json()
    assert CRON_DAILY_PATH not in client.get("/openapi.json").json()["paths"]


def test_cron_route_checks_the_bearer_token(db: Session, client: TestClient, secret: str) -> None:
    for value in (
        None,
        secret,
        f"Basic {secret}",
        "Bearer wrong-token-1234567",
        "Bearer ",
        f"Bearer {secret}x",
        f"Bearer {secret[:-1]}",
    ):
        headers = {"Authorization": value} if value is not None else {}
        assert client.get(CRON_DAILY_PATH, headers=headers).status_code == 401, value
    assert watchdog_logs(db) == []  # nothing ran for a refused call
    ok = client.get(CRON_DAILY_PATH, headers={"Authorization": f"Bearer {secret}"})
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "private, no-store"
    assert "cdn-cache-control" not in ok.headers
    body = ok.json()
    assert set(body) == BODY_KEYS
    assert body["status"] in {"ok", "degraded", "failing"}
    assert isinstance(body["problems"], list) and body["alerts_sent"] == []  # no admin
    assert body["heartbeat_age_minutes"] is None  # no daemon heartbeat in Redis
    (log,) = watchdog_logs(db)  # one ops.data_health run, logged by the job runner
    assert log.status == JobStatus.SUCCESS and log.details["status"] == body["status"]
    assert log.details["problems"] == body["problems"]


def test_admins_are_alerted_once_when_health_is_degraded(
    db: Session, client: TestClient, secret: str
) -> None:
    """The test database has no forecast run, so health reports problems (not ok)."""
    make_admin(db)
    first = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] != "ok" and body["problems"]
    assert body["alerts_sent"] and all(a.startswith("new:") for a in body["alerts_sent"])
    assert {a.removeprefix("new:") for a in body["alerts_sent"]} <= set(body["problems"])
    (mail,) = ops_emails()
    assert mail["to"] == "ops-admin@example.com"
    assert "Data health" in str(mail["subject"])

    # A duplicate delivery of the same cron (Vercel may send one) alerts nobody twice:
    # the alert state was stored in the job's log row.
    again = client.get(CRON_DAILY_PATH, headers=AUTH).json()
    assert again["problems"] == body["problems"] and again["alerts_sent"] == []
    assert len(ops_emails()) == 1
    assert [log.status for log in watchdog_logs(db)] == [JobStatus.SUCCESS] * 2


def test_the_daemon_heartbeat_age_is_reported(
    db: Session, client: TestClient, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.sync import state

    three_hours_ago = utcnow() - timedelta(hours=3)
    with monkeypatch.context() as clock:
        clock.setattr(state, "utcnow", lambda: three_hours_ago)
        state.write_heartbeat(None, runner="daemon", note="synthetic heartbeat")
    age = client.get(CRON_DAILY_PATH, headers=AUTH).json()["heartbeat_age_minutes"]
    assert 179.5 <= age <= 181


def test_another_run_holding_the_job_lock_is_skipped(
    db: Session, client: TestClient, secret: str
) -> None:
    get_redis().set(f"lock:job:{DATA_HEALTH_TASK}", "held-by-the-daemon", ex=60)
    body = client.get(CRON_DAILY_PATH, headers=AUTH).json()
    assert body["status"] == "skipped" and body["alerts_sent"] == []
    assert [log.status for log in watchdog_logs(db)] == [JobStatus.SKIPPED]


@pytest.fixture
def redis_refused(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """REDIS_URL points at a local port nothing listens on (connections refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with monkeypatch.context() as patch:
        patch.setattr(get_settings(), "redis_url", f"redis://127.0.0.1:{port}/0")
        reset_redis()
        yield
    reset_redis()


def test_the_watchdog_alerts_with_redis_down(
    db: Session, redis_refused: None, client: TestClient, secret: str
) -> None:
    """Upstash refusing commands (quota, outage) must not silence the watchdog: the check
    runs without its Redis lock and still emails the admins."""
    make_admin(db)
    resp = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] != "ok" and body["alerts_sent"]
    assert "redis_unavailable" in body["problems"]
    # Redis being down is not reported as a failed watchdog run (no such run was logged).
    assert f"job_failed:{DATA_HEALTH_TASK}" not in body["problems"]
    assert body["heartbeat_age_minutes"] is None
    assert len(ops_emails()) == 1
    (ran,) = watchdog_logs(db)  # Redis did not answer: only the unlocked run, logged
    assert ran.status == JobStatus.SUCCESS and ran.details["lock"].startswith("not taken")
    assert ran.details["alert_state"]  # the next run reads it


def test_redis_failing_after_the_ping_still_gets_the_check_run(
    db: Session,
    redis_refused: None,
    client: TestClient,
    secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis answered the PING, then refused the job lock: the locked attempt is logged
    FAILED and the check runs without the lock."""
    import app.api.internal as internal

    monkeypatch.setattr(internal, "_redis_reachable", lambda: True)
    make_admin(db)
    body = client.get(CRON_DAILY_PATH, headers=AUTH).json()
    assert body["status"] != "ok" and body["alerts_sent"]
    assert len(ops_emails()) == 1
    failed, ran = watchdog_logs(db)  # the locked attempt failed, the unlocked one ran
    assert failed.status == JobStatus.FAILED and "ConnectionError" in (failed.error_message or "")
    assert ran.status == JobStatus.SUCCESS and ran.details["lock"].startswith("not taken")


def test_an_unexpected_failure_answers_503(
    db: Session, client: TestClient, secret: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.notifications import ops_alerts

    def broken(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("synthetic failure at db-host-7")

    monkeypatch.setattr(ops_alerts, "run_data_health_check", broken)
    resp = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.json() == {
        "status": "error",
        "problems": ["watchdog_failed:RuntimeError"],
        "alerts_sent": [],
        "heartbeat_age_minutes": None,
    }
    assert "db-host-7" not in resp.text  # no internal detail in the response
    (log,) = watchdog_logs(db)
    assert log.status == JobStatus.FAILED
