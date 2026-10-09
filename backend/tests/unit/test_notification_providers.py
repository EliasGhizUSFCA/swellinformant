"""Email/SMS adapters, templates and alert decision rules (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.core.logging import redact
from app.models.enums import MatchStatus, NotificationChannel, NotificationKind, QualityLabel
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications import providers as prov
from app.services.notifications.alerts import decide_kind, dedup_key
from app.services.notifications.templates import alert_subject, render_alert_email, render_alert_sms


def client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


MSG = prov.EmailMessageData(
    to="kai@example.com",
    subject="Hi",
    text="Body",
    html="<p>Body</p>",
    headers={"List-Unsubscribe": "<https://x/u>"},
)


class TestEmail:
    def test_resend(self) -> None:
        seen = {}

        def handler(r: httpx.Request) -> httpx.Response:
            seen["auth"] = r.headers["Authorization"]
            seen["body"] = json.loads(r.content)
            return httpx.Response(200, json={"id": "re_123"})

        p = prov.ResendEmailProvider(
            Settings(resend_api_key="re_key"), client(handler), backoff_seconds=0
        )
        assert p.send(MSG).message_id == "re_123"
        assert seen["auth"] == "Bearer re_key"
        assert (
            seen["body"]["to"] == ["kai@example.com"]
            and seen["body"]["headers"]["List-Unsubscribe"]
        )

    def test_resend_errors(self) -> None:
        bad_key = prov.ResendEmailProvider(
            Settings(resend_api_key="x"), client(lambda r: httpx.Response(401)), backoff_seconds=0
        )
        with pytest.raises(PermanentProviderError):
            bad_key.send(MSG)
        down = prov.ResendEmailProvider(
            Settings(resend_api_key="x"), client(lambda r: httpx.Response(503)), backoff_seconds=0
        )
        with pytest.raises(TransientProviderError):
            down.send(MSG)
        with pytest.raises(PermanentProviderError):
            prov.ResendEmailProvider(Settings(resend_api_key=None)).send(MSG)

    def test_sendgrid(self) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            body = json.loads(r.content)
            assert body["personalizations"][0]["to"] == [{"email": "kai@example.com"}]
            assert {c["type"] for c in body["content"]} == {"text/plain", "text/html"}
            return httpx.Response(202, headers={"X-Message-Id": "sg_1"})

        p = prov.SendGridEmailProvider(
            Settings(sendgrid_api_key="sg", email_from="Swell <a@b.co>"),
            client(handler),
            backoff_seconds=0,
        )
        assert p.send(MSG).message_id == "sg_1"

    def test_smtp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[object] = []

        class FakeSMTP:
            def __init__(self, host: str, port: int, timeout: int) -> None:
                assert (host, port) == ("mail.local", 1025)

            def __enter__(self) -> FakeSMTP:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def send_message(self, msg: object) -> None:
                sent.append(msg)

        monkeypatch.setattr(prov.smtplib, "SMTP", FakeSMTP)
        p = prov.SmtpEmailProvider(Settings(smtp_host="mail.local", smtp_port=1025))
        result = p.send(MSG)
        assert result.message_id and sent
        assert sent[0]["List-Unsubscribe"] == "<https://x/u>"  # type: ignore[index]

    def test_smtp_connection_failure_is_transient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*a: object, **k: object) -> None:
            raise ConnectionRefusedError("no server")

        monkeypatch.setattr(prov.smtplib, "SMTP", boom)
        with pytest.raises(TransientProviderError):
            prov.SmtpEmailProvider(Settings()).send(MSG)

    def test_console_outbox(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        s = Settings(outbox_dir=str(tmp_path))
        prov.ConsoleEmailProvider(s).send(MSG)
        prov.ConsoleSmsProvider(s).send("+14155550123", "hello")
        box = prov.read_outbox(s)
        assert [m["kind"] for m in box] == ["sms", "email"]


class TestTwilio:
    def make(self, handler, **kw) -> prov.TwilioSmsProvider:  # type: ignore[no-untyped-def]
        s = Settings(
            twilio_account_sid="AC1",
            twilio_auth_token="tok",
            twilio_from_number="+15005550006",
            **kw,
        )
        return prov.TwilioSmsProvider(s, client(handler), backoff_seconds=0)

    def test_send(self) -> None:
        def handler(r: httpx.Request) -> httpx.Response:
            assert r.url.path == "/2010-04-01/Accounts/AC1/Messages.json"
            assert b"To=%2B14155550123" in r.content
            return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

        assert self.make(handler).send("+14155550123", "hi").message_id == "SM1"

    def test_opted_out_recipient(self) -> None:
        p = self.make(
            lambda r: httpx.Response(400, json={"code": 21610, "message": "unsubscribed"})
        )
        with pytest.raises(prov.RecipientOptedOut):
            p.send("+14155550123", "hi")

    def test_outage_is_transient(self) -> None:
        with pytest.raises(TransientProviderError):
            self.make(lambda r: httpx.Response(503)).send("+14155550123", "hi")

    def test_unconfigured_or_disabled_sms_is_unavailable(self) -> None:
        with pytest.raises(prov.SmsUnavailable):
            prov.TwilioSmsProvider(Settings(twilio_account_sid=None))
        with pytest.raises(prov.SmsUnavailable):
            prov.build_sms_provider(Settings(sms_provider="disabled")).send("+1", "x")


SNAP = {
    "kind": "new_opportunity",
    "match_id": "m1",
    "event_id": "e1",
    "spot_name": "Jeffreys Bay",
    "spot_slug": "jeffreys-bay",
    "country": "South Africa",
    "window_start": "2026-07-12T06:00:00+00:00",
    "window_end": "2026-07-14T15:00:00+00:00",
    "window_label": "Jul 12–14",
    "height_min_ft": 6.0,
    "height_max_ft": 9.0,
    "peak_label": "excellent",
    "peak_score": 91,
    "confidence": 60,
    "confidence_label": "moderate",
    "swell_height_m": 2.4,
    "swell_period_s": 16,
    "swell_direction": "SW",
    "wind_relation": "offshore",
    "wind_speed_kmh": 10,
    "wind_direction": "WNW",
    "is_demo_forecast": False,
    "arrival_date": "2026-07-10",
    "departure_date": "2026-07-15",
    "price": "780.00",
    "currency": "USD",
    "origin": "SFO",
    "destination": "PLZ",
    "airline": "Test Air",
    "outbound_hours": 26,
    "inbound_hours": 25,
    "is_mock_flight": False,
}
LINKS = {
    "opportunity": "https://app/o/m1",
    "forecast": "https://app/s/j",
    "unsubscribe": "https://app/u?t=1",
    "flights": "https://flights",
}


class TestTemplates:
    def test_subject_and_body(self) -> None:
        subject, text, html = render_alert_email(SNAP, LINKS)
        assert subject == "Swell Alert — Jeffreys Bay — Excellent Surf Forecast"
        for expected in (
            "Expected surf: 6–9 ft",
            "Swell: 2.4 m @ 16 s from SW",
            "Round-trip flight: $780",
            "Recommended arrival: Jul 10",
            "Recommended departure: Jul 15",
            "Departure airport: SFO",
            "Destination airport: PLZ",
            "Forecast confidence: Moderate",
            "Unsubscribe",
        ):
            assert expected in text, expected
        assert "https://app/u?t=1" in html

    def test_demo_and_mock_are_labelled(self) -> None:
        demo = {**SNAP, "is_demo_forecast": True, "is_mock_flight": True}
        subject, text, _ = render_alert_email(demo, LINKS)
        assert subject.startswith("[DEMO]") and "MOCK FARE" in text and "DEMO DATA" in text
        assert render_alert_sms(demo, "https://app/o").startswith("[DEMO]")

    def test_html_escapes_content(self) -> None:
        _, _, html = render_alert_email({**SNAP, "spot_name": "<script>x</script>"}, LINKS)
        assert "<script>x" not in html and "&lt;script&gt;" in html

    def test_sms_is_concise(self) -> None:
        sms = render_alert_sms(SNAP, "https://app/o/m1")
        assert len(sms) <= 320 and "STOP" in sms and "SFO-PLZ $780" in sms

    def test_price_drop_subject(self) -> None:
        assert "now $700" in alert_subject({**SNAP, "kind": "price_drop", "price": "700"})


def fake_match(**kw: object) -> SimpleNamespace:
    base = dict(
        last_notified_snapshot=None,
        peak_label=QualityLabel.EXCELLENT,
        peak_score=88,
        best_price=Decimal("780.00"),
        currency="USD",
        status=MatchStatus.FLIGHT_FOUND,
        recommended_arrival_date=date(2026, 7, 10),
        recommended_departure_date=date(2026, 7, 15),
    )
    base.update(kw)
    return SimpleNamespace(**base)


SEARCH = SimpleNamespace(notify_surf_only=False, notify_on_updates=True)


class TestAlertRules:
    def test_first_alert(self) -> None:
        assert decide_kind(fake_match(), SEARCH, True) == NotificationKind.NEW_OPPORTUNITY  # type: ignore[arg-type]
        assert decide_kind(fake_match(), SEARCH, False) is None  # type: ignore[arg-type]
        surf_only = SimpleNamespace(notify_surf_only=True, notify_on_updates=True)
        assert decide_kind(fake_match(), surf_only, False) == NotificationKind.SURF_ONLY  # type: ignore[arg-type]

    def test_no_repeat_for_identical_conditions(self) -> None:
        prev = {**SNAP, "peak_label": "excellent", "peak_score": 88}
        assert decide_kind(fake_match(last_notified_snapshot=prev), SEARCH, True) is None  # type: ignore[arg-type]

    def test_material_improvement(self) -> None:
        prev = {**SNAP, "peak_label": "very_good", "peak_score": 80}
        assert (
            decide_kind(fake_match(last_notified_snapshot=prev), SEARCH, True)
            == NotificationKind.IMPROVED
        )  # type: ignore[arg-type]

    def test_price_drop_thresholds(self) -> None:
        prev = {**SNAP, "peak_score": 88, "price": "900.00"}
        assert (
            decide_kind(fake_match(last_notified_snapshot=prev), SEARCH, True)
            == NotificationKind.PRICE_DROP
        )  # type: ignore[arg-type]
        small = {**SNAP, "peak_score": 88, "price": "800.00"}  # only $20 / 2.5% cheaper
        assert decide_kind(fake_match(last_notified_snapshot=small), SEARCH, True) is None  # type: ignore[arg-type]

    def test_schedule_change(self) -> None:
        prev = {**SNAP, "peak_score": 88, "arrival_date": "2026-07-07"}
        assert (
            decide_kind(fake_match(last_notified_snapshot=prev), SEARCH, True)
            == NotificationKind.SCHEDULE_CHANGE
        )  # type: ignore[arg-type]

    def test_updates_can_be_disabled(self) -> None:
        prev = {**SNAP, "peak_label": "fair", "peak_score": 45}
        quiet = SimpleNamespace(notify_surf_only=False, notify_on_updates=False)
        assert decide_kind(fake_match(last_notified_snapshot=prev), quiet, True) is None  # type: ignore[arg-type]

    def test_flights_appearing_after_surf_only_alert(self) -> None:
        prev = {**SNAP, "kind": "surf_only", "price": None}
        assert (
            decide_kind(fake_match(last_notified_snapshot=prev), SEARCH, True)
            == NotificationKind.NEW_OPPORTUNITY
        )  # type: ignore[arg-type]

    def test_dedup_key_identity(self) -> None:
        a = dedup_key("u1", NotificationChannel.EMAIL, SNAP)
        assert a == dedup_key(
            "u1", NotificationChannel.EMAIL, {**SNAP, "price": "784.00"}
        )  # same $10 bucket
        assert a != dedup_key("u1", NotificationChannel.EMAIL, {**SNAP, "price": "700.00"})
        assert a != dedup_key("u1", NotificationChannel.SMS, SNAP)
        assert a != dedup_key("u2", NotificationChannel.EMAIL, SNAP)


def test_log_redaction() -> None:
    line = "Authorization: Bearer abc.def password=hunter2 url?apikey=XYZ to kai@example.com +14155550123"
    out = redact(line)
    for secret in ("abc.def", "hunter2", "XYZ", "kai@example.com", "+14155550123"):
        assert secret not in out


def test_twilio_signature_roundtrip() -> None:
    from app.api.webhooks import twilio_signature

    sig = twilio_signature(
        "tok", "https://api.example.com/api/webhooks/twilio/sms", {"From": "+1", "Body": "STOP"}
    )
    assert sig == twilio_signature(
        "tok", "https://api.example.com/api/webhooks/twilio/sms", {"Body": "STOP", "From": "+1"}
    )
    assert sig != twilio_signature(
        "other", "https://api.example.com/api/webhooks/twilio/sms", {"From": "+1", "Body": "STOP"}
    )


def test_security_helpers() -> None:
    from app.auth.security import (
        PasswordPolicyError,
        hash_password,
        hash_token,
        validate_password,
        verify_password,
    )

    h = hash_password("Barrels4Days!")
    assert h.startswith("$argon2id$") and "Barrels4Days" not in h
    assert verify_password(h, "Barrels4Days!") and not verify_password(h, "wrong")
    assert len(hash_token("x")) == 64
    for bad in ("short1!", "password123", "aaaaaaaaaaaa", "onlyletterss"):
        with pytest.raises(PasswordPolicyError):
            validate_password(bad)
    with pytest.raises(PasswordPolicyError):
        validate_password("kaisurfer99!", email="kaisurfer@example.com")
    validate_password("Barrels4Days!")


def test_settings_guard_rails() -> None:
    with pytest.raises(ValueError, match="mix"):
        Settings(forecast_providers="open_meteo,demo")
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(
            app_env="production",
            secret_key="dev-insecure-secret-change-me",
            cookie_secure=True,
            enable_dev_endpoints=False,
        )
    with pytest.raises(ValueError, match="DEV_ENDPOINTS"):
        Settings(
            app_env="production",
            secret_key="a-very-long-random-production-key",
            cookie_secure=True,
            enable_dev_endpoints=True,
        )
    s = Settings(
        flight_provider="duffel",
        duffel_access_token=None,
        email_provider="resend",
        sms_provider="twilio",
    )
    assert {"DUFFEL_ACCESS_TOKEN", "RESEND_API_KEY", "TWILIO_ACCOUNT_SID"} <= set(
        s.missing_live_credentials()
    )
    assert datetime.now(UTC)
