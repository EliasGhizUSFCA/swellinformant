"""Provider delivery events (webhooks) → delivery status of alerts and account emails.

Only these events can mark a message DELIVERED. Statuses move forward only, so a
duplicate or out-of-order webhook can never turn a bounce back into "delivered".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmailMessage, Notification
from app.models.enums import EmailDeliveryStatus, NotificationStatus

logger = logging.getLogger(__name__)

# Normalised event → (rank, status value). Higher rank wins; equal rank keeps the first.
EVENT_STATUS: dict[str, tuple[int, str] | None] = {
    "accepted": (1, "accepted"),
    "delivered": (2, "delivered"),
    "bounced": (3, "bounced"),
    "rejected": (3, "rejected"),
    "complained": (4, "complained"),
    "delayed": None,  # recorded, status unchanged
}
STATUS_RANK = {
    "queued": 0,
    "sending": 0,
    "captured": 1,
    "accepted": 1,
    "delivered": 2,
    "bounced": 3,
    "rejected": 3,
    "failed": 3,
    "complained": 4,
}

RESEND_EVENTS = {
    "email.sent": "accepted",
    "email.delivered": "delivered",
    "email.delivery_delayed": "delayed",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.failed": "rejected",
}
SENDGRID_EVENTS = {
    "processed": "accepted",
    "delivered": "delivered",
    "deferred": "delayed",
    "bounce": "bounced",
    "blocked": "bounced",
    "dropped": "rejected",
    "spamreport": "complained",
}


def verify_svix_signature(
    secret: str, headers: dict[str, str], body: bytes, now: datetime, tolerance_s: int = 300
) -> bool:
    """Resend webhooks are signed with Svix: base64(HMAC-SHA256(key, f"{id}.{ts}.{body}"))."""
    msg_id, timestamp, signatures = (
        headers.get("svix-id"),
        headers.get("svix-timestamp"),
        headers.get("svix-signature"),
    )
    if not (msg_id and timestamp and signatures):
        return False
    try:
        if abs(now.timestamp() - int(timestamp)) > tolerance_s:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        hmac.compare_digest(expected, part.split(",", 1)[1])
        for part in signatures.split()
        if part.startswith("v1,") and "," in part
    )


def verify_sendgrid_signature(
    public_key_b64: str, signature_b64: str, timestamp: str, body: bytes
) -> bool:
    """SendGrid Event Webhook: ECDSA P-256 / SHA-256 over ``timestamp + body``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        key = serialization.load_der_public_key(base64.b64decode(public_key_b64))
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        key.verify(
            base64.b64decode(signature_b64), timestamp.encode() + body, ec.ECDSA(hashes.SHA256())
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _advance(row: Any, event: str, at: datetime, raw_type: str) -> bool:
    mapping = EVENT_STATUS.get(event)
    row.events = [*(row.events or []), {"type": raw_type, "event": event, "at": at.isoformat()}][
        -20:
    ]
    if mapping is None:
        return False
    rank, status = mapping
    current = row.status.value if hasattr(row.status, "value") else str(row.status)
    if rank <= STATUS_RANK.get(current, 0):
        return False
    row.status = (
        EmailDeliveryStatus(status) if isinstance(row, EmailMessage) else NotificationStatus(status)
    )
    if status == "delivered":
        row.delivered_at = at
    return True


def apply_provider_event(
    db: Session, provider: str, message_id: str, event: str, at: datetime, raw_type: str
) -> int:
    """Apply one normalised event to every message with this provider id. Returns matches."""
    matched = 0
    rows: list[Notification | EmailMessage] = [
        *db.scalars(
            select(Notification).where(
                Notification.provider == provider, Notification.provider_message_id == message_id
            )
        ).all(),
        *db.scalars(
            select(EmailMessage).where(
                EmailMessage.provider == provider, EmailMessage.provider_message_id == message_id
            )
        ).all(),
    ]
    for row in rows:
        matched += 1
        changed = _advance(row, event, at, raw_type)
        logger.info(
            "email event=webhook provider=%s type=%s id=%s status=%s changed=%s",
            provider,
            raw_type,
            row.id,
            row.status.value,
            changed,
        )
    return matched


def resend_events(payload: dict[str, Any]) -> list[tuple[str, str, datetime, str]]:
    raw_type = str(payload.get("type", ""))
    event = RESEND_EVENTS.get(raw_type)
    data = payload.get("data") or {}
    message_id = data.get("email_id")
    if not event or not message_id:
        return []
    at = _parse_time(payload.get("created_at"))
    return [(str(message_id), event, at, raw_type)]


def sendgrid_events(items: list[dict[str, Any]]) -> list[tuple[str, str, datetime, str]]:
    out = []
    for item in items:
        raw_type = str(item.get("event", ""))
        event = SENDGRID_EVENTS.get(raw_type)
        sg_id = str(item.get("sg_message_id") or "")
        if not event or not sg_id:
            continue
        # sg_message_id = "<X-Message-Id from the send response>.<filter suffix>"
        message_id = sg_id.split(".", 1)[0]
        ts = item.get("timestamp")
        at = (
            datetime.fromtimestamp(int(ts), UTC)
            if isinstance(ts, int | float)
            else datetime.now(UTC)
        )
        out.append((message_id, event, at, raw_type))
    return out


def _parse_time(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
