"""Account emails (verification, password reset, developer test) with delivery records.

``queue_account_email`` writes an ``email_messages`` row (status QUEUED) and hands the
message to the job runner (app.workers.dispatch): the Celery worker under
JOB_RUNNER=celery, an inline send with bounded retries under JOB_RUNNER=daemon.
``deliver_account_email`` records what the provider actually did. Webhooks later move a row
to DELIVERED / BOUNCED / COMPLAINED. The message body is passed along but never stored,
because it contains a single-use token.

Nothing here imports Celery or kombu: the API and the sync daemon send without the worker
extra.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.database import SessionLocal, utcnow
from app.models import EmailMessage, User
from app.models.enums import EmailDeliveryStatus, EmailPurpose
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications.providers import EmailMessageData, build_email_provider

logger = logging.getLogger(__name__)

DONE = frozenset(
    {
        EmailDeliveryStatus.ACCEPTED,
        EmailDeliveryStatus.CAPTURED,
        EmailDeliveryStatus.DELIVERED,
        EmailDeliveryStatus.BOUNCED,
        EmailDeliveryStatus.COMPLAINED,
        EmailDeliveryStatus.REJECTED,
    }
)


def log_email_event(event: str, row: EmailMessage, detail: str | None = None) -> None:
    """One structured line per lifecycle step (addresses and tokens are never logged)."""
    logger.info(
        "email event=%s id=%s purpose=%s status=%s provider=%s attempts=%d%s",
        event,
        row.id,
        row.purpose.value if hasattr(row.purpose, "value") else row.purpose,
        row.status.value if hasattr(row.status, "value") else row.status,
        row.provider or "-",
        row.attempts or 0,
        f" detail={detail!r}" if detail else "",
    )


def queue_account_email(
    db: Session, user: User, purpose: EmailPurpose, subject: str, text: str, html: str | None
) -> EmailMessage:
    row = EmailMessage(
        user_id=user.id,
        purpose=purpose,
        status=EmailDeliveryStatus.QUEUED,
        recipient=user.email,
        subject=subject[:255],
        attempts=0,
        events=[],
    )
    db.add(row)
    db.commit()
    log_email_event("queued", row)
    from app.workers.dispatch import dispatch_account_email

    dispatch_account_email(row.id, text, html)
    return row


def deliver_account_email(
    message_id: uuid.UUID | str,
    text: str,
    html: str | None,
    *,
    final_attempt: bool = True,
    now: datetime | None = None,
) -> EmailDeliveryStatus | None:
    """Send one queued account email and record the outcome.

    Raises TransientProviderError when a retry is still possible (``final_attempt`` False)
    so the caller (the Celery task, or the inline retries of app.workers.dispatch) can try
    again; otherwise records FAILED and returns.
    """
    with SessionLocal() as db:
        row = db.get(EmailMessage, uuid.UUID(str(message_id)))
        if row is None:
            logger.warning("email event=missing id=%s", message_id)
            return None
        if row.status in DONE:
            return row.status  # redelivered task: never send twice
        now = now or utcnow()
        row.status = EmailDeliveryStatus.SENDING
        row.attempts = (row.attempts or 0) + 1
        db.commit()
        tag = row.purpose.value
        try:
            result = build_email_provider().send(
                EmailMessageData(
                    to=row.recipient, subject=row.subject, text=text, html=html, tag=tag
                )
            )
        except TransientProviderError as exc:
            row.last_error = str(exc)[:1000]
            row.status = EmailDeliveryStatus.FAILED if final_attempt else EmailDeliveryStatus.QUEUED
            db.commit()
            log_email_event("failed" if final_attempt else "retrying", row, row.last_error)
            if not final_attempt:
                raise
            return row.status
        except PermanentProviderError as exc:
            row.last_error = str(exc)[:1000]
            row.status = EmailDeliveryStatus.FAILED
            db.commit()
            log_email_event("failed", row, row.last_error)
            return row.status
        row.provider = result.provider
        row.provider_message_id = result.message_id
        row.status = (
            EmailDeliveryStatus.CAPTURED if result.captured else EmailDeliveryStatus.ACCEPTED
        )
        row.sent_at = now
        row.last_error = None
        db.commit()
        log_email_event(row.status.value, row)
        return row.status


def deliver_sms(to: str, body: str) -> str | None:
    """Send one SMS now (phone verification codes); returns the provider's message id.

    The body of the ``notifications.send_sms`` task and of its inline fallback (no Celery
    import).
    """
    from app.services.notifications import providers

    return providers.build_sms_provider().send(to, body).message_id


def latest_account_email(db: Session, user: User, purpose: EmailPurpose) -> EmailMessage | None:
    from sqlalchemy import select

    return db.scalar(
        select(EmailMessage)
        .where(EmailMessage.user_id == user.id, EmailMessage.purpose == purpose)
        .order_by(EmailMessage.created_at.desc())
        .limit(1)
    )
