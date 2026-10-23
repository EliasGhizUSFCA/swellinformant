"""Celery's side of the synced pipeline (D14): the sync.tick task and the messaging tasks
with the broker down.

* ``sync.tick`` runs registry job ``sync.tick`` (app.services.sync.orchestrator.tick with
  trigger "celery") through the job runner: a job log row, the 1 h job lock, chain off.
* With the demo provider (synthetic forecasts by design), one tick syncs and publishes and
  a second tick is not due (sync-core's orchestrator; never skipped since the integration).
* With the broker refusing connections, registering an account (whose verification email
  is a messaging task) still answers in under 3 s: the messaging tasks ignore their
  result, so ``.delay()`` fails fast and the email is sent inline.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis import redis_lock
from app.models import BackgroundJobLog
from app.models.enums import JobStatus
from app.models.sync import SyncRun
from app.services.sync import orchestrator
from app.services.sync.published import latest_publication
from app.workers import tasks
from app.workers.celery_app import celery_app
from tests.conftest import ApiClient, outbox_messages

pytestmark = pytest.mark.integration

REFUSED_BROKER = "redis://127.0.0.1:1/0"  # nothing listens on port 1: connection refused


def tick_logs(db: Session) -> list[BackgroundJobLog]:
    db.expire_all()
    return list(
        db.scalars(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == "sync.tick")
            .order_by(BackgroundJobLog.id)
        ).all()
    )


# --------------------------------------------------------------------------- the task
def test_sync_tick_runs_the_registry_job_with_the_celery_trigger(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_tick(*, trigger: str) -> dict[str, Any]:  # stands in for sync-core's tick
        calls.append(trigger)
        return {"due": False, "reasons": ["synthetic: nothing new"], "status": "not_due"}

    monkeypatch.setattr(orchestrator, "tick", fake_tick)
    result = tasks.sync_tick.delay().get()
    assert calls == ["celery"]
    # The job's status, and the check's outcome under its own key.
    assert result["status"] == "success" and result["sync_status"] == "not_due"
    assert result["reasons"] == ["synthetic: nothing new"]
    (row,) = tick_logs(db)
    assert row.status == JobStatus.SUCCESS and row.finished_at is not None
    assert isinstance(row.details["cpu_s"], float) and row.details["due"] is False
    assert row.details["sync_status"] == "not_due" and "status" not in row.details
    # chain=False: nothing is queued after the check (the sync runs its own stages).
    db.expire_all()
    names = set(db.scalars(select(BackgroundJobLog.job_name)).all())
    assert names == {"sync.tick"}


def test_a_check_while_another_holds_the_job_lock_is_skipped(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "tick", lambda *, trigger: calls.append(trigger) or {})
    with redis_lock("job:sync.tick", ttl_seconds=30):
        result = tasks.sync_tick.delay().get()
    assert result["status"] == "skipped" and calls == []
    assert tick_logs(db)[-1].status == JobStatus.SKIPPED


def test_the_job_lock_lasts_as_long_as_the_sync_may(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.redis import get_redis

    seen: list[int] = []

    def fake_tick(*, trigger: str) -> dict[str, Any]:
        seen.append(int(get_redis().ttl("lock:job:sync.tick")))
        return {"status": "not_due"}

    monkeypatch.setattr(orchestrator, "tick", fake_tick)
    tasks.sync_tick.delay().get()
    assert len(seen) == 1 and 3590 <= seen[0] <= 3600


def test_a_failing_check_is_logged_failed(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*, trigger: str) -> dict[str, Any]:
        raise RuntimeError("synthetic failure with api_key=SECRET123")

    monkeypatch.setattr(orchestrator, "tick", boom)
    with pytest.raises(RuntimeError):
        tasks.sync_tick.delay().get()
    row = tick_logs(db)[-1]
    assert row.status == JobStatus.FAILED and "synthetic failure" in (row.error_message or "")
    assert "SECRET123" not in (row.error_message or "")


def test_one_tick_syncs_and_publishes_and_the_next_is_not_due(
    db: Session, few_spots: list[str]
) -> None:
    """The real orchestrator (sync-core) with the demo provider: synthetic forecasts."""
    first = tasks.sync_tick.delay().get()
    assert first["status"] == "success" and first["sync_status"] == "success", first
    assert first["due"] is True and first["sync_run_id"] is not None
    assert first["published_at"] is not None
    db.expire_all()
    run = db.get(SyncRun, first["sync_run_id"])
    assert run is not None and run.trigger == "celery" and run.published_at is not None
    publication = latest_publication(db)
    assert publication is not None and publication.id == run.id
    names = {s["name"]: s["status"] for s in run.stages}
    assert names["spots"] == "success" and names["publish"] == "success"

    second = tasks.sync_tick.delay().get()
    assert second["status"] == "success" and second["sync_status"] == "not_due", second
    assert second["due"] is False
    assert second["sync_run_id"] is None
    db.expire_all()
    assert len(db.scalars(select(SyncRun)).all()) == 1  # no second sync
    assert [r.status for r in tick_logs(db)] == [JobStatus.SUCCESS, JobStatus.SUCCESS]


# --------------------------------------------------------------------------- broker down
@pytest.fixture
def refused_broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Celery in real (not eager) mode with Redis refusing connections: the broker and the
    result backend (one Redis in the classic stack, REDIS_URL)."""
    conf = celery_app.conf
    saved = (conf.broker_url, conf.result_backend, conf.task_always_eager, celery_app._pool)
    saved_producers = celery_app.amqp._producer_pool
    saved_backend = (celery_app._backend_cache, getattr(celery_app._local, "backend", None))
    conf.broker_url = conf.result_backend = REFUSED_BROKER
    conf.task_always_eager = False
    celery_app._pool = None
    celery_app.amqp._producer_pool = None
    celery_app._backend_cache = None
    celery_app._local.backend = None
    monkeypatch.setattr(get_settings(), "celery_task_always_eager", False)
    monkeypatch.setattr(get_settings(), "job_runner", "celery")
    try:
        assert "127.0.0.1:1" in str(celery_app.backend.url)  # the refused result backend
        yield
    finally:
        # The refused broker's pool stays in kombu's registry (it holds no connection).
        conf.broker_url, conf.result_backend, conf.task_always_eager, celery_app._pool = saved
        celery_app.amqp._producer_pool = saved_producers
        celery_app._backend_cache, celery_app._local.backend = saved_backend


