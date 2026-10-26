"""app.workers.dispatch: handing work to the job runner. Owned by the api-serverless
package.

* JOB_RUNNER=daemon: account emails and SMS go out inline (3 attempts, 0.5 s and 1.5 s
  pauses after a TransientProviderError, the last attempt final) without importing Celery;
  kicks are Redis flags.
* JOB_RUNNER=celery: any exception from ``.delay()`` falls back to the inline delivery.
* :func:`dispatch.kick` (F7) never raises and returns within 2 s.

Provider failures and slow brokers here are synthetic test doubles.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import redis

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.redis import get_redis, reset_redis
from app.models.enums import EmailDeliveryStatus
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.notifications import transactional
from app.workers import dispatch

BACKEND = Path(__file__).resolve().parents[2]


def closed_port() -> int:
    """A local TCP port nothing listens on (connections are refused at once)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The retry pauses dispatch would sleep (recorded, not slept)."""
    seen: list[float] = []
    monkeypatch.setattr(dispatch, "_pause", seen.append)
    return seen


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "job_runner", "daemon")


class FlakyAccountEmail:
    """Stands in for deliver_account_email: ``failures`` transient errors, then the
    recorded outcome; the final attempt records FAILED like the real function."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[bool] = []

    def __call__(
        self, message_id: Any, text: str, html: str | None, *, final_attempt: bool = True
    ) -> EmailDeliveryStatus:
        self.calls.append(final_attempt)
        if len(self.calls) <= self.failures:
            if final_attempt:
                return EmailDeliveryStatus.FAILED
            raise TransientProviderError("synthetic: provider unavailable")
        return EmailDeliveryStatus.ACCEPTED


