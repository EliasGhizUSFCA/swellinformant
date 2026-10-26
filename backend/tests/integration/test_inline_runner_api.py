"""Account emails and phone codes under JOB_RUNNER=daemon with Redis unreachable. Owned by
the api-serverless package.

The hosted API has no Celery worker: register, resend-verification, forgot-password and
the phone code send their message inline (app.workers.dispatch). Redis (rate limits, kick
flags) being down must neither block nor slow these requests: before Upgrade 4 a
registration with Redis refusing connections waited 19 s for Celery's result backend, and
with Redis blackholed it hung. Each request here finishes in under 3 s with the message in
the console outbox and its delivery recorded. Accounts and addresses are synthetic.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterator

import pytest
import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis import get_redis, reset_redis
from app.models import EmailMessage
from app.models.enums import EmailDeliveryStatus, EmailPurpose
from app.workers import dispatch
from tests.conftest import ApiClient, latest_link_token, outbox_messages, search_payload

pytestmark = pytest.mark.integration

# The acceptance bound of a request with Redis unreachable (design: api-serverless).
MAX_SECONDS = 3.0
PASSWORD = "Barrels4Days!"
# The hosted profile's Redis timeouts (app.core.profiles.HOSTED_PROFILE).
HOSTED_CONNECT_TIMEOUT = 1.5
HOSTED_SOCKET_TIMEOUT = 2.0


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def daemon_runner(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """JOB_RUNNER=daemon with rate limits on (they fail open without Redis); any attempt
    to reach Celery fails the test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "job_runner", "daemon")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "redis_connect_timeout_seconds", HOSTED_CONNECT_TIMEOUT)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", HOSTED_SOCKET_TIMEOUT)

    def no_celery() -> None:
        pytest.fail("Celery was used under JOB_RUNNER=daemon")

    monkeypatch.setattr(dispatch, "_celery_tasks", no_celery)


