"""Admin alert when the flight or email provider's credentials expire (spec 16; review
r1-spec-SPEC-7).

The providers answer through httpx.MockTransport (no request leaves the test): Duffel and
Resend refuse the key with HTTP 401, then accept it again. Accounts are SYNTHETIC.
"""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator
from datetime import date
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.jobs.runner import run_job
from app.jobs.schedule import DATA_HEALTH_TASK
from app.models import Notification, NotificationPreference, User
from app.models.enums import CabinClass, NotificationStatus, SearchStatus
from app.services import service_credentials
from app.services.flights.base import FlightQuery
from app.services.flights.duffel import DuffelProvider
from app.services.health import admin_problems
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications import delivery
from app.services.notifications.ops_alerts import run_data_health_check
from app.services.notifications.providers import (
    EmailMessageData,
    MailpitEmailProvider,
    ResendEmailProvider,
    SmtpEmailProvider,
)
from tests.conftest import outbox_messages

pytestmark = pytest.mark.integration

QUERY = FlightQuery(
    origin="LHR",
    destination="LIS",
    departure_date=date(2026, 11, 20),
    return_date=date(2026, 11, 27),
    adults=1,
    cabin_class=CabinClass.ECONOMY,
    currency="EUR",
    max_connections=1,
)


def keys(db: Session) -> dict[str, str]:
    return {p.key: p.severity for p in admin_problems(db)}


