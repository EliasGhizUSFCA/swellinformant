"""Job G — deliver queued notifications with retries.

* Claiming uses ``SELECT … FOR UPDATE SKIP LOCKED`` so concurrent workers never send the
  same notification twice.
* A notification stuck in SENDING (worker crashed mid-send) is re-queued after
  NOTIFICATION_SENDING_TIMEOUT_MINUTES. Providers are idempotent enough for this rare
  case; a duplicate is preferable to a silently lost alert.
* Preferences are re-checked at send time: opting out after an alert was queued is
  honoured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import Notification, NotificationPreference, User
from app.models.enums import NotificationChannel, NotificationStatus
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications.providers import (
    EmailMessageData,
    EmailProvider,
    RecipientOptedOut,
    SmsProvider,
    SmsUnavailable,
    build_email_provider,
    build_sms_provider,
)

logger = logging.getLogger(__name__)


@dataclass
class DeliveryStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0
    reclaimed: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(60, 2 ** max(0, attempts - 1)))


def reclaim_stuck(db: Session, now: datetime) -> int:
    timeout = timedelta(minutes=get_settings().notification_sending_timeout_minutes)
    result = db.execute(
        update(Notification)
        .where(
            Notification.status == NotificationStatus.SENDING,
            Notification.updated_at < now - timeout,
        )
        .values(
            status=NotificationStatus.QUEUED,
            next_attempt_at=now,
            updated_at=now,
            last_error="re-queued after worker interruption",
        )
    )
    db.commit()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def claim(db: Session, now: datetime, limit: int) -> list[Notification]:
    rows = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.QUEUED,
                Notification.next_attempt_at <= now,
            )
            .order_by(Notification.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True, of=Notification)
        ).all()
    )
    for n in rows:
        n.status = NotificationStatus.SENDING
        n.updated_at = now
    db.commit()
    return rows


class _Providers:
    def __init__(self) -> None:
        self._email: EmailProvider | None = None
        self._sms: SmsProvider | None = None
        self.sms_error: str | None = None

    def email(self) -> EmailProvider:
        if self._email is None:
            self._email = build_email_provider()
        return self._email

    def sms(self) -> SmsProvider:
        if self._sms is None:
            self._sms = build_sms_provider()
        return self._sms


def _skip(n: Notification, reason: str, stats: DeliveryStats) -> None:
    n.status = NotificationStatus.SKIPPED
    n.last_error = reason
    stats.skipped += 1


def send_one(
    db: Session, n: Notification, providers: _Providers, now: datetime, stats: DeliveryStats
) -> None:
    settings = get_settings()
    user = db.get(User, n.user_id)
    prefs = db.get(NotificationPreference, n.user_id)
    if user is None or prefs is None or not user.is_active:
        return _skip(n, "account inactive or deleted", stats)
    if prefs.all_paused:
        return _skip(n, "notifications paused by user", stats)
    n.attempts += 1
    try:
        if n.channel == NotificationChannel.EMAIL:
            if not prefs.email_enabled or not user.email_verified_at:
                return _skip(n, "email alerts disabled or email not verified", stats)
            n.recipient = user.email
            headers = {
                "List-Unsubscribe": (
                    f"<{settings.api_base_url}/api/notifications/unsubscribe"
                    f"?token={prefs.unsubscribe_token}>"
                ),
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }
            provider = providers.email()
            result = provider.send(
                EmailMessageData(
                    to=user.email,
                    subject=n.subject,
                    text=n.body_text,
                    html=n.body_html,
                    headers=headers,
                )
            )
        else:
            if not prefs.sms_enabled or not prefs.sms_ready:
                return _skip(n, "SMS not enabled, not verified, or opted out", stats)
            n.recipient = prefs.phone_number or ""
            sms = providers.sms()
            result = sms.send(n.recipient, n.body_text)
    except RecipientOptedOut as exc:
        prefs.sms_opted_out_at = now
        n.status = NotificationStatus.FAILED
        n.last_error = str(exc)[:1000]
        stats.failed += 1
        return
    except SmsUnavailable as exc:
        n.status = NotificationStatus.FAILED
        n.last_error = f"SMS service unavailable: {exc}"[:1000]
        stats.failed += 1
        logger.error("SMS delivery unavailable for notification %s: %s", n.id, exc)
        return
    except TransientProviderError as exc:
        n.last_error = str(exc)[:1000]
        if n.attempts >= settings.notification_max_attempts:
            n.status = NotificationStatus.FAILED
            stats.failed += 1
            logger.error("Notification %s failed after %d attempts: %s", n.id, n.attempts, exc)
        else:
            n.status = NotificationStatus.QUEUED
            n.next_attempt_at = now + backoff(n.attempts)
            stats.retried += 1
            logger.warning("Notification %s will retry: %s", n.id, exc)
        return
    except PermanentProviderError as exc:
        n.status = NotificationStatus.FAILED
        n.last_error = str(exc)[:1000]
        stats.failed += 1
        logger.error("Notification %s failed permanently: %s", n.id, exc)
        return
    n.status = NotificationStatus.SENT
    n.sent_at = now
    n.provider = result.provider
    n.provider_message_id = result.message_id
    n.last_error = None
    stats.sent += 1


def deliver_pending(db: Session, now: datetime | None = None, limit: int = 100) -> DeliveryStats:
    now = now or utcnow()
    stats = DeliveryStats(reclaimed=reclaim_stuck(db, now))
    providers = _Providers()
    batch = claim(db, now, limit)
    stats.claimed = len(batch)
    for n in batch:
        try:
            send_one(db, n, providers, now, stats)
        except Exception as exc:  # unexpected bug: never leave it stuck in SENDING
            logger.exception("Unexpected delivery error for notification %s", n.id)
            n.last_error = f"unexpected error: {exc}"[:1000]
            if n.attempts >= get_settings().notification_max_attempts:
                n.status = NotificationStatus.FAILED
                stats.failed += 1
            else:
                n.status = NotificationStatus.QUEUED
                n.next_attempt_at = now + backoff(n.attempts)
        n.updated_at = now
        db.commit()
    if stats.claimed:
        logger.info("Notification delivery: %s", stats.as_dict())
    return stats
