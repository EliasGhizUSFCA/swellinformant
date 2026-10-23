"""The sync lease on a hosted daemon (Upgrade 4 integration).

Vercel stops a daemon at a deploy with SIGTERM and kills it 120 s later; a full map stage
can take longer, so a sync can die mid-stage and leave its lease. Under JOB_RUNNER=daemon
the lease is 15 minutes unless SYNC_LEASE_MINUTES says otherwise (45 under Celery), so the
next daemon takes over soon, and a keeper extends it every third while a stage runs, so a
stage longer than the lease never loses it. Demo spot forecasts and SYNTHETIC map fields
(tests/sync_fixtures.py).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import DAEMON_LEASE_MINUTES, Settings, get_settings
from app.core.database import utcnow
from app.models.sync import SyncRun
from app.services.sync import orchestrator
from app.services.sync.types import SyncPlan
from tests.sync_fixtures import NOW, MapWorld, install_synthetic_map

pytestmark = pytest.mark.integration


def plan() -> SyncPlan:
    return SyncPlan(checked_at=utcnow(), due=True, components=(), anchor_cycle=None, reasons=())


def expiry(db: Session, row_id: int) -> Any:
    db.expire_all()
    return db.scalar(select(SyncRun.lease_expires_at).where(SyncRun.id == row_id))


def test_the_daemon_lease_is_short_unless_set() -> None:
    def make(**kw: Any) -> Settings:
        return Settings(_env_file=None, **kw)  # type: ignore[arg-type]

    assert make().sync_lease_minutes == 45  # Celery (classic compose): unchanged
    assert make(job_runner="daemon").sync_lease_minutes == DAEMON_LEASE_MINUTES == 15
    assert make(job_runner="daemon", sync_lease_minutes=45).sync_lease_minutes == 45


def test_the_keeper_extends_the_lease_while_a_stage_runs(db: Session) -> None:
    lease = orchestrator._Lease.acquire(trigger="cli", plan=plan(), settings=get_settings())
    db.execute(
        update(SyncRun)
        .where(SyncRun.id == lease.row_id)
        .values(lease_expires_at=utcnow() + timedelta(seconds=30))
    )
    db.commit()
    with orchestrator._LeaseKeeper(lease, interval_s=0.05) as keeper:
        time.sleep(0.4)
    assert keeper.extended >= 2
    ahead = expiry(db, lease.row_id) - utcnow()
    assert timedelta(minutes=lease.minutes) - timedelta(seconds=5) < ahead
    assert lease.release("success", [], None)


def test_the_keeper_stops_when_the_lease_was_taken_over(db: Session) -> None:
    lease = orchestrator._Lease.acquire(trigger="cli", plan=plan(), settings=get_settings())
    db.execute(update(SyncRun).where(SyncRun.id == lease.row_id).values(status="abandoned"))
    db.commit()
    keeper = orchestrator._LeaseKeeper(lease, interval_s=0.05)
    with keeper:
        time.sleep(0.3)
        assert not keeper._thread.is_alive()  # gave up at its first extension
    assert keeper.extended == 0
    assert db.get(SyncRun, lease.row_id).status == "abandoned"  # type: ignore[union-attr]


def test_a_sync_keeps_its_lease_through_a_stage_longer_than_the_keeper_interval(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    world: MapWorld = install_synthetic_map(monkeypatch)
    assert world is not None
    from app.services.surf_quality import predict

    real_keeper = orchestrator._LeaseKeeper
    monkeypatch.setattr(
        orchestrator, "_LeaseKeeper", lambda lease: real_keeper(lease, interval_s=0.05)
    )
    seen: list[Any] = []
    real_predict = predict.compute_pending_predictions

    def slow(db: Session, now: Any = None) -> Any:
        row = select(SyncRun.lease_expires_at).where(SyncRun.status == "running")
        seen.append(db.scalar(row))
        time.sleep(0.4)  # a stage that outlasts several keeper intervals
        db.expire_all()
        seen.append(db.scalar(row))
        return real_predict(db, now)

    monkeypatch.setattr(predict, "compute_pending_predictions", slow)
    result = orchestrator.run_sync(trigger="daemon", now=NOW)
    assert result.status == "success"
    assert seen[1] > seen[0]  # extended during the stage, not only after it
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "success" and row.lease_expires_at is None