def answer(status: int, body: dict[str, Any]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


def health_job() -> dict[str, Any]:
    """One ops.data_health run through the job runner (its alert state persists)."""
    return run_job(DATA_HEALTH_TASK, run_data_health_check, chain=False)


def make_admin(db: Session) -> None:
    db.add(
        User(
            email="creds-admin@example.com",
            password_hash="synthetic-hash",
            full_name="Synthetic Admin",
            is_admin=True,
            is_active=True,
            email_verified_at=utcnow(),
        )
    )
    db.commit()


def test_a_refused_flight_token_alerts_the_admins_until_a_search_succeeds(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "flight_provider", "duffel")
    monkeypatch.setattr(settings, "duffel_access_token", "duffel_test_synthetic")
    make_admin(db)
    own = Settings(duffel_access_token="duffel_test_synthetic", flight_provider="duffel")
    refused = DuffelProvider(
        lambda code: "UTC",  # type: ignore[arg-type,return-value]
        own,
        client=answer(401, {"errors": [{"message": "The access token is not valid"}]}),
        backoff_seconds=0,
    )
    with pytest.raises(PermanentProviderError, match="authentication failed"):
        refused.search(QUERY)
    assert keys(db).get("provider_credentials:flights_duffel") == "failing"
    report = health_job()
    assert "new:provider_credentials:flights_duffel" in report["alerts"]
    sent = [m for m in outbox_messages() if m.get("tag") == "ops_alert"]
    assert sent and {m["to"] for m in sent} == {"creds-admin@example.com"}
    assert "DUFFEL_ACCESS_TOKEN" in str(sent[-1]["text"])
    # The token is renewed: the next search is accepted and the problem clears.
    accepted = DuffelProvider(
        lambda code: "UTC",  # type: ignore[arg-type,return-value]
        own,
        client=answer(200, {"data": {"offers": []}}),
        backoff_seconds=0,
    )
    assert accepted.search(QUERY) == []
    assert "provider_credentials:flights_duffel" not in keys(db)
    recovered = health_job()
    assert "recovered:provider_credentials:flights_duffel" in recovered["alerts"]


def test_a_refused_email_key_is_a_problem_until_a_message_is_accepted(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "email_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_synthetic")
    message = EmailMessageData(
        to="someone@example.com", subject="Synthetic", text="Synthetic body", tag="test"
    )
    with pytest.raises(PermanentProviderError, match="authentication failed"):
        ResendEmailProvider(settings, client=answer(401, {}), backoff_seconds=0).send(message)
    problems = admin_problems(db)
    (problem,) = [p for p in problems if p.key == "provider_credentials:email_resend"]
    assert problem.severity == "failing" and "RESEND_API_KEY" in problem.message
    ResendEmailProvider(settings, client=answer(200, {"id": "re_1"}), backoff_seconds=0).send(
        message
    )
    assert "provider_credentials:email_resend" not in keys(db)


def test_another_providers_record_is_not_reported(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the configured providers count: a record left by a provider the deployment no
    longer uses (FLIGHT_PROVIDER changed) is no problem."""
    service_credentials.record_refused("flights", "amadeus", "HTTP 401")
    monkeypatch.setattr(get_settings(), "flight_provider", "duffel")
    assert not any(k.startswith("provider_credentials:flights_") for k in keys(db))


class FakeSmtp(socketserver.StreamRequestHandler):
    """A SYNTHETIC SMTP server on localhost: refuses every AUTH with 535 while ``refuse``
    is set (an expired or revoked password), or with 454 while ``temporary`` is also set
    (RFC 4954 section 6: its authentication backend is briefly down), else accepts it and
    the message. ``refuse_at`` = (verb, reply) answers that one command (MAIL, RCPT or DATA)
    with the given reply instead."""

    refuse: ClassVar[bool] = True
    temporary: ClassVar[bool] = False
    refuse_at: ClassVar[tuple[str, str] | None] = None
    messages: ClassVar[int] = 0

    def reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())

    def handle(self) -> None:
        self.reply("220 synthetic ESMTP")
        while raw := self.rfile.readline():
            verb = raw.decode(errors="replace").strip().split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.reply("250-synthetic")
                self.reply("250 AUTH PLAIN LOGIN")
            elif verb == "AUTH":
                if not self.refuse:
                    self.reply("235 2.7.0 Authentication successful")
                elif self.temporary:
                    self.reply("454 4.7.0 Temporary authentication failure")
                else:
                    self.reply("535 5.7.8 Authentication credentials invalid")
            elif self.refuse_at is not None and verb == self.refuse_at[0]:
                self.reply(self.refuse_at[1])
            elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                self.reply("250 OK")
            elif verb == "DATA":
                self.reply("354 End data with <CR><LF>.<CR><LF>")
                while (line := self.rfile.readline()) and line not in (b".\r\n", b".\n"):
                    pass
                FakeSmtp.messages += 1
                self.reply("250 OK queued")
            elif verb == "QUIT":
                self.reply("221 Bye")
                return
            else:
                self.reply("502 Command not implemented")


@pytest.fixture
def smtp_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    FakeSmtp.refuse, FakeSmtp.temporary, FakeSmtp.messages = True, False, 0
    FakeSmtp.refuse_at = None
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSmtp)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = get_settings()
    for name, value in {
        "email_provider": "smtp",
        "smtp_host": "127.0.0.1",
        "smtp_port": server.server_address[1],
        "smtp_use_tls": False,
        "smtp_use_ssl": False,
        "smtp_username": "synthetic-user",
        "smtp_password": "synthetic-password",
    }.items():
        monkeypatch.setattr(settings, name, value)
    try:
        yield settings
    finally:
        server.shutdown()
        server.server_close()


def test_a_refused_smtp_password_is_a_problem_until_a_message_is_accepted(
    db: Session, smtp_server: Settings
) -> None:
    """Review r2-backend-N3: EMAIL_PROVIDER=smtp with an expired or revoked password (the
    server answers 535) raises provider_credentials:email_smtp; the next accepted message
    clears it."""
    message = EmailMessageData(
        to="someone@example.com", subject="Synthetic", text="Synthetic body", tag="test"
    )
    with pytest.raises(PermanentProviderError, match="SMTP rejected"):
        SmtpEmailProvider(smtp_server).send(message)
    (problem,) = [p for p in admin_problems(db) if p.key == "provider_credentials:email_smtp"]
    assert problem.severity == "failing" and "SMTP_USERNAME / SMTP_PASSWORD" in problem.message
    assert problem.detail is not None and "535" in problem.detail
    FakeSmtp.refuse = False
    SmtpEmailProvider(smtp_server).send(message)
    assert FakeSmtp.messages == 1
    assert "provider_credentials:email_smtp" not in keys(db)


def test_a_temporary_smtp_auth_failure_is_retried_and_no_credentials_problem(
    db: Session, smtp_server: Settings
) -> None:
    """Review r3-backend-R3-3: smtplib raises SMTPAuthenticationError for any refused AUTH,
    including 454 (a temporary server-side failure, not a wrong password). That is a
    temporary error (the message is retried, spec 21) and no refused credentials."""
    FakeSmtp.temporary = True
    message = EmailMessageData(to="someone@example.com", subject="S", text="T", tag="test")
    with pytest.raises(TransientProviderError, match="temporarily unavailable"):
        SmtpEmailProvider(smtp_server).send(message)
    assert "provider_credentials:email_smtp" not in keys(db)
    assert service_credentials.refused(["email_smtp"]) == []


# RFC 5321 section 4.2.1: a 4yz reply is a transient failure, a 5yz reply a permanent one.
# The replies are SYNTHETIC, worded as relays word them.
REFUSALS = [
    ("MAIL", "454 4.7.0 Throttling failure: Maximum sending rate exceeded", TransientProviderError),
    ("MAIL", "421 4.7.0 Try again later, closing connection", TransientProviderError),
    ("MAIL", "553 5.1.8 Sender address rejected", PermanentProviderError),
    ("RCPT", "450 4.2.1 Receiving mail at a rate that prevents more", TransientProviderError),
    ("RCPT", "451 4.7.1 Greylisted, please try again later", TransientProviderError),
    ("RCPT", "550 5.1.1 No such user", PermanentProviderError),
    ("DATA", "451 4.3.0 Local error in processing", TransientProviderError),
    ("DATA", "554 5.7.1 Message rejected", PermanentProviderError),
]


@pytest.mark.parametrize(("verb", "reply", "error"), REFUSALS)
def test_a_refused_smtp_command_is_temporary_or_permanent_by_its_reply_code(
    db: Session, smtp_server: Settings, verb: str, reply: str, error: type[Exception]
) -> None:
    """Review r4-backend-R4-1: smtplib raises SMTPSenderRefused, SMTPRecipientsRefused or
    SMTPDataError whatever the reply code. A relay's sending-rate limit or greylisting (4yz
    at MAIL FROM or RCPT TO) is retried; a 5yz at DATA is not. Neither is a refused password."""
    FakeSmtp.refuse = False
    FakeSmtp.refuse_at = (verb, reply)
    message = EmailMessageData(to="someone@example.com", subject="S", text="T", tag="test")
    with pytest.raises(error) as raised:
        SmtpEmailProvider(smtp_server).send(message)
    assert type(raised.value) is error and reply[:3] in str(raised.value)
    assert FakeSmtp.messages == 0
    assert service_credentials.refused(["email_smtp"]) == []


def test_an_alert_the_relay_throttles_is_queued_for_a_retry(
    db: Session, smtp_server: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r4-backend-R4-1, through delivery: an alert email whose RCPT TO the relay
    answers 451 stays QUEUED with its backoff (not FAILED); a 554 at DATA fails it at once.
    The match's search lookup is stubbed ACTIVE: no match is needed to reach the provider."""
    make_admin(db)
    user = db.query(User).filter_by(email="creds-admin@example.com").one()
    db.add(NotificationPreference(user_id=user.id, unsubscribe_token="synthetic-unsub-smtp"))
    n = Notification(
        user_id=user.id,
        channel="email",
        kind="new_opportunity",
        status=NotificationStatus.SENDING,
        dedup_key="synthetic-smtp-throttle",
        recipient=user.email,
        subject="Synthetic alert",
        body_text="Synthetic alert body",
        snapshot={},
        events=[],
        attempts=0,
    )
    db.add(n)
    db.commit()
    monkeypatch.setattr(delivery, "_search_status", lambda db, match_id: SearchStatus.ACTIVE)
    FakeSmtp.refuse = False
    FakeSmtp.refuse_at = ("RCPT", "451 4.7.1 Greylisted, please try again later")
    now = utcnow()
    stats = delivery.DeliveryStats()
    delivery.send_one(db, n, delivery._Providers(), now, stats)
    assert n.status == NotificationStatus.QUEUED and stats.retried == 1 and stats.failed == 0
    assert n.next_attempt_at == now + delivery.backoff(1)
    assert "451" in (n.last_error or "")
    FakeSmtp.refuse_at = ("DATA", "554 5.7.1 Message rejected")
    delivery.send_one(db, n, delivery._Providers(), now, stats)
    assert n.status == NotificationStatus.FAILED and stats.failed == 1 and n.attempts == 2
    assert FakeSmtp.messages == 0


def test_a_development_inbox_records_no_credentials(db: Session, smtp_server: Settings) -> None:
    """Mailpit captures messages locally: its answers say nothing about a real provider's
    credentials, so they are never recorded."""
    with pytest.raises(PermanentProviderError):
        MailpitEmailProvider(smtp_server).send(
            EmailMessageData(to="someone@example.com", subject="S", text="T", tag="test")
        )
    assert service_credentials.refused(["email_mailpit"]) == []


def test_a_refused_email_key_reaches_the_admins_by_sms(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r2-spec-SPEC-6: the alert about a refused email key cannot be emailed through
    that same provider. The admins with a verified, opted-in phone number get one SMS a day
    (SMS_PROVIDER, the console outbox here); the email is still retried at every check."""
    from app.models import NotificationPreference
    from app.services.notifications import ops_alerts

    settings = get_settings()
    monkeypatch.setattr(settings, "email_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_synthetic")
    monkeypatch.setattr(settings, "sms_provider", "console")
    make_admin(db)
    admin = db.query(User).filter_by(email="creds-admin@example.com").one()
    now = utcnow()
    db.add(
        NotificationPreference(
            user_id=admin.id,
            phone_number="+15555550123",  # SYNTHETIC (a fictional 555 number)
            phone_verified_at=now,
            sms_opt_in_at=now,
            unsubscribe_token="synthetic-unsubscribe-token-sms",
        )
    )
    db.commit()
    refusing = ResendEmailProvider(settings, client=answer(401, {}), backoff_seconds=0)
    monkeypatch.setattr(ops_alerts, "build_email_provider", lambda *a, **k: refusing)
    message = EmailMessageData(to="someone@example.com", subject="S", text="T", tag="test")
    with pytest.raises(PermanentProviderError):
        refusing.send(message)
    report = health_job()
    assert "new:provider_credentials:email_resend" in report["alerts"]
    assert report["email"]["sent"] == 0 and report["email"]["sms"]["sent"] == 1
    sms = [m for m in outbox_messages() if m.get("kind") == "sms"]
    assert [m["to"] for m in sms] == ["+15555550123"]
    assert "refused its credentials" in str(sms[0]["text"])
    # The email is still due (nothing accepted it); the SMS is not sent twice a day.
    again = health_job()
    assert "unsent:provider_credentials:email_resend" in again["alerts"]
    assert again["email"]["sms"]["skipped"] == "already sent today"
    assert len([m for m in outbox_messages() if m.get("kind") == "sms"]) == 1
