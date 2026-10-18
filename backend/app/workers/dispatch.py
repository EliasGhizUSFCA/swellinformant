"""Hand transactional messages to the worker, falling back to inline delivery.

Account emails (verification, password reset) and phone codes are sent by Celery tasks
with automatic retries. If the broker is unreachable the message is sent inline so a
user is never left without their verification link. The inline senders
(app.services.notifications.transactional: deliver_account_email, deliver_email,
deliver_sms) import neither Celery nor kombu.

:func:`kick` asks the job runner to run a job soon ("match my search", "refresh flights").

Celery and kombu are imported inside the functions, so importing this module (the API
does, through app.services.accounts) never loads them.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.core.config import get_settings

if TYPE_CHECKING:
    from app.services.sync.state import KickJob

logger = logging.getLogger(__name__)


def dispatch_account_email(message_id: uuid.UUID | str, text: str, html: str | None) -> None:
    """Queue delivery of a recorded account email (see services/notifications/transactional)."""
    from kombu.exceptions import OperationalError

    from app.workers import tasks

    try:
        tasks.send_account_email.delay(str(message_id), text, html)
    except OperationalError:
        logger.warning("Broker unavailable; sending account email %s inline", message_id)
        from app.services.notifications.transactional import deliver_account_email

        deliver_account_email(message_id, text, html, final_attempt=True)


def dispatch_email(to: str, subject: str, text: str, html: str | None, tag: str) -> None:
    from kombu.exceptions import OperationalError

    from app.workers import tasks

    try:
        tasks.send_email.delay(to, subject, text, html, tag)
    except OperationalError:
        logger.warning("Broker unavailable; sending %s email inline", tag)
        from app.services.notifications.transactional import deliver_email

        deliver_email(to, subject, text, html, tag)


def dispatch_sms(to: str, body: str) -> None:
    from kombu.exceptions import OperationalError

    from app.workers import tasks

    try:
        tasks.send_sms.delay(to, body)
    except OperationalError:
        logger.warning("Broker unavailable; sending SMS inline")
        from app.services.notifications.transactional import deliver_sms

        deliver_sms(to, body)


def kick(job: KickJob, *, match_ids: Sequence[str] = ()) -> None:
    """Ask the job runner to run ``job`` soon; the periodic schedule runs it anyway.

    Never raises. JOB_RUNNER=celery: the task is queued (flights.discover with
    ``match_ids``); a broker error is logged. JOB_RUNNER=daemon: a Redis kick flag
    (app.services.sync.state.request_kick) that the daemon picks up within
    SYNC_DAEMON_POLL_SECONDS.
    """
    try:
        if get_settings().job_runner == "daemon":
            from app.services.sync.state import request_kick

            if not request_kick(job, match_ids):
                logger.warning("Could not record the %s request; it will run on schedule", job)
            return
        from app.workers.celery_app import celery_app

        kwargs = {"match_ids": list(match_ids)} if job == "flights.discover" and match_ids else {}
        celery_app.signature(job, kwargs=kwargs).delay()
    except Exception:  # broker or Redis down: the schedule picks the job up
        logger.warning("Could not enqueue %s; it will run on schedule", job)
