"""The map serves the published run (SYNC_PUBLISH_GATE): index, fields and retention.

Owned by the egress-map-serving package. Every field here is SYNTHETIC (built by
tests.synthetic_map_fields: constant made-up values in the real NODD layout); the sync_runs
rows are written by the tests the way the publish stage writes them (published_at and
map_run_id), so no real sync is involved.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine
from app.models import ForecastRun, SyncRun
from app.models.forecast_engine import MapField
from app.services import map_fields as map_fields_service
from app.services.forecasts.ingestion import acquire_map_fields
from app.services.maintenance import cleanup, prune_map_fields
from app.services.sync.published import latest_publication
from tests.conftest import ApiClient
from tests.integration.test_map_fields import CYCLE, NOW, TIMES, fields_only, standalone_world

pytestmark = pytest.mark.integration

NEWER = CYCLE + timedelta(hours=6)  # 2026-10-11 00Z
NEWER_KEY = "noaa-gfs-fields:2026101100"
OLDER_KEY = "noaa-gfs-fields:2026101018"


def _fields_run(db: Session, cycle: str) -> ForecastRun:
    if cycle == "older":
        out = acquire_map_fields(db, now=NOW, provider=fields_only(standalone_world()))
    else:
        world = standalone_world(NEWER, [0])
        out = acquire_map_fields(db, cycle=NEWER, provider=fields_only(world, 0))
    assert out["status"] == "success", out
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None
    return run


@pytest.fixture
def two_runs(db: Session) -> tuple[ForecastRun, ForecastRun]:
    """Two servable fields runs: the 18Z cycle (f000, f012, f024) and the newer 00Z (f000)."""
    older = _fields_run(db, "older")
    newer = _fields_run(db, "newer")
    assert [r.run_id for r in map_fields_service.servable_runs(db)] == [newer.id, older.id]
    return older, newer


def publish(db: Session, map_run: ForecastRun | None, minutes: int = 0) -> SyncRun:
    """A sync_runs row as the publish stage writes it (published_at and map_run_id)."""
    when = NOW + timedelta(minutes=minutes)
    row = SyncRun(
        trigger="pipeline",
        status="success",
        started_at=when - timedelta(minutes=5),
        finished_at=when,
        published_at=when,
        map_run_id=map_run.id if map_run is not None else None,
    )
    db.add(row)
    db.commit()
    return row


def _field_run_key(client: ApiClient, at: str) -> str:
    resp = client.get("/api/map/fields/hs", params={"at": at})
    assert resp.status_code == 200, resp.text
    return str(json.loads(resp.headers["x-field-meta"])["run_key"])


def test_the_published_run_is_served_even_when_a_newer_one_exists(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], app_client: ApiClient
) -> None:
    assert get_settings().sync_publish_gate is True  # the default
    older, newer = two_runs
    row = publish(db, older)

    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == OLDER_KEY
    assert [datetime.fromisoformat(t) for t in body["valid_times"]] == TIMES
    assert body["sync_run_id"] == row.id
    assert datetime.fromisoformat(body["published_at"]) == row.published_at
    # A field without ``run`` is the index's run too, at any time it lists.
    assert _field_run_key(app_client, TIMES[0].isoformat()) == OLDER_KEY
    # A run_key request is unchanged: the newer, retained run is served when named.
    named = app_client.get("/api/map/fields/hs", params={"at": NEWER.isoformat(), "run": NEWER_KEY})
    assert json.loads(named.headers["x-field-meta"])["run_key"] == NEWER_KEY

    # The next publication switches the map to the newer run.
    later = publish(db, newer, minutes=30)
    body = app_client.get("/api/map/fields").json()
    assert (body["run_key"], body["sync_run_id"]) == (NEWER_KEY, later.id)
    assert _field_run_key(app_client, NEWER.isoformat()) == NEWER_KEY


def test_without_a_publication_the_newest_run_is_served(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], app_client: ApiClient
) -> None:
    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == NEWER_KEY
    assert body["published_at"] is None and body["sync_run_id"] is None
    assert _field_run_key(app_client, NEWER.isoformat()) == NEWER_KEY
    # A sync that published no map run (none was servable then) does not pin anything.
    publish(db, None)
    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == NEWER_KEY and body["published_at"] is None


def test_a_published_run_that_is_no_longer_servable_falls_back_to_the_newest(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], app_client: ApiClient
) -> None:
    older, _ = two_runs
    publish(db, older)
    assert app_client.get("/api/map/fields").json()["run_key"] == OLDER_KEY

    # Its fields pruned (here: deleted outright): the newest servable run is served, and the
    # index does not claim a publication for it.
    db.execute(delete(MapField).where(MapField.run_id == older.id))
    db.commit()
    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == NEWER_KEY
    assert body["published_at"] is None and body["sync_run_id"] is None
    assert _field_run_key(app_client, NEWER.isoformat()) == NEWER_KEY


def test_a_deleted_published_run_falls_back_to_the_newest(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], app_client: ApiClient
) -> None:
    older, _ = two_runs
    row = publish(db, older)
    db.execute(delete(ForecastRun).where(ForecastRun.id == older.id))  # fields cascade
    db.commit()
    db.refresh(row)
    assert row.map_run_id is None  # ON DELETE SET NULL
    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == NEWER_KEY and body["sync_run_id"] is None


def test_with_the_gate_off_the_newest_run_is_served(
    db: Session,
    two_runs: tuple[ForecastRun, ForecastRun],
    app_client: ApiClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older, newer = two_runs
    publish(db, older)
    monkeypatch.setattr(get_settings(), "sync_publish_gate", False)
    body = app_client.get("/api/map/fields").json()
    assert body["run_key"] == NEWER_KEY
    # The served run is not the published one: no publication is claimed for it.
    assert body["published_at"] is None and body["sync_run_id"] is None
    assert _field_run_key(app_client, NEWER.isoformat()) == NEWER_KEY
    # When the newest is the published run, the index names that publication.
    row = publish(db, newer, minutes=30)
    assert app_client.get("/api/map/fields").json()["sync_run_id"] == row.id


def test_served_run_is_the_index_run(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    older, newer = two_runs

    def served() -> int | None:
        run = map_fields_service.served_run(db)
        return None if run is None else run.run_id

    assert served() == newer.id  # nothing published yet
    publish(db, older)
    assert served() == older.id
    assert map_fields_service.field_index(db, NOW).run_key == OLDER_KEY
    monkeypatch.setattr(get_settings(), "sync_publish_gate", False)
    assert served() == newer.id
    assert map_fields_service.field_index(db, NOW).run_key == NEWER_KEY
    db.execute(delete(MapField))
    db.commit()
    assert served() is None


def test_the_index_names_the_publication_latest_publication_returns(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun]
) -> None:
    older, newer = two_runs
    first = publish(db, newer, minutes=10)
    tie = publish(db, newer, minutes=10)  # same published_at: the higher id wins
    publish(db, older, minutes=5)  # published earlier, inserted later
    db.add(SyncRun(trigger="cli", status="running", started_at=NOW + timedelta(hours=1)))
    db.commit()
    newest = latest_publication(db)
    assert newest is not None and newest.id == tie.id and tie.id > first.id
    index = map_fields_service.field_index(db, NOW)
    assert (index.sync_run_id, index.published_at) == (newest.id, newest.published_at)
    assert index.run_key == NEWER_KEY


def test_the_index_reads_no_plan_or_stages(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun]
) -> None:
    older, _ = two_runs
    publish(db, older)
    seen: list[str] = []

    def capture(*args: object) -> None:
        seen.append(str(args[2]))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert map_fields_service.field_index(db, NOW).run_key == OLDER_KEY
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    sync_reads = [s for s in seen if "FROM sync_runs" in s]
    assert sync_reads and not any("sync_runs.plan" in s or "sync_runs.stages" in s for s in seen)
    # Nor the run's details (the map summary and warnings): only the columns it reports.
    assert not any("forecast_runs.details" in s for s in seen)


def test_retention_keeps_the_published_run_besides_the_two_best(
    db: Session, two_runs: tuple[ForecastRun, ForecastRun], app_client: ApiClient
) -> None:
    older, newer = two_runs
    publish(db, older)
    # A third, newer cycle arrives before any sync publishes it (e.g. two syncs whose
    # publish stage failed, or a manual `engine map-fields`): the published run is no longer
    # among the two best, and is kept anyway because the map serves it.
    third = NEWER + timedelta(hours=6)
    out = acquire_map_fields(db, cycle=third, provider=fields_only(standalone_world(third, [0]), 0))
    assert out["status"] == "success" and out["pruned_fields"] == 0
    kept = set(db.scalars(select(MapField.run_id).distinct()))
    assert kept == {older.id, newer.id, out["run_id"]}
    assert app_client.get("/api/map/fields").json()["run_key"] == OLDER_KEY

    # Once the newest run is published, the old one goes at the next prune.
    third_run = db.get(ForecastRun, out["run_id"])
    publish(db, third_run, minutes=30)
    assert prune_map_fields(db) == 18  # the 18Z run: 3 valid times x 6 variables
    db.commit()
    assert set(db.scalars(select(MapField.run_id).distinct())) == {newer.id, out["run_id"]}
    assert cleanup(db, now=NOW)["map_fields"] == 0
    count = db.scalar(select(func.count()).select_from(MapField))
    assert count == 12  # two runs x one valid time x 6 variables
