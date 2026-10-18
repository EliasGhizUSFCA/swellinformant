"""sync_runs (foundation F2) and the published-sync queries (F3, published.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ForecastRun, ForecastSource, SyncRun
from app.models.enums import ForecastRunStatus
from app.services.sync.published import latest_publication, published_map_run_id

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def forecast_run(db: Session, key: str) -> ForecastRun:
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "sync_test_fields"))
    if source is None:
        source = ForecastSource(
            code="sync_test_fields",
            name="sync test fields",
            provider="noaa",
            is_demo=False,
            attribution="",
            license_notes="",
        )
        db.add(source)
        db.flush()
    run = ForecastRun(
        source_id=source.id,
        run_key=key,
        issued_at=T0,
        started_at=T0,
        status=ForecastRunStatus.SUCCESS,
        details={},
    )
    db.add(run)
    db.flush()
    return run


def sync_run(db: Session, status: str = "success", **kw: object) -> SyncRun:
    row = SyncRun(trigger=kw.pop("trigger", "cli"), status=status, **kw)
    db.add(row)
    db.flush()
    return row


def test_defaults_and_columns(db: Session) -> None:
    row = sync_run(db, status="running", holder="host:1:tok", lease_expires_at=T0)
    db.commit()
    db.expire_all()
    stored = db.get(SyncRun, row.id)
    assert stored is not None
    assert stored.plan == {} and stored.stages == []
    assert stored.started_at is not None and stored.started_at.tzinfo is not None
    assert stored.published_at is None and stored.map_run_id is None
    assert stored.blend_run_id is None and stored.error_message is None


def test_at_most_one_running_sync(db: Session) -> None:
    sync_run(db, status="running")
    sync_run(db, status="success")
    sync_run(db, status="abandoned")
    db.commit()
    with pytest.raises(IntegrityError, match="uq_sync_runs_running"):
        sync_run(db, status="running")
    db.rollback()


@pytest.mark.parametrize(("field", "value"), [("status", "done"), ("trigger", "cron")])
def test_status_and_trigger_are_checked(db: Session, field: str, value: str) -> None:
    values = {"status": "success", "trigger": "cli", field: value}
    db.add(SyncRun(**values))
    with pytest.raises(IntegrityError, match=f"ck_sync_runs_{field}"):
        db.flush()
    db.rollback()


def test_deleting_a_forecast_run_keeps_the_sync_row(db: Session) -> None:
    map_run = forecast_run(db, "map")
    blend_run = forecast_run(db, "blend")
    row = sync_run(db, published_at=T0, map_run_id=map_run.id, blend_run_id=blend_run.id)
    db.commit()
    db.execute(delete(ForecastRun).where(ForecastRun.id.in_([map_run.id, blend_run.id])))
    db.commit()
    db.expire_all()
    stored = db.get(SyncRun, row.id)
    assert stored is not None
    assert stored.map_run_id is None and stored.blend_run_id is None


def test_latest_publication(db: Session) -> None:
    assert latest_publication(db) is None and published_map_run_id(db) is None
    older_map = forecast_run(db, "older")
    newer_map = forecast_run(db, "newer")
    older = sync_run(db, published_at=T0, map_run_id=older_map.id)
    newest = sync_run(db, published_at=T0 + timedelta(hours=6), map_run_id=newer_map.id)
    # Newer rows that published nothing do not count.
    sync_run(db, status="failed", started_at=T0 + timedelta(hours=7))
    sync_run(db, status="running", started_at=T0 + timedelta(hours=8))
    db.commit()
    latest = latest_publication(db)
    assert latest is not None and latest.id == newest.id
    assert published_map_run_id(db) == newer_map.id
    # Same publication time: the higher id wins.
    tie = sync_run(db, published_at=T0 + timedelta(hours=6), map_run_id=older_map.id)
    db.commit()
    tied = latest_publication(db)
    assert tied is not None and tied.id == tie.id > newest.id
    assert published_map_run_id(db) == older_map.id
    # A publication without a servable map run: no map run is published.
    sync_run(db, published_at=T0 + timedelta(hours=12), map_run_id=None)
    db.commit()
    assert published_map_run_id(db) is None
    assert older.id < newest.id
