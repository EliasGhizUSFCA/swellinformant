"""Hand transactional messages to the worker, falling back to inline delivery.

Account emails (verification, password reset) and phone codes are sent by Celery tasks
with automatic retries. If the broker is unreachable the message is sent inline so a
user is never left without their verification link.
"""

from __future__ import annotations

import logging

from kombu.exceptions import OperationalError

logger = logging.getLogger(__name__)


def dispatch_email(to: str, subject: str, text: str, html: str | None, tag: str) -> None:
    from app.workers import tasks

    try:
        tasks.send_email.delay(to, subject, text, html, tag)
    except OperationalError:
        logger.warning("Broker unavailable; sending %s email inline", tag)
        tasks.deliver_email(to, subject, text, html, tag)


def dispatch_sms(to: str, body: str) -> None:
    from app.workers import tasks

    try:
        tasks.send_sms.delay(to, body)
    except OperationalError:
        logger.warning("Broker unavailable; sending SMS inline")
        tasks.deliver_sms(to, body)
