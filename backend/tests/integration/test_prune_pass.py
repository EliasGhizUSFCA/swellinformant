"""maintenance.prune_pass, the prune after every sync (SYNC_PRUNE_EACH_PASS). Owned by the
egress-map-serving package.

prune_pass deletes derived data the sync can recompute: the predictions of superseded member
runs (when PRUNE_SUPERSEDED_MEMBER_PREDICTIONS) and the map fields of runs that are neither
among the two best servable ones nor the newest publication's map run. Since the egress
integration it also applies the forecast-run retention (RETENTION_FORECAST_DAYS, the daily
cleanup's rule, so the free database profile's 1 day holds between cleanups; its return dict
gained ``forecast_runs``). Then it commits.
Every forecast and field here is SYNTHETIC (tests.integration.test_retention_bounds'
constant sea state; tests.synthetic_map_fields' constant GRIB fields).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import ForecastRun, SurfQualityPrediction, SyncRun
from app.models.forecast_engine import MapField
from app.services import maintenance
from app.services.forecasts import blend
from app.services.forecasts.ingestion import acquire_map_fields
from app.services.maintenance import prune_pass
from app.services.surf_quality.predict import compute_pending_predictions
from tests.integration.test_map_fields import CYCLE, fields_only, standalone_world
from tests.integration.test_retention_bounds import (  # noqa: F401 (codes is a fixture)
    NOW,
    SyntheticProvider,
    _ingest,
    _predictions,
    codes,
)

pytestmark = pytest.mark.integration


def test_prune_pass_on_an_empty_database(db: Session) -> None:
    assert prune_pass(db) == {"superseded_predictions": 0, "map_fields": 0, "forecast_runs": 0}


@pytest.mark.parametrize("prune_predictions", [True, False])
def test_prune_pass_runs_both_prunes_and_commits(
    db: Session, monkeypatch: pytest.MonkeyPatch, prune_predictions: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(get_settings(), "prune_superseded_member_predictions", prune_predictions)
    monkeypatch.setattr(
        maintenance,
        "prune_superseded_predictions",
        lambda _db, now=None: calls.append("predictions") or 4,
    )
    monkeypatch.setattr(maintenance, "prune_map_fields", lambda _db: calls.append("fields") or 7)
    monkeypatch.setattr(
        maintenance, "prune_forecast_runs", lambda _db, now: calls.append("runs") or 2
    )
    committed: list[bool] = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))
    out = prune_pass(db)
    assert out == {
        "superseded_predictions": 4 if prune_predictions else 0,
        "map_fields": 7,
        "forecast_runs": 2,
    }
    assert calls == (["fields", "runs", "predictions"] if prune_predictions else ["fields", "runs"])
    assert committed == [True]


def _fields(db: Session, hours_after: int) -> ForecastRun:
    cycle = CYCLE + timedelta(hours=hours_after)
    world = standalone_world(cycle, [0])
    out = acquire_map_fields(db, cycle=cycle, provider=fields_only(world, 0))
    assert out["status"] == "success", out
    run = db.get(ForecastRun, out["run_id"])
    assert run is not None
    return run


@pytest.mark.usefixtures("codes")  # two synthetic member sources t_gfs and t_ecmwf
@pytest.mark.parametrize("prune_predictions", [True, False])
def test_a_pass_prunes_superseded_predictions_and_unserved_fields(
    db: Session, monkeypatch: pytest.MonkeyPatch, prune_predictions: bool
) -> None:
    monkeypatch.setattr(get_settings(), "prune_superseded_member_predictions", prune_predictions)
    # Spot runs: a superseded GFS run, the newest GFS and ECMWF runs and their blend.
    old = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=12)))
    new = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=6)))
    ec = _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=6)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    blend_run = db.scalars(select(ForecastRun).where(ForecastRun.run_key.like("blend:%"))).one()
    superseded = _predictions(db, old)
    assert superseded > 0
    # Map runs: the published one, then two newer ones a failed publication left behind
    # (each acquisition prunes to the best two plus the published run, so all three stay).
    published = _fields(db, 0)
    db.add(
        SyncRun(
            trigger="pipeline",
            status="success",
            started_at=NOW,
            published_at=NOW,
            map_run_id=published.id,
        )
    )
    db.commit()
    _fields(db, 6)
    _fields(db, 12)
    assert db.scalar(select(func.count(func.distinct(MapField.run_id)))) == 3
    # A fourth, unservable run (no hs anywhere): never among the best, never published.
    rows = [r for r in db.scalars(select(MapField).where(MapField.run_id == published.id))]
    stray = ForecastRun(
        source_id=published.source_id,
        run_key="stray",
        issued_at=CYCLE + timedelta(hours=18),
        started_at=NOW,
        status=published.status,
        details={},
    )
    db.add(stray)
    db.flush()
    columns = [c.key for c in MapField.__table__.columns if c.key not in ("id", "run_id")]
    for r in rows:
        if r.variable != "hs":
            db.add(MapField(**{c: getattr(r, c) for c in columns}, run_id=stray.id))
    db.commit()

    out = prune_pass(db, NOW)

    assert out == {
        "superseded_predictions": superseded if prune_predictions else 0,
        "map_fields": 5,  # the stray run's five variables
        "forecast_runs": 0,  # every run is younger than RETENTION_FORECAST_DAYS
    }
    with SessionLocal() as other:  # committed: another session sees the result
        runs = set(other.scalars(select(MapField.run_id).distinct()))
        assert stray.id not in runs and published.id in runs and len(runs) == 3
        kept = other.scalar(
            select(func.count())
            .select_from(SurfQualityPrediction)
            .where(SurfQualityPrediction.run_id == old.id)
        )
        assert kept == (0 if prune_predictions else superseded)
    assert all(_predictions(db, r) > 0 for r in (new, ec, blend_run))  # never pruned here
    again = prune_pass(db, NOW)  # idempotent
    assert again == {"superseded_predictions": 0, "map_fields": 0, "forecast_runs": 0}
