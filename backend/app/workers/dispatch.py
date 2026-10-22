"""Hand work to the job runner (JOB_RUNNER).

Transactional messages (account emails, phone verification codes):

* JOB_RUNNER=daemon: sent inline, in the request, through the Celery-free senders of
  app.services.notifications.transactional (deliver_account_email, deliver_email,
  deliver_sms). A TransientProviderError is retried: :data:`INLINE_ATTEMPTS` attempts with
  :data:`INLINE_BACKOFF_SECONDS` between them; the last attempt is final
  (deliver_account_email then records FAILED with the provider's reason). An attempt
  started after :data:`INLINE_RETRY_BUDGET_SECONDS` is also final, so a slow provider does
  not get three slow attempts inside one request.
* JOB_RUNNER=celery: the Celery task, which retries in the worker. When queueing fails
  for any reason (broker refused, result backend down, Celery not installed) or does not
  finish within :data:`QUEUE_TIMEOUT_SECONDS` (a blackholed broker), the message is sent
  inline as above, so a user is never left without their verification link. Should a
  stalled publish still reach the worker later, an account email whose record is done is
  not sent again; an SMS or a plain email may arrive twice.

:func:`kick` asks the job runner to run a job soon ("match my search", "refresh flights").

Celery and kombu are imported only inside the functions and only under JOB_RUNNER=celery,
so the API (which imports this module through app.services.accounts) never loads them in
daemon mode.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from time import monotonic
from types import ModuleType
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.http import ProviderError, TransientProviderError

if TYPE_CHECKING:
    from app.models.enums import EmailDeliveryStatus
    from app.services.sync.state import KickJob

logger = logging.getLogger(__name__)

# Inline delivery: attempts, and the pause after each failed attempt but the last.
INLINE_ATTEMPTS = 3
INLINE_BACKOFF_SECONDS = (0.5, 1.5)
# An attempt that starts later than this after the first one is the final attempt.
INLINE_RETRY_BUDGET_SECONDS = 15.0
# kick() returns after at most this long, whatever the broker or Redis does.
KICK_TIMEOUT_SECONDS = 2.0
# Longest wait for Celery to accept a message (.delay()) before it is sent inline. A
# healthy broker answers in milliseconds; with Redis refusing connections .delay() raised
# only after 19 s, and with Redis blackholed it hung for more than 900 s (measured
# 2026-10-18, design D5).
QUEUE_TIMEOUT_SECONDS = 2.0

SMS_NOT_SENT = "We could not send the verification code right now. Try again in a few minutes."


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _with_retries[T](send: Callable[[bool], T], what: str) -> T:
    """``send(final)`` until it returns, at most INLINE_ATTEMPTS times.

    A TransientProviderError from a non-final attempt is retried after the next
    INLINE_BACKOFF_SECONDS pause; whatever the final attempt raises propagates.
    """
    started = monotonic()
    for attempt in range(1, INLINE_ATTEMPTS + 1):
        final = attempt == INLINE_ATTEMPTS or monotonic() - started >= INLINE_RETRY_BUDGET_SECONDS
        try:
            return send(final)
        except TransientProviderError as exc:
            if final:
                raise
            pause = INLINE_BACKOFF_SECONDS[attempt - 1]
            logger.warning(
                "%s: attempt %d of %d failed (%s); retrying in %.1f s",
                what,
                attempt,
                INLINE_ATTEMPTS,
                type(exc).__name__,
                pause,
            )
            _pause(pause)
    raise AssertionError("unreachable")  # pragma: no cover - the last attempt is final


def send_account_email_inline(
    message_id: uuid.UUID | str, text: str, html: str | None
) -> EmailDeliveryStatus | None:
    """Deliver a recorded account email now, retrying transient provider failures.

    Returns the recorded status (CAPTURED / ACCEPTED, or FAILED once the final attempt
    failed); None when the record does not exist.
    """
    from app.services.notifications.transactional import deliver_account_email

    return _with_retries(
        lambda final: deliver_account_email(message_id, text, html, final_attempt=final),
        f"account email {message_id}",
    )


def send_sms_inline(to: str, body: str) -> str | None:
    """Send an SMS now, retrying transient provider failures; returns the message id.

    Raises AppError (HTTP 503) when it could not be sent, so the user knows the code is
    not on its way. Neither the number nor the body is logged.
    """
    from app.services.notifications.transactional import deliver_sms

    try:
        return _with_retries(lambda _final: deliver_sms(to, body), "SMS")
    except ProviderError as exc:
        logger.warning("SMS not sent: %s", type(exc).__name__)
        raise AppError(SMS_NOT_SENT, code="sms_not_sent", status_code=503) from exc


def send_email_inline(to: str, subject: str, text: str, html: str | None, tag: str) -> str | None:
    """Send an email without a delivery record now, retrying transient provider failures."""
    from app.services.notifications.transactional import deliver_email

    return _with_retries(lambda _final: deliver_email(to, subject, text, html, tag), f"{tag} email")


def _bounded(call: Callable[[], object], what: str, timeout: float) -> bool:
    """Run ``call`` in a background thread and wait for it at most ``timeout`` seconds.

    True when it returned in time. False when it raised or is still running (logged); a
    call still running is left to finish on its own (daemon thread).
    """
    outcome: list[Exception | None] = []

    def target() -> None:
        try:
            call()
        except Exception as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=target, name=f"runner:{what}", daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        logger.warning("%s: no answer within %.1f s", what, timeout)
        return False
    if outcome[0] is not None:
        logger.warning("%s failed (%s)", what, type(outcome[0]).__name__)
        return False
    return True


def _queued(publish: Callable[[], object], what: str) -> bool:
    """Hand a message to Celery: True once it is queued (JOB_RUNNER=celery only).

    Eager mode (tests, single-process development) runs the task here, as before; anything
    it raises counts as not queued. Otherwise the publish gets QUEUE_TIMEOUT_SECONDS.
    """
    if get_settings().celery_task_always_eager:
        try:
            publish()
        except Exception as exc:
            logger.warning("%s failed (%s)", what, type(exc).__name__)
            return False
        return True
    return _bounded(publish, what, QUEUE_TIMEOUT_SECONDS)


def _celery_tasks() -> ModuleType | None:
    """app.workers.tasks, imported in the caller's thread (so a slow first import is not
    mistaken for a stalled broker); None when Celery cannot be imported."""
    try:
        from app.workers import tasks
    except Exception as exc:
        logger.warning("Celery unavailable (%s); sending inline", type(exc).__name__)
        return None
    return tasks


def dispatch_account_email(message_id: uuid.UUID | str, text: str, html: str | None) -> None:
    """Deliver a recorded account email (see services/notifications/transactional)."""
    if get_settings().runs_celery and (tasks := _celery_tasks()) is not None:
        task = tasks.send_account_email
        if _queued(
            lambda: task.delay(str(message_id), text, html),
            f"Queueing account email {message_id}",
        ):
            return
    send_account_email_inline(message_id, text, html)


def dispatch_email(to: str, subject: str, text: str, html: str | None, tag: str) -> None:
    if get_settings().runs_celery and (tasks := _celery_tasks()) is not None:
        task = tasks.send_email
        if _queued(lambda: task.delay(to, subject, text, html, tag), f"Queueing {tag} email"):
            return
    send_email_inline(to, subject, text, html, tag)


def dispatch_sms(to: str, body: str) -> None:
    """Send a phone verification code; AppError (503) when it cannot be sent inline."""
    if get_settings().runs_celery and (tasks := _celery_tasks()) is not None:
        task = tasks.send_sms
        if _queued(lambda: task.delay(to, body), "Queueing SMS"):
            return
    send_sms_inline(to, body)


def _kick_now(job: KickJob, match_ids: list[str]) -> None:
    """Record the request (daemon) or queue the task (Celery); raises on failure."""
    if get_settings().job_runner == "daemon":
        from app.services.sync import state

        if not state.request_kick(job, match_ids):
            raise RuntimeError("the kick flag was not written (Redis unavailable)")
        return
    from app.workers.celery_app import celery_app

    kwargs = {"match_ids": match_ids} if job == "flights.discover" and match_ids else {}
    celery_app.signature(job, kwargs=kwargs).delay()


def kick(job: KickJob, *, match_ids: Sequence[str] = ()) -> None:
    """Ask the job runner to run ``job`` soon; the periodic schedule runs it anyway.

    Never raises and returns within KICK_TIMEOUT_SECONDS (the request runs in a
    background thread that is left to finish on its own). JOB_RUNNER=celery: the task is
    queued (flights.discover with ``match_ids``); in eager mode (tests, single-process
    development) it runs here, as before. JOB_RUNNER=daemon: a Redis kick flag
    (app.services.sync.state.request_kick) that the daemon picks up within
    SYNC_DAEMON_POLL_SECONDS. A failure is logged: the job then runs on schedule.
    """
    ids = [str(m) for m in match_ids]
    settings = get_settings()
    what = f"Requesting {job} (it will run on schedule if this fails)"
    if settings.runs_celery and settings.celery_task_always_eager:
        try:
            _kick_now(job, ids)
        except Exception as exc:
            logger.warning("%s failed (%s)", what, type(exc).__name__)
        return
    _bounded(lambda: _kick_now(job, ids), what, KICK_TIMEOUT_SECONDS)
