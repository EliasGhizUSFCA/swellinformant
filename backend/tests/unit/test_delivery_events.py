"""Provider webhook verification and delivery-status precedence (no network)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.models.enums import NotificationStatus
from app.services.notifications import delivery_events as de

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
SECRET = "whsec_" + base64.b64encode(b"super-secret-signing-key").decode()


def svix_headers(body: bytes, ts: datetime = NOW, secret: str = SECRET) -> dict[str, str]:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    stamp = str(int(ts.timestamp()))
    sig = base64.b64encode(
        hmac.new(key, b"msg_1." + stamp.encode() + b"." + body, hashlib.sha256).digest()
    )
    return {"svix-id": "msg_1", "svix-timestamp": stamp, "svix-signature": f"v1,{sig.decode()}"}


class TestSvix:
    def test_valid_signature(self) -> None:
        body = b'{"type":"email.delivered"}'
        assert de.verify_svix_signature(SECRET, svix_headers(body), body, NOW)

    def test_tampered_body_rejected(self) -> None:
        body = b'{"type":"email.delivered"}'
        assert not de.verify_svix_signature(
            SECRET, svix_headers(body), b'{"type":"email.bounced"}', NOW
        )

    def test_wrong_secret_and_stale_timestamp_rejected(self) -> None:
        body = b"{}"
        other = "whsec_" + base64.b64encode(b"another-key").decode()
        assert not de.verify_svix_signature(other, svix_headers(body), body, NOW)
        old = svix_headers(body, ts=NOW - timedelta(minutes=10))
        assert not de.verify_svix_signature(SECRET, old, body, NOW)

    def test_missing_headers_rejected(self) -> None:
        assert not de.verify_svix_signature(SECRET, {}, b"{}", NOW)


class TestSendGrid:
    def test_ecdsa_signature_roundtrip(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        public = base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode()
        body, ts = b'[{"event":"delivered"}]', "1791720000"
        sig = base64.b64encode(key.sign(ts.encode() + body, ec.ECDSA(hashes.SHA256()))).decode()
        assert de.verify_sendgrid_signature(public, sig, ts, body)
        assert not de.verify_sendgrid_signature(public, sig, ts, body + b" ")
        assert not de.verify_sendgrid_signature(public, "not-base64!", ts, body)
        assert not de.verify_sendgrid_signature("garbage", sig, ts, body)


class TestEventParsing:
    def test_resend_mapping(self) -> None:
        events = de.resend_events(
            {
                "type": "email.bounced",
                "created_at": "2026-10-10T12:00:00Z",
                "data": {"email_id": "re_1"},
            }
        )
        assert events == [("re_1", "bounced", NOW, "email.bounced")]
        assert de.resend_events({"type": "email.opened", "data": {"email_id": "x"}}) == []

    def test_sendgrid_message_id_prefix(self) -> None:
        events = de.sendgrid_events(
            [
                {
                    "event": "delivered",
                    "sg_message_id": "abc123.filterdrecv-1",
                    "timestamp": 1791720000,
                },
                {"event": "open", "sg_message_id": "abc123.f"},
            ]
        )
        assert [(e[0], e[1]) for e in events] == [("abc123", "delivered")]


class TestPrecedence:
    def row(self, status: NotificationStatus) -> SimpleNamespace:
        return SimpleNamespace(status=status, events=[], delivered_at=None)

    def test_accepted_then_delivered_sets_timestamp(self) -> None:
        r = self.row(NotificationStatus.ACCEPTED)
        assert de._advance(r, "delivered", NOW, "delivered")
        assert r.status == NotificationStatus.DELIVERED and r.delivered_at == NOW

    def test_bounce_is_never_undone(self) -> None:
        r = self.row(NotificationStatus.BOUNCED)
        assert not de._advance(r, "delivered", NOW, "delivered")
        assert not de._advance(r, "accepted", NOW, "processed")
        assert r.status == NotificationStatus.BOUNCED and len(r.events) == 2

    def test_delayed_is_recorded_without_status_change(self) -> None:
        r = self.row(NotificationStatus.ACCEPTED)
        assert not de._advance(r, "delayed", NOW, "deferred")
        assert r.status == NotificationStatus.ACCEPTED and r.events[-1]["type"] == "deferred"