@pytest.fixture(params=["refused", "blackholed"])
def redis_down(
    request: pytest.FixtureRequest, daemon_runner: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """REDIS_URL points at a closed local port (connections refused at once) or at an
    unroutable address (each connect waits for the connect timeout)."""
    url = {
        "refused": f"redis://127.0.0.1:{closed_port()}/0",
        "blackholed": "redis://10.255.255.1:6379/0",
    }[request.param]
    with monkeypatch.context() as patch:
        patch.setattr(get_settings(), "redis_url", url)
        reset_redis()
        yield request.param
    reset_redis()


def timed_post(client: ApiClient, url: str, body: object) -> tuple[int, float]:
    started = time.monotonic()
    resp = client.post(url, body)
    return resp.status_code, time.monotonic() - started


def account_emails(db: Session, purpose: EmailPurpose) -> list[EmailMessage]:
    db.expire_all()
    return list(
        db.scalars(
            select(EmailMessage)
            .where(EmailMessage.purpose == purpose)
            .order_by(EmailMessage.created_at)
        )
    )


def test_register_resend_and_reset_with_redis_down(
    redis_down: str, app_client: ApiClient, db: Session
) -> None:
    with pytest.raises(redis.RedisError):  # unreachable
        get_redis().ping()
    email = f"inline-{redis_down}@example.com"
    status, elapsed = timed_post(
        app_client,
        "/api/auth/register",
        {
            "full_name": "Inline Surfer",
            "email": email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    assert status == 201 and elapsed < MAX_SECONDS, (status, elapsed)
    (verify,) = account_emails(db, EmailPurpose.VERIFY_EMAIL)
    assert verify.status == EmailDeliveryStatus.CAPTURED and verify.attempts == 1
    assert verify.provider == "console" and verify.sent_at is not None
    assert latest_link_token("verify-email")  # the link is in the console outbox

    status, elapsed = timed_post(app_client, "/api/auth/resend-verification", None)
    assert status == 200 and elapsed < MAX_SECONDS, (status, elapsed)
    status, elapsed = timed_post(
        app_client, "/api/auth/resend-verification-public", {"email": email}
    )
    assert status == 200 and elapsed < MAX_SECONDS, (status, elapsed)
    verifies = account_emails(db, EmailPurpose.VERIFY_EMAIL)
    assert [row.status for row in verifies] == [EmailDeliveryStatus.CAPTURED] * 3

    status, elapsed = timed_post(app_client, "/api/auth/forgot-password", {"email": email})
    assert status == 200 and elapsed < MAX_SECONDS, (status, elapsed)
    (reset,) = account_emails(db, EmailPurpose.PASSWORD_RESET)
    assert reset.status == EmailDeliveryStatus.CAPTURED
    assert latest_link_token("reset-password")

    mails = [m for m in outbox_messages() if m["kind"] == "email" and m["to"] == email]
    assert len(mails) == 4  # three verification links and one reset link


def test_phone_code_with_redis_down(redis_down: str, app_client: ApiClient) -> None:
    app_client.register(email=f"phone-{redis_down}@example.com")
    status, elapsed = timed_post(
        app_client, "/api/users/me/phone/start", {"phone_number": "+14155550123"}
    )
    assert status == 200 and elapsed < MAX_SECONDS, (status, elapsed)
    sms = next(m for m in outbox_messages() if m["kind"] == "sms")
    assert sms["to"] == "+14155550123" and "verification code" in str(sms["text"])


def test_a_failing_sms_provider_is_reported_to_the_user(
    daemon_runner: None, app_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline SMS: three transient failures (a synthetic provider outage) answer 503 with
    a message, after the 0.5 s and 1.5 s pauses, instead of a silent loss."""
    from app.services.http import TransientProviderError
    from app.services.notifications import transactional

    pauses: list[float] = []
    monkeypatch.setattr(dispatch, "_pause", pauses.append)

    def outage(to: str, body: str) -> str:
        raise TransientProviderError("synthetic: SMS provider unavailable")

    app_client.register(email="phone-outage@example.com")
    monkeypatch.setattr(transactional, "deliver_sms", outage)
    resp = app_client.post("/api/users/me/phone/start", {"phone_number": "+14155550123"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "sms_not_sent"
    assert "synthetic" not in resp.text  # the provider's detail stays in the logs
    assert pauses == list(dispatch.INLINE_BACKOFF_SECONDS)


def test_the_api_records_kicks_for_the_daemon(
    make_client: Callable[[], ApiClient],
    db: Session,
    few_spots: list[str],
    only_simulated_swells: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under JOB_RUNNER=daemon, "match my search" (create, update, resume) and "refresh
    flights" leave Redis kick flags for the daemon (app.services.sync.state), never Celery
    tasks; pausing a search asks for nothing. The opportunity comes from the demo pipeline
    and a simulated swell (synthetic data)."""
    from app.services.pipeline import run_pipeline
    from app.services.sync import state

    owner: ApiClient = make_client()
    owner.register("kicks@example.com")
    created = owner.post("/api/searches", search_payload())
    assert created.status_code == 201, created.text
    run_pipeline(db)
    sim = owner.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    (top, *_) = owner.get("/api/opportunities").json()

    settings = get_settings()
    monkeypatch.setattr(settings, "job_runner", "daemon")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    def no_celery(*_: object, **__: object) -> None:
        pytest.fail("Celery was used under JOB_RUNNER=daemon")

    from app.workers.celery_app import celery_app

    monkeypatch.setattr(celery_app, "signature", no_celery)
    monkeypatch.setattr(dispatch, "_celery_tasks", no_celery)
    assert state.pop_kicks() == {}  # nothing left over from the setup

    search_id = created.json()["id"]
    for step, call in (
        ("create", lambda: owner.post("/api/searches", search_payload(name="Second search"))),
        ("update", lambda: owner.put(f"/api/searches/{search_id}", search_payload())),
        ("resume", lambda: owner.post(f"/api/searches/{search_id}/resume")),
    ):
        assert call().status_code in (200, 201), step
        kicks = state.pop_kicks()
        assert list(kicks) == ["matching.match"], (step, kicks)
    assert owner.post(f"/api/searches/{search_id}/pause").status_code == 200
    assert state.pop_kicks() == {}

    refreshed = owner.post(f"/api/opportunities/{top['id']}/refresh-flights")
    assert refreshed.status_code == 200, refreshed.text
    assert state.pop_kicks() == {"flights.discover": [top["id"]]}


def test_a_transient_email_failure_is_retried_inline(
    daemon_runner: None, app_client: ApiClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider fails once (synthetic), the second attempt succeeds: one record,
    two attempts, CAPTURED."""
    from app.services.http import TransientProviderError
    from app.services.notifications import providers

    pauses: list[float] = []
    monkeypatch.setattr(dispatch, "_pause", pauses.append)
    real = providers.build_email_provider
    calls: list[int] = []

    class FlakyOnce:
        def send(self, message: providers.EmailMessageData) -> providers.SendResult:
            calls.append(1)
            if len(calls) == 1:
                raise TransientProviderError("synthetic: 503 from the provider")
            return real().send(message)

    monkeypatch.setattr(
        "app.services.notifications.transactional.build_email_provider", lambda: FlakyOnce()
    )
    app_client.register(email="flaky@example.com", verify=False)
    (row,) = account_emails(db, EmailPurpose.VERIFY_EMAIL)
    assert row.status == EmailDeliveryStatus.CAPTURED and row.attempts == 2
    assert row.last_error is None and pauses == [0.5]
