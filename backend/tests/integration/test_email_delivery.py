"""Account-email delivery records, provider failures, webhooks and the dev test email."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import EmailMessage, Notification, User
from app.models.enums import EmailDeliveryStatus, EmailPurpose, NotificationStatus
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications import providers as prov
from tests.conftest import ApiClient

SECRET = "whsec_" + base64.b64encode(b"integration-signing-key").decode()


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    get_settings.cache_clear()


def set_env(mp: pytest.MonkeyPatch, **values: str) -> None:
    for k, v in values.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def messages(db: Session, purpose: EmailPurpose | None = None) -> list[EmailMessage]:
    db.expire_all()
    stmt = select(EmailMessage).order_by(EmailMessage.created_at)
    if purpose:
        stmt = stmt.where(EmailMessage.purpose == purpose)
    return list(db.scalars(stmt).all())


def test_registration_records_the_verification_email(app_client: ApiClient, db: Session) -> None:
    app_client.register(verify=False)
    [row] = messages(db, EmailPurpose.VERIFY_EMAIL)
    assert row.status == EmailDeliveryStatus.CAPTURED  # console provider: captured, not delivered
    assert row.provider == "console" and row.attempts == 1 and row.sent_at is not None
    assert row.delivered_at is None


def test_transient_provider_failure_retries_then_fails_visibly(
    app_client: ApiClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def flaky(self: object, message: object) -> prov.SendResult:
        calls["n"] += 1
        raise TransientProviderError("SMTP unavailable: connection refused")

    monkeypatch.setattr(prov.ConsoleEmailProvider, "send", flaky)
    app_client.register(verify=False)
    [row] = messages(db, EmailPurpose.VERIFY_EMAIL)
    assert row.status == EmailDeliveryStatus.FAILED
    assert calls["n"] == row.attempts == 6  # first try + 5 retries
    assert "connection refused" in (row.last_error or "")
    status = app_client.get("/api/auth/verification-status").json()
    assert (
        status["last_email"]["status"] == "failed"
        and "connection refused" in status["last_email"]["error"]
    )


def test_permanent_provider_failure_is_not_retried(
    app_client: ApiClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(self: object, message: object) -> prov.SendResult:
        raise PermanentProviderError("resend authentication failed (HTTP 401)")

    monkeypatch.setattr(prov.ConsoleEmailProvider, "send", reject)
    app_client.register(verify=False)
    [row] = messages(db, EmailPurpose.VERIFY_EMAIL)
    assert row.status == EmailDeliveryStatus.FAILED and row.attempts == 1


def test_resend_webhook_marks_delivered_and_rejects_bad_signatures(
    app_client: ApiClient, db: Session, settings_env: pytest.MonkeyPatch
) -> None:
    app_client.register(verify=False)
    [row] = messages(db, EmailPurpose.VERIFY_EMAIL)
    row.provider, row.provider_message_id, row.status = (
        "resend",
        "re_abc",
        EmailDeliveryStatus.ACCEPTED,
    )
    db.commit()

    body = json.dumps(
        {
            "type": "email.delivered",
            "created_at": utcnow().isoformat(),
            "data": {"email_id": "re_abc"},
        }
    ).encode()
    assert app_client.client.post("/api/webhooks/resend", content=body).status_code == 404  # off
    set_env(settings_env, RESEND_WEBHOOK_SECRET=SECRET)
    stamp = str(int(utcnow().timestamp()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"m1.{stamp}.".encode() + body, hashlib.sha256).digest())
    headers = {"svix-id": "m1", "svix-timestamp": stamp, "svix-signature": f"v1,{sig.decode()}"}
    bad = {**headers, "svix-signature": "v1,AAAA"}
    assert (
        app_client.client.post("/api/webhooks/resend", content=body, headers=bad).status_code == 403
    )
    ok = app_client.client.post("/api/webhooks/resend", content=body, headers=headers)
    assert ok.status_code == 200 and ok.json() == {"matched": 1}
    db.expire_all()
    row = db.get(EmailMessage, row.id)  # type: ignore[assignment]
    assert row.status == EmailDeliveryStatus.DELIVERED and row.delivered_at is not None
    assert row.events[-1]["type"] == "email.delivered"


def test_webhook_updates_alert_notifications_too(
    app_client: ApiClient, db: Session, settings_env: pytest.MonkeyPatch
) -> None:
    app_client.register()
    user = db.scalar(select(User))
    assert user is not None
    n = Notification(
        user_id=user.id,
        channel="email",
        kind="new_opportunity",
        status=NotificationStatus.ACCEPTED,
        dedup_key="test-key",
        recipient=user.email,
        subject="s",
        body_text="t",
        snapshot={},
        provider="resend",
        provider_message_id="re_alert",
        events=[],
    )
    db.add(n)
    db.commit()
    set_env(settings_env, RESEND_WEBHOOK_SECRET=SECRET)
    body = json.dumps({"type": "email.bounced", "data": {"email_id": "re_alert"}}).encode()
    stamp = str(int(utcnow().timestamp()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"m2.{stamp}.".encode() + body, hashlib.sha256).digest())
    r = app_client.client.post(
        "/api/webhooks/resend",
        content=body,
        headers={"svix-id": "m2", "svix-timestamp": stamp, "svix-signature": f"v1,{sig.decode()}"},
    )
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Notification, n.id).status == NotificationStatus.BOUNCED  # type: ignore[union-attr]
    history = app_client.get("/api/notifications").json()
    assert history["items"][0]["status"] == "bounced"


def test_dev_test_email_is_admin_only_and_goes_to_own_verified_address(
    app_client: ApiClient, db: Session
) -> None:
    app_client.register()
    assert app_client.post("/api/dev/test-email").status_code == 403  # not an admin
    user = db.scalar(select(User))
    assert user is not None
    user.is_admin = True
    db.commit()
    r = app_client.post("/api/dev/test-email")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "captured" and data["purpose"] == "test"
    assert "captured by the development inbox" in data["meaning"]
    [row] = messages(db, EmailPurpose.TEST)
    assert row.recipient == user.email  # only ever the requester's own address
    assert app_client.get(f"/api/dev/test-email/{data['id']}").json()["status"] == "captured"


def test_dev_test_email_requires_verified_address(app_client: ApiClient, db: Session) -> None:
    app_client.register(verify=False)
    user = db.scalar(select(User))
    assert user is not None
    user.is_admin = True
    db.commit()
    r = app_client.post("/api/dev/test-email")
    assert r.status_code == 400 and r.json()["error"]["code"] == "email_not_verified"


def test_mailpit_provider_reports_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            assert (host, port) == ("mailpit", 1025)

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def send_message(self, msg: object) -> None:
            sent.append(msg)

    monkeypatch.setattr(prov.smtplib, "SMTP", FakeSMTP)
    settings = get_settings().model_copy(
        update={"email_provider": "mailpit", "smtp_host": "mailpit"}
    )
    result = prov.build_email_provider(settings).send(
        prov.EmailMessageData(to="a@b.co", subject="s", text="t")
    )
    assert result.captured and result.provider == "mailpit" and sent
    smtp = prov.SmtpEmailProvider(settings).send(
        prov.EmailMessageData(to="a@b.co", subject="s", text="t")
    )
    assert not smtp.captured


def test_production_refuses_capture_only_email(settings_env: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    with pytest.raises(ValueError, match="only captures mail locally"):
        Settings(
            app_env="production",
            secret_key="x" * 40,
            cookie_secure=True,
            enable_dev_endpoints=False,
            email_provider="mailpit",
        )
