"""Jobs and syncs when Redis does not answer (Upgrade 4 integration).

Under JOB_RUNNER=daemon a job runs without its Redis lock when Redis does not answer (the
daemon runs one job at a time and a sync holds the Postgres lease); under Celery it fails
as before. Every lock call goes through the configured client, so REDIS_CONNECT_TIMEOUT_
SECONDS, REDIS_SOCKET_TIMEOUT_SECONDS and the retry policy bound it.

The unreachable Redis is a local stand-in: a port nobody listens on (refused) or a socket
that accepts connections and never answers (silent, like a blackholed host after the TCP
handshake). The sync uses the demo provider (synthetic data by design).
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis import get_redis, reset_redis
from app.jobs.runner import LOCK_NOT_TAKEN, run_job
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.services.sync.orchestrator import run_sync

pytestmark = pytest.mark.integration

TIMEOUT_S = 0.5


@pytest.fixture
def silent_port() -> Iterator[int]:
    """A TCP port that accepts connections (kernel backlog) and never answers."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()


@pytest.fixture
def refused_port() -> int:
    """A TCP port nobody listens on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


@pytest.fixture
def redis_at(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Point the app's Redis client at a port, with short hosted-style timeouts."""

    def point(port: int, runner: str) -> None:
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
        monkeypatch.setenv("REDIS_CONNECT_TIMEOUT_SECONDS", str(TIMEOUT_S))
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_SECONDS", str(TIMEOUT_S))
        monkeypatch.setenv("JOB_RUNNER", runner)
        get_settings.cache_clear()
        reset_redis()

    yield point
    monkeypatch.undo()
    get_settings.cache_clear()
    reset_redis()


def logs(db: Session, name: str) -> list[BackgroundJobLog]:
    db.expire_all()
    stmt = select(BackgroundJobLog).where(BackgroundJobLog.job_name == name)
    return list(db.scalars(stmt.order_by(BackgroundJobLog.id)))


def test_lock_calls_use_the_configured_timeouts_and_retry(redis_at: Any, silent_port: int) -> None:
    redis_at(silent_port, "daemon")
    pool_kwargs = get_redis().connection_pool.connection_kwargs
    assert pool_kwargs["socket_connect_timeout"] == TIMEOUT_S
    assert pool_kwargs["socket_timeout"] == TIMEOUT_S
    retry = pool_kwargs["retry"]
    assert retry.get_retries() == 1  # one retry after a connection error, none after a timeout
    started = time.monotonic()
    with pytest.raises(redis.TimeoutError):
        get_redis().set("lock:job:synthetic", "token", nx=True, ex=60)
    assert time.monotonic() - started < TIMEOUT_S + 1.0


@pytest.mark.parametrize("unreachable", ["silent", "refused"])
def test_under_the_daemon_a_job_runs_without_its_lock(
    db: Session,
    redis_at: Any,
    silent_port: int,
    refused_port: int,
    unreachable: str,
) -> None:
    redis_at(silent_port if unreachable == "silent" else refused_port, "daemon")
    calls: list[int] = []
    started = time.monotonic()
    out = run_job("maintenance.cleanup", lambda _db: calls.append(1) or {"done": True})
    elapsed = time.monotonic() - started
    assert out["status"] == "success" and out["lock"] == LOCK_NOT_TAKEN and calls == [1]
    (row,) = logs(db, "maintenance.cleanup")
    assert row.status == JobStatus.SUCCESS and row.details["lock"] == LOCK_NOT_TAKEN
    # The lock and the CPU counter each wait out one timeout at most (silent), or fail at
    # once (refused).
    assert elapsed < 4 * TIMEOUT_S + 1.0


def test_under_celery_a_job_without_its_lock_fails(
    db: Session, redis_at: Any, silent_port: int
) -> None:
    redis_at(silent_port, "celery")
    calls: list[int] = []
    started = time.monotonic()
    with pytest.raises(redis.RedisError):
        run_job("maintenance.cleanup", lambda _db: calls.append(1) or {})
    assert time.monotonic() - started < 4 * TIMEOUT_S + 1.0
    assert calls == []  # several workers could run it: never without the lock
    (row,) = logs(db, "maintenance.cleanup")
    assert row.status == JobStatus.FAILED and "TimeoutError" in (row.error_message or "")


def test_a_redis_error_inside_the_job_is_the_jobs_own(
    db: Session, redis_at: Any, refused_port: int
) -> None:
    """Only taking the lock is excused: the body is never run twice or without a failure."""
    redis_at(refused_port, "daemon")
    calls: list[int] = []

    def body(_db: Session) -> dict[str, Any]:
        calls.append(1)
        raise redis.ConnectionError("synthetic: the job's own Redis call")

    with pytest.raises(redis.ConnectionError):
        run_job("maintenance.cleanup", body)
    assert calls == [1]
    (row,) = logs(db, "maintenance.cleanup")
    assert row.status == JobStatus.FAILED


@pytest.mark.parametrize("runner", ["daemon", "celery"])
def test_a_sync_with_redis_down(
    db: Session, few_spots: list[str], redis_at: Any, refused_port: int, runner: str
) -> None:
    """The sync-core review measured every stage failing on its lock (the whole sync failed
    in 0.4 s). Under the daemon it now stores and publishes; Celery keeps failing."""
    redis_at(refused_port, runner)
    result = run_sync(trigger="cli", force=True)
    stages = {s.name: s.status for s in result.stages}
    if runner == "daemon":
        assert result.status == "success" and result.published_at is not None
        assert stages["spots"] == stages["predict"] == stages["publish"] == "success"
        assert all(r.details.get("lock") == LOCK_NOT_TAKEN for r in logs(db, "quality.predict"))
    else:
        assert result.status == "failed" and result.published_at is None
        assert stages["spots"] == stages["predict"] == stages["publish"] == "failed"
