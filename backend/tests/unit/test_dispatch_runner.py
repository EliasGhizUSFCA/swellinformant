"""app.workers.dispatch: handing work to the job runner. Owned by the api-serverless
package.

Created by the foundation with :func:`dispatch.kick` (F7) and the Celery-free inline
senders; api-serverless adds the JOB_RUNNER=daemon inline delivery, the broad fallback
under Celery and the 2 s bound of its design.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from app.core.config import get_settings
from app.workers import dispatch

BACKEND = Path(__file__).resolve().parents[2]


class FakeSignature:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], name: str, kwargs: Any) -> None:
        self.calls, self.name, self.kwargs = calls, name, kwargs

    def delay(self) -> None:
        self.calls.append((self.name, self.kwargs))


def test_kick_under_celery_queues_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers.celery_app import celery_app

    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        celery_app, "signature", lambda name, kwargs=None: FakeSignature(calls, name, kwargs)
    )
    assert get_settings().job_runner == "celery"
    dispatch.kick("matching.match")
    dispatch.kick("flights.discover", match_ids=["m1"])
    dispatch.kick("flights.discover")
    assert calls == [
        ("matching.match", {}),
        ("flights.discover", {"match_ids": ["m1"]}),
        ("flights.discover", {}),
    ]


def test_kick_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers.celery_app import celery_app

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(celery_app, "signature", broken)
    dispatch.kick("matching.match")  # logged, not raised


def test_kick_under_the_daemon_writes_a_redis_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.sync import state

    calls: list[tuple[str, list[str]]] = []

    def request(job: str, match_ids: Any = ()) -> bool:
        calls.append((job, list(match_ids)))
        return False  # Redis down: logged, not raised

    monkeypatch.setattr(get_settings(), "job_runner", "daemon")
    monkeypatch.setattr(state, "request_kick", request)
    dispatch.kick("flights.discover", match_ids=("m1", "m2"))
    dispatch.kick("matching.match")
    assert calls == [("flights.discover", ["m1", "m2"]), ("matching.match", [])]


def test_the_inline_senders_need_no_celery() -> None:
    """deliver_account_email / deliver_email / deliver_sms (the inline path) import
    neither Celery nor kombu, and the Celery task module still offers the same functions."""
    code = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("celery", "kombu"):
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None

        sys.meta_path.insert(0, Blocker())
        from app.services.notifications.transactional import (
            deliver_account_email, deliver_email, deliver_sms,
        )
        import app.workers.dispatch
        print("ok")
        """
    )
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-3000:]
    from app.services.notifications import transactional
    from app.workers import tasks

    assert tasks.deliver_email is transactional.deliver_email
    assert tasks.deliver_sms is transactional.deliver_sms


def test_inline_sms_and_email_use_the_configured_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.notifications import providers, transactional

    sent: list[tuple[str, ...]] = []

    class Sms:
        def send(self, to: str, body: str) -> providers.SendResult:
            sent.append(("sms", to, body))
            return providers.SendResult(provider="fake", message_id="sms-1")

    class Email:
        def send(self, message: providers.EmailMessageData) -> providers.SendResult:
            sent.append(("email", message.to, message.subject, message.tag))
            return providers.SendResult(provider="fake", message_id="mail-1")

    monkeypatch.setattr(providers, "build_sms_provider", lambda: Sms())
    monkeypatch.setattr(providers, "build_email_provider", lambda: Email())
    assert transactional.deliver_sms("+15550100", "code 123456") == "sms-1"
    assert transactional.deliver_email("a@example.com", "Hi", "text", None, "test") == "mail-1"
    assert sent == [
        ("sms", "+15550100", "code 123456"),
        ("email", "a@example.com", "Hi", "test"),
    ]