@pytest.mark.parametrize(
    "send",
    [
        lambda: tasks.send_account_email.delay("00000000-0000-0000-0000-000000000000", "t", None),
        lambda: tasks.send_email.delay("synthetic@example.com", "s", "t", None, "synthetic"),
        lambda: tasks.send_sms.delay("+15550000000", "synthetic"),
    ],
    ids=["account_email", "email", "sms"],
)
def test_a_messaging_task_fails_fast_with_the_broker_refused(
    refused_broker: None, send: Any
) -> None:
    from kombu.exceptions import OperationalError

    started = time.monotonic()
    with pytest.raises(OperationalError):
        send()
    # Was a RuntimeError after 19 s: the result backend's subscription (measured 2026-10-21).
    assert time.monotonic() - started < 3.0


def test_register_answers_within_3_s_with_the_broker_refused(
    db: Session, app_client: ApiClient, refused_broker: None
) -> None:
    started = time.monotonic()
    resp = app_client.post(
        "/api/auth/register",
        {
            "full_name": "Synthetic Surfer",
            "email": "broker-down@example.com",
            "password": "Barrels4Days!",
            "confirm_password": "Barrels4Days!",
        },
    )
    elapsed = time.monotonic() - started
    assert resp.status_code == 201, resp.text
    assert elapsed < 3.0, elapsed
    # The verification email went out inline (the console provider's outbox).
    mails = [m for m in outbox_messages() if m.get("to") == "broker-down@example.com"]
    assert mails and "verify-email" in str(mails[0].get("text"))