# --------------------------------------------------------------------------- daemon: inline
def test_daemon_account_email_retries_transient_failures_inline(
    daemon: None, pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FlakyAccountEmail(failures=2)
    monkeypatch.setattr(transactional, "deliver_account_email", fake)
    assert dispatch.send_account_email_inline("m1", "text", None) == EmailDeliveryStatus.ACCEPTED
    dispatch.dispatch_account_email("m1", "text", None)  # the same path, through dispatch
    # Three attempts, the last one final; then the dispatch call succeeds at once.
    assert fake.calls == [False, False, True, False]
    assert pauses == [0.5, 1.5]


def test_daemon_account_email_records_failed_after_three_attempts(
    daemon: None, pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FlakyAccountEmail(failures=5)
    monkeypatch.setattr(transactional, "deliver_account_email", fake)
    assert dispatch.send_account_email_inline("m1", "text", None) == EmailDeliveryStatus.FAILED
    assert fake.calls == [False, False, True] and pauses == [0.5, 1.5]


def test_an_attempt_after_the_retry_budget_is_final(
    daemon: None, pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow provider: the second attempt starts after the budget, so it is the last."""
    clock = iter([0.0, 0.0, dispatch.INLINE_RETRY_BUDGET_SECONDS + 1])  # start, 1st, 2nd
    monkeypatch.setattr(dispatch, "monotonic", lambda: next(clock))
    fake = FlakyAccountEmail(failures=5)
    monkeypatch.setattr(transactional, "deliver_account_email", fake)
    assert dispatch.send_account_email_inline("m1", "text", None) == EmailDeliveryStatus.FAILED
    assert fake.calls == [False, True] and pauses == [0.5]


def test_daemon_sms_is_sent_inline_or_reported(
    daemon: None, pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    outcomes: list[Exception | None] = [TransientProviderError("synthetic"), None]

    def deliver(to: str, body: str) -> str:
        calls.append(to)
        error = outcomes.pop(0) if outcomes else TransientProviderError("synthetic")
        if error is not None:
            raise error
        return "sms-1"

    monkeypatch.setattr(transactional, "deliver_sms", deliver)
    dispatch.dispatch_sms("+15550100", "code")  # one transient failure, then sent
    assert calls == ["+15550100"] * 2 and pauses == [0.5]
    with pytest.raises(AppError) as failed:  # three transient failures: the user is told
        dispatch.dispatch_sms("+15550100", "code")
    assert failed.value.status_code == 503 and failed.value.code == "sms_not_sent"
    assert len(calls) == 5 and pauses == [0.5, 0.5, 1.5]

    def permanent(to: str, body: str) -> str:
        calls.append(to)
        raise PermanentProviderError("synthetic: SMS disabled")

    monkeypatch.setattr(transactional, "deliver_sms", permanent)
    with pytest.raises(AppError):
        dispatch.dispatch_sms("+15550100", "code")
    assert len(calls) == 6  # a permanent failure is not retried


def test_daemon_dispatch_never_imports_celery(tmp_path: Path) -> None:
    """Under JOB_RUNNER=daemon, account email, email and SMS dispatch run with Celery and
    kombu impossible to import, and leave them out of sys.modules."""
    code = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("celery", "kombu"):
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None

        sys.meta_path.insert(0, Blocker())
        from app.services.notifications import transactional
        from app.workers import dispatch

        sent = []
        transactional.deliver_account_email = (
            lambda *a, final_attempt=True: sent.append(("account", final_attempt)) or "ok"
        )
        transactional.deliver_sms = lambda *a: sent.append(("sms",)) or "s1"
        dispatch.dispatch_account_email("m1", "text", None)
        dispatch.dispatch_sms("+15550100", "code")
        assert sent == [("account", False), ("sms",)], sent
        assert not {"celery", "kombu"} & set(sys.modules), sorted(sys.modules)
        print("ok")
        """
    )
    env = {"PATH": "/usr/bin:/bin", "JOB_RUNNER": "daemon", "HOME": str(tmp_path)}
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-3000:]


# --------------------------------------------------------------------------- celery fallback
@pytest.mark.parametrize(
    "error",
    [RuntimeError("Retry limit exceeded while trying to reconnect"), OSError("refused")],
)
def test_celery_queueing_failure_falls_back_inline(
    error: Exception, pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception from .delay() (the result backend's RuntimeError after Redis refused
    connections was measured at 19 s) means the message is sent inline at once."""
    from app.workers import tasks

    def broken(*_: object, **__: object) -> None:
        raise error

    assert get_settings().job_runner == "celery"
    for task in (tasks.send_account_email, tasks.send_sms):
        monkeypatch.setattr(task, "delay", broken)
    fake = FlakyAccountEmail(failures=1)
    sent: list[str] = []
    monkeypatch.setattr(transactional, "deliver_account_email", fake)
    monkeypatch.setattr(transactional, "deliver_sms", lambda *a: sent.append("sms"))
    dispatch.dispatch_account_email("m1", "text", None)
    dispatch.dispatch_sms("+15550100", "code")
    assert fake.calls == [False, False] and pauses == [0.5]
    assert sent == ["sms"]


def test_a_stalled_celery_publish_falls_back_inline(
    pauses: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blackholed broker: .delay() does not return (it hung for more than 900 s when
    measured). After QUEUE_TIMEOUT_SECONDS the message is sent inline."""
    from app.workers import tasks

    def hanging(*_: object, **__: object) -> None:
        time.sleep(5)

    monkeypatch.setattr(get_settings(), "celery_task_always_eager", False)
    for task in (tasks.send_account_email, tasks.send_sms):
        monkeypatch.setattr(task, "delay", hanging)
    fake = FlakyAccountEmail(failures=0)
    sent: list[str] = []
    monkeypatch.setattr(transactional, "deliver_account_email", fake)
    monkeypatch.setattr(transactional, "deliver_sms", lambda *a: sent.append("sms"))
    for dispatch_one in (
        lambda: dispatch.dispatch_account_email("m1", "text", None),
        lambda: dispatch.dispatch_sms("+15550100", "code"),
    ):
        started = time.monotonic()
        dispatch_one()
        elapsed = time.monotonic() - started
        assert dispatch.QUEUE_TIMEOUT_SECONDS - 0.1 <= elapsed < dispatch.QUEUE_TIMEOUT_SECONDS + 1
    assert fake.calls == [False] and sent == ["sms"] and pauses == []


def test_a_quick_celery_publish_is_not_sent_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import tasks

    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(get_settings(), "celery_task_always_eager", False)
    monkeypatch.setattr(tasks.send_sms, "delay", lambda *a: queued.append(a))
    monkeypatch.setattr(transactional, "deliver_sms", lambda *a: pytest.fail("sent inline"))
    started = time.monotonic()
    dispatch.dispatch_sms("+15550100", "code")
    assert queued == [("+15550100", "code")] and time.monotonic() - started < 1


def test_celery_runner_without_celery_installed_sends_inline(tmp_path: Path) -> None:
    """JOB_RUNNER=celery on an install without the worker extra: the messages still go
    out (inline) instead of failing the request."""
    code = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("celery", "kombu"):
                    raise ModuleNotFoundError(f"No module named {name!r}", name=name)
                return None

        sys.meta_path.insert(0, Blocker())
        from app.services.notifications import transactional
        from app.workers import dispatch

        sent = []
        transactional.deliver_account_email = (
            lambda *a, final_attempt=True: sent.append(("account", final_attempt)) or "ok"
        )
        transactional.deliver_sms = lambda *a: sent.append(("sms",)) or "s1"
        dispatch.dispatch_account_email("m1", "text", None)
        dispatch.dispatch_sms("+15550100", "code")
        dispatch.kick("matching.match")  # logged, never raised
        assert sent == [("account", False), ("sms",)], sent
        print("ok")
        """
    )
    env = {"PATH": "/usr/bin:/bin", "JOB_RUNNER": "celery", "HOME": str(tmp_path)}
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-3000:]


def test_celery_queueing_success_sends_nothing_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import tasks

    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks.send_account_email, "delay", lambda *a: queued.append(a))
    monkeypatch.setattr(transactional, "deliver_account_email", FlakyAccountEmail(9))
    dispatch.dispatch_account_email("m1", "text", "<p>html</p>")
    assert queued == [("m1", "text", "<p>html</p>")]


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
    """deliver_account_email / deliver_sms (the inline path) import neither Celery nor
    kombu, and the Celery send_sms task sends through the same function."""
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
            deliver_account_email, deliver_sms,
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

    assert tasks.deliver_sms is transactional.deliver_sms


def test_inline_sms_uses_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.notifications import providers, transactional

    sent: list[tuple[str, ...]] = []

    class Sms:
        def send(self, to: str, body: str) -> providers.SendResult:
            sent.append(("sms", to, body))
            return providers.SendResult(provider="fake", message_id="sms-1")

    monkeypatch.setattr(providers, "build_sms_provider", lambda: Sms())
    assert transactional.deliver_sms("+15550100", "code 123456") == "sms-1"
    assert sent == [("sms", "+15550100", "code 123456")]


# --------------------------------------------------------------------------- kick bounds
@pytest.fixture
def kick_redis(daemon: None) -> Iterator[redis.Redis]:
    """The test Redis (TEST_REDIS_URL), without kick flags before and after."""
    from app.services.sync import state

    client = get_redis()
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis not available")
    client.delete(state.MATCH_KICK_KEY, state.FLIGHTS_KICK_KEY)
    yield client
    client.delete(state.MATCH_KICK_KEY, state.FLIGHTS_KICK_KEY)


def test_kick_under_the_daemon_writes_the_kick_keys(kick_redis: redis.Redis) -> None:
    from app.services.sync import state

    dispatch.kick("matching.match")
    dispatch.kick("flights.discover", match_ids=["m1", "m2"])
    dispatch.kick("flights.discover")
    assert kick_redis.get(state.MATCH_KICK_KEY)
    assert kick_redis.smembers(state.FLIGHTS_KICK_KEY) == {"m1", "m2", state.ALL_MATCHES}
    assert 0 < kick_redis.ttl(state.FLIGHTS_KICK_KEY) <= state.KICK_TTL_SECONDS
    popped = state.pop_kicks()
    assert popped["flights.discover"] == ["*", "m1", "m2"] and "matching.match" in popped


@pytest.mark.parametrize(
    ("url", "connect_timeout"),
    [
        (f"redis://127.0.0.1:{closed_port()}/0", 5.0),  # refused at once
        ("redis://10.255.255.1:6379/0", 5.0),  # unroutable: the connect would wait 5 s
    ],
    ids=["refused", "blackholed"],
)
def test_kick_returns_within_two_seconds_when_redis_is_down(
    url: str, connect_timeout: float, daemon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", url)
    monkeypatch.setattr(settings, "redis_connect_timeout_seconds", connect_timeout)
    reset_redis()
    try:
        for job, ids in (("matching.match", ()), ("flights.discover", ("m1",))):
            started = time.monotonic()
            dispatch.kick(job, match_ids=ids)  # type: ignore[arg-type]  # never raises
            assert time.monotonic() - started < dispatch.KICK_TIMEOUT_SECONDS + 0.3, job
    finally:
        reset_redis()


def test_kick_returns_within_two_seconds_when_the_broker_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JOB_RUNNER=celery (not eager) with a broker that never answers: kick gives up
    waiting after KICK_TIMEOUT_SECONDS; the publish may still finish later, harmlessly."""
    from app.workers.celery_app import celery_app

    class Hanging:
        def delay(self) -> None:
            time.sleep(5)

    monkeypatch.setattr(get_settings(), "celery_task_always_eager", False)
    monkeypatch.setattr(celery_app, "signature", lambda name, kwargs=None: Hanging())
    started = time.monotonic()
    dispatch.kick("matching.match")
    elapsed = time.monotonic() - started
    assert dispatch.KICK_TIMEOUT_SECONDS - 0.1 <= elapsed < dispatch.KICK_TIMEOUT_SECONDS + 0.5
