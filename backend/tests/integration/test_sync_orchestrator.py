"""The sync orchestrator, the publish stage and the ``sync`` commands (docs/sync.md).

Demo spot forecasts and SYNTHETIC map fields (tests/sync_fixtures.py): one sync stores,
publishes and sets the map run; a second check is not due; a second concurrent sync gets
SyncBusy; a crashed sync's lease expires and the next sync marks it abandoned and finishes
its work; every stage is logged with its CPU time; ``stop`` ends a sync after the current
stage.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app import cli
from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import get_redis
from app.models import BackgroundJobLog, ForecastRun, WaveForecast
from app.models.forecast_engine import MapField
from app.models.sync import SyncRun
from app.services.sync import cli as sync_cli
from app.services.sync import orchestrator, state
from app.services.sync.detect import empty_known, plan_sync
from app.services.sync.published import latest_publication, published_map_run_id
from app.services.sync.types import STAGE_JOB_NAMES, STAGES, SyncBusy
from tests.sync_fixtures import CYCLE, NOW, MapWorld, install_synthetic_map

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_map(monkeypatch: pytest.MonkeyPatch, few_spots: list[str]) -> MapWorld:
    return install_synthetic_map(monkeypatch)


def stages(result: Any) -> dict[str, str]:
    return {s.name: s.status for s in result.stages}


def counts(db: Session) -> tuple[int, int, int]:
    db.expire_all()
    return (
        int(db.scalar(select(func.count()).select_from(WaveForecast)) or 0),
        int(db.scalar(select(func.count()).select_from(MapField)) or 0),
        int(db.scalar(select(func.count()).select_from(ForecastRun)) or 0),
    )


def running_row(db: Session, *, expires_in: timedelta) -> SyncRun:
    """A sync of another process that holds the lease (or held it, when expired)."""
    row = SyncRun(
        trigger="daemon",
        status="running",
        started_at=utcnow() - timedelta(minutes=5),
        lease_expires_at=utcnow() + expires_in,
        holder="elsewhere:1234:abcd",
        plan={},
        stages=[],
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- one sync
def test_one_sync_stores_publishes_and_records_every_stage(
    db: Session, synthetic_map: MapWorld
) -> None:
    result = orchestrator.run_sync(trigger="cli", now=NOW)
    assert result.status == "success", result.as_dict()
    assert [s.name for s in result.stages] == list(STAGES)
    assert stages(result) == {
        "spots": "success",
        "ensemble": "skipped",  # GEFS_ENABLED=false: the component is disabled
        "map": "success",
        # One provider: nothing to blend, decided without importing the blend (Swell 2.0:
        # the refresh Function has no pandas).
        "blend": "skipped",
        "predict": "success",
        "publish": "success",
        "detect": "success",
        "match": "success",
        "flights": "success",
        "alerts": "success",
        "deliver": "success",
        "prune": "skipped",  # SYNC_PRUNE_EACH_PASS=false
    }
    ensemble = next(s for s in result.stages if s.name == "ensemble")
    assert ensemble.detail == {"reason": "disabled (GEFS_ENABLED=false)"}
    blend = next(s for s in result.stages if s.name == "blend")
    assert blend.detail == {"reason": "one forecast provider: nothing to blend"}
    spots = next(s for s in result.stages if s.name == "spots")
    # One entry per component (design D6): the demo's wave and atmosphere parts, both new.
    assert spots.detail["components"] == [
        {
            "source_code": "demo",
            "provider": "demo",
            "component": component,
            "outcome": "stored",
            "message": "",
            "run_key": f"demo:{CYCLE:%Y%m%d%H}",
            "retry_after_s": None,
        }
        for component in ("wave", "atmosphere")
    ]
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "success" and row.trigger == "cli"
    assert row.lease_expires_at is None and row.finished_at is not None
    assert row.published_at == result.published_at is not None
    fields_run = db.scalar(select(ForecastRun).where(ForecastRun.run_key.like("noaa-gfs-fields:%")))
    assert fields_run is not None and row.map_run_id == fields_run.id
    assert published_map_run_id(db) == fields_run.id
    assert latest_publication(db) == row
    assert row.plan["due"] is True and row.error_message is None
    # sync_runs.stages: one entry per stage with duration and CPU seconds.
    assert [s["name"] for s in row.stages] == list(STAGES)
    for entry in row.stages:
        assert set(entry) == {"name", "status", "started_at", "duration_ms", "cpu_s", "detail"}
        assert isinstance(entry["cpu_s"], float) and entry["cpu_s"] >= 0
    assert sum(s["cpu_s"] for s in row.stages) > 0
    # One job log per stage that ran (existing job names), plus one sync.run row.
    logs = {log.job_name: log for log in db.scalars(select(BackgroundJobLog)).all()}
    ran = [STAGE_JOB_NAMES[n] for n, status in stages(result).items() if status != "skipped"]
    assert set(logs) == {*ran, "sync.run"}
    assert all(isinstance(log.details.get("cpu_s"), float) for log in logs.values())
    run_log = logs["sync.run"]
    assert run_log.status.value == "success"
    assert run_log.details["sync_run_id"] == row.id and run_log.details["sync_status"] == "success"
    assert run_log.details["cpu_s_total"] >= run_log.details["cpu_s"] >= 0

    # An immediate second check finds nothing new, and a sync is skipped without a row.
    later = NOW + timedelta(minutes=10)
    plan = plan_sync(db, now=later)
    assert not plan.due
    assert {c.source_code: c.state for c in plan.components} == {
        "demo": "current",
        "noaa_gefs_wave": "disabled",
        "noaa_gfs_fields": "current",
    }
    again = orchestrator.run_sync(trigger="cli", now=later)
    assert again.status == "skipped" and again.sync_run_id is None and again.stages == []
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 1


def test_a_forced_second_sync_is_idempotent(db: Session, synthetic_map: MapWorld) -> None:
    first = orchestrator.run_sync(trigger="cli", now=NOW)
    assert first.status == "success"
    before = counts(db)
    requests = len(synthetic_map.world.requests)
    second = orchestrator.run_sync(trigger="cli", force=True, now=NOW)
    assert second.status == "success" and second.plan.forced
    outcomes = [
        c["outcome"]
        for s in second.stages
        if s.name in ("spots", "map")
        for c in s.detail["components"]
    ]
    assert outcomes == ["current", "current", "current"]  # demo wave, demo atmosphere, map
    # Force acquires every configured component, never a disabled one.
    ensemble = next(s for s in second.stages if s.name == "ensemble")
    assert ensemble.status == "skipped"
    assert ensemble.detail == {"reason": "disabled (GEFS_ENABLED=false)"}
    assert counts(db) == before  # no row added or duplicated
    # The map stage only checked the cycle (.idx HEADs), it downloaded no field.
    new = synthetic_map.world.requests[requests:]
    assert new and {r.method for r in new} == {"HEAD"}
    assert second.published_at is not None and second.published_at > first.published_at


def test_a_sync_that_finds_everything_stored_publishes_nothing(
    db: Session, synthetic_map: MapWorld
) -> None:
    """A runner whose known runs are stale (e.g. Redis lost them) plans the stored runs as
    new; the acquisitions find them stored and published already, so blend and publish do
    not run, the publication does not move, and the sync ends skipped."""
    first = orchestrator.run_sync(trigger="cli", now=NOW)
    assert first.status == "success" and first.published_at is not None
    plan = plan_sync(None, now=NOW, known=empty_known(NOW))
    assert plan.due and plan.due_kinds() == {"spots", "map"}
    second = orchestrator.run_sync(trigger="cli", now=NOW, plan=plan)
    outcomes = [
        c["outcome"]
        for s in second.stages
        if s.name in ("spots", "map")
        for c in s.detail["components"]
    ]
    assert outcomes == ["current", "current", "current"]  # demo wave, demo atmosphere, map
    assert stages(second)["blend"] == "skipped" and stages(second)["publish"] == "skipped"
    assert second.status == "skipped" and second.published_at is None
    assert second.sync_run_id is not None and second.sync_run_id != first.sync_run_id
    assert latest_publication(db).id == first.sync_run_id  # type: ignore[union-attr]


def test_force_turns_a_given_plan_into_a_forced_one(db: Session, synthetic_map: MapWorld) -> None:
    assert orchestrator.run_sync(trigger="cli", now=NOW).status == "success"
    plan = plan_sync(db, now=NOW)
    assert not plan.due and not plan.forced
    result = orchestrator.run_sync(trigger="cli", force=True, plan=plan, now=NOW)
    assert result.plan.forced and result.plan.due and "forced" in result.plan.reasons
    assert result.sync_run_id is not None and result.status == "success"
    assert stages(result)["publish"] == "success"


def test_the_lease_is_renewed_after_every_stage(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lease_expires_at moves SYNC_LEASE_MINUTES ahead after each stage, so a sync that
    takes longer than one lease (a slow map stage) keeps it."""
    from app.services.forecasts import ingestion
    from app.services.surf_quality import predict

    seen: dict[str, Any] = {}

    def lease(db: Session) -> Any:
        return db.scalar(select(SyncRun.lease_expires_at).where(SyncRun.status == "running"))

    # One provider: the spot stage is a component refresh (ingestion.refresh_source).
    real_acquire, real_predict = ingestion.refresh_source, predict.compute_pending_predictions

    def acquire(db: Session, *args: Any, **kw: Any) -> Any:
        seen["spots"] = lease(db)  # the lease as acquired: no stage has finished yet
        return real_acquire(db, *args, **kw)

    def scored(db: Session, now: Any = None) -> Any:
        seen["predict"] = lease(db)  # renewed after spots, ensemble, map and blend
        return real_predict(db, now)

    monkeypatch.setattr(ingestion, "refresh_source", acquire)
    monkeypatch.setattr(predict, "compute_pending_predictions", scored)
    started = utcnow()
    result = orchestrator.run_sync(trigger="cli", now=NOW)
    assert result.status == "success"
    lease_time = timedelta(minutes=get_settings().sync_lease_minutes)
    assert timedelta(0) <= seen["spots"] - (started + lease_time) < timedelta(minutes=1)
    assert seen["predict"] > seen["spots"]


def test_a_second_concurrent_sync_gets_sync_busy(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.surf_quality import predict

    entered, release = threading.Event(), threading.Event()
    real = predict.compute_pending_predictions

    def slow(db: Session, now: Any = None) -> Any:
        entered.set()
        assert release.wait(30)
        return real(db, now)

    monkeypatch.setattr(predict, "compute_pending_predictions", slow)
    out: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: out.setdefault("result", orchestrator.run_sync(trigger="daemon", now=NOW))
    )
    worker.start()
    try:
        assert entered.wait(60)
        with pytest.raises(SyncBusy) as busy:
            orchestrator.run_sync(trigger="cli", force=True, now=NOW)
        assert busy.value.holder and busy.value.lease_expires_at is not None
        with pytest.raises(SyncBusy):
            orchestrator.run_sync(trigger="cli", now=NOW)
        plan = plan_sync(db, now=NOW)
        assert not plan.due and any("is running" in r for r in plan.reasons)
    finally:
        release.set()
        worker.join(60)
    assert out["result"].status == "success"
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 1


class _Crash(BaseException):
    """The process dies (as with SIGKILL): nothing after this runs."""


def test_a_crashed_sync_is_abandoned_and_the_next_one_completes_it(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The process dies in the predict stage (with one provider the blend stage does not
    # run, Swell 2.0).
    from app.services.surf_quality import predict

    def crash(*_a: Any, **_k: Any) -> Any:
        raise _Crash()

    with monkeypatch.context() as m:
        m.setattr(predict, "compute_pending_predictions", crash)
        m.setattr(orchestrator._Lease, "release", lambda *a, **k: True)  # never reached
        m.setattr(orchestrator, "_close_log", lambda *a, **k: None)
        with pytest.raises(_Crash):
            orchestrator.run_sync(trigger="daemon", now=NOW)
    db.expire_all()
    crashed = db.scalar(select(SyncRun))
    assert crashed is not None and crashed.status == "running"
    assert [s["name"] for s in crashed.stages] == ["spots", "ensemble", "map", "blend"]
    assert crashed.published_at is None
    # While the lease is valid nobody else may sync.
    with pytest.raises(SyncBusy):
        orchestrator.run_sync(trigger="cli", force=True, now=NOW)
    # SYNC_LEASE_MINUTES later the lease has expired.
    db.execute(update(SyncRun).values(lease_expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    nxt = orchestrator.run_sync(trigger="daemon", now=NOW)
    db.expire_all()
    assert db.get(SyncRun, crashed.id).status == "abandoned"  # type: ignore[union-attr]
    assert "lease expired" in (db.get(SyncRun, crashed.id).error_message or "")  # type: ignore[union-attr]
    assert nxt.status == "success" and nxt.sync_run_id != crashed.id
    # The crashed sync had stored the runs; the next one found them stored and published.
    outcomes = [
        c["outcome"]
        for s in nxt.stages
        if s.name in ("spots", "map")
        for c in s.detail["components"]
    ]
    assert outcomes == ["current", "current", "current"]  # demo wave, demo atmosphere, map
    assert stages(nxt)["publish"] == "success" and stages(nxt)["blend"] == "skipped"
    assert stages(nxt)["predict"] == "success"
    assert published_map_run_id(db) is not None


def test_a_sync_that_cannot_open_its_job_log_releases_the_lease(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(row_id: int) -> int:
        raise RuntimeError("the database went away (test)")

    with monkeypatch.context() as m:
        m.setattr(orchestrator, "_open_log", gone)
        with pytest.raises(RuntimeError, match="went away"):
            orchestrator.run_sync(trigger="daemon", now=NOW)
    db.expire_all()
    row = db.scalar(select(SyncRun))
    assert row is not None and row.status == "failed" and row.lease_expires_at is None
    assert "went away" in (row.error_message or "")
    assert orchestrator.run_sync(trigger="daemon", now=NOW).status == "success"  # not busy


def test_a_sync_that_lost_its_lease_stops(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync stalled past SYNC_LEASE_MINUTES whose lease another runner took over stops at
    the next stage boundary and leaves the row to the new holder."""
    from app.services.surf_quality import predict

    real = predict.compute_pending_predictions

    def taken_over(db: Session, now: Any = None) -> Any:
        db.execute(update(SyncRun).values(status="abandoned", holder="other:1:x"))
        db.commit()
        return real(db, now)

    monkeypatch.setattr(predict, "compute_pending_predictions", taken_over)
    result = orchestrator.run_sync(trigger="daemon", now=NOW)
    assert result.status == "abandoned" and result.published_at is None
    assert [s.name for s in result.stages if s.detail.get("reason") != "lease lost"] == [
        "spots",
        "ensemble",
        "map",
        "blend",
        "predict",
    ]
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "abandoned" and row.holder == "other:1:x"
    log = db.scalar(select(BackgroundJobLog).where(BackgroundJobLog.job_name == "sync.run"))
    assert log is not None and log.status.value == "failed"
    assert "lease was lost" in (log.error_message or "")


def test_stop_ends_the_sync_after_the_current_stage(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.surf_quality import predict

    stop = threading.Event()
    real = predict.compute_pending_predictions

    def predict_then_sigterm(db: Session, now: Any = None) -> Any:
        out = real(db, now)
        stop.set()  # SIGTERM arrives during the predict stage
        return out

    monkeypatch.setattr(predict, "compute_pending_predictions", predict_then_sigterm)
    result = orchestrator.run_sync(trigger="daemon", now=NOW, stop=stop)
    assert result.status == "partial" and result.published_at is None
    done = ["spots", "ensemble", "map", "blend", "predict"]
    assert [s.name for s in result.stages if s.detail.get("reason") != "stopped"] == done
    assert all(s.status == "skipped" for s in result.stages[len(done) :])
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "partial" and row.lease_expires_at is None
    assert "stopped after stage predict" in (row.error_message or "")
    # The next sync publishes what the stopped one stored.
    monkeypatch.setattr(predict, "compute_pending_predictions", real)
    plan = plan_sync(db, now=NOW)
    assert plan.due  # stored after the newest publication (none): not stored yet
    nxt = orchestrator.run_sync(trigger="daemon", now=NOW, plan=plan)
    assert nxt.status == "success" and nxt.published_at is not None


def test_a_failed_component_makes_a_partial_sync_and_counts_an_attempt(
    db: Session, synthetic_map: MapWorld
) -> None:
    known = empty_known()
    plan = plan_sync(db, now=NOW, known=known)
    assert plan.due and plan.due_kinds() == frozenset({"spots", "map"})
    synthetic_map.fail_everything(503)  # NODD fails between the check and the sync
    result = orchestrator.run_sync(trigger="daemon", now=NOW, plan=plan)
    assert result.status == "partial"
    assert stages(result)["map"] == "failed" and stages(result)["publish"] == "success"
    map_stage = next(s for s in result.stages if s.name == "map")
    (component,) = map_stage.detail["components"]
    assert component["outcome"] == "failed" and "503" in component["message"]
    orchestrator.remember(known, result, now=NOW)
    assert known.attempts[f"noaa_gfs_fields|noaa-gfs-fields:{CYCLE:%Y%m%d%H}"][0] == 1
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and "noaa_gfs_fields" in (row.error_message or "")
    # The retry waits SYNC_RETRY_MINUTES (the map is back meanwhile).
    synthetic_map.world.fail.clear()
    soon = plan_sync(None, now=NOW + timedelta(minutes=5), known=known)
    assert not soon.due
    assert {c.source_code: c.state for c in soon.components}["noaa_gfs_fields"] == "waiting"
    assert plan_sync(None, now=NOW + timedelta(minutes=16), known=known).due


def test_a_failed_publication_fails_the_sync_and_publishes_nothing(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.sync import publish as publish_module

    def broken(db: Session, **kw: Any) -> Any:
        raise RuntimeError("the publication could not be committed (test)")

    monkeypatch.setattr(publish_module, "publish", broken)
    result = orchestrator.run_sync(trigger="cli", now=NOW)
    assert result.status == "failed" and result.published_at is None
    assert stages(result)["spots"] == "success" and stages(result)["publish"] == "failed"
    after = [s for s in result.stages if s.name in ("detect", "match", "flights", "alerts")]
    assert {s.status for s in after} == {"skipped"}
    assert {s.detail["reason"] for s in after} == {"nothing was published"}
    assert latest_publication(db) is None
    # The data stored is unpublished, so the next check plans it again.
    assert plan_sync(db, now=NOW + timedelta(minutes=10)).due


PREDICT_LOCK = f"lock:job:{STAGE_JOB_NAMES['predict']}"


def _predictions(db: Session) -> int:
    from app.models import SurfQualityPrediction

    db.expire_all()
    return int(db.scalar(select(func.count()).select_from(SurfQualityPrediction)) or 0)


def test_a_sync_whose_predict_could_not_run_publishes_nothing_and_retries(
    db: Session, synthetic_map: MapWorld
) -> None:
    """A daemon killed mid-predict leaves the job's lock for its TTL; the takeover sync's
    predict cannot run. It must not publish the unscored runs (the map would switch while
    the spot pages stay on the old run): the sync fails visibly, the runs stay unpublished,
    each acquired component counts a failed attempt, and the check after the retry wait
    syncs again and publishes the scored runs."""
    get_redis().set(PREDICT_LOCK, "killed-process", ex=1800)
    known = empty_known()
    plan = plan_sync(db, now=NOW, known=known)
    assert plan.due
    result = orchestrator.run_sync(trigger="daemon", now=NOW, plan=plan)
    assert result.status == "failed" and result.published_at is None
    predict_stage = next(s for s in result.stages if s.name == "predict")
    assert predict_stage.status == "failed"
    assert "another worker is running this job" in predict_stage.detail["error"]
    publish_stage = next(s for s in result.stages if s.name == "publish")
    assert publish_stage.status == "skipped"
    assert publish_stage.detail == {"reason": orchestrator.PUBLISH_HELD}
    assert {s.status for s in result.stages if s.name in orchestrator.AFTER_PUBLISH} == {"skipped"}
    components = [
        c for s in result.stages if s.name in ("spots", "map") for c in s.detail["components"]
    ]
    assert [(c["source_code"], c["component"], c["outcome"]) for c in components] == [
        ("demo", "wave", "failed"),
        ("demo", "atmosphere", "failed"),
        ("noaa_gfs_fields", None, "failed"),
    ]
    assert {c["message"] for c in components} == {orchestrator.HELD_MESSAGE}
    assert latest_publication(db) is None and _predictions(db) == 0
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "failed"
    assert "publication held back" in (row.error_message or "")
    assert row.stages[3 + 1]["status"] == "failed"  # sync_runs.stages: predict
    orchestrator.remember(known, result, now=NOW)
    for part in ("wave", "atmosphere"):  # one attempt per component (design D6)
        assert known.attempts[f"demo:{part}|demo:{CYCLE:%Y%m%d%H}"][0] == 1
    # Inside SYNC_RETRY_MINUTES nothing is due; after it the next check syncs again.
    soon = plan_sync(None, now=NOW + timedelta(minutes=5), known=known)
    assert not soon.due
    assert {c.source_code: c.state for c in soon.components}["demo"] == "waiting"
    get_redis().delete(PREDICT_LOCK)  # the lock's TTL ran out
    later = NOW + timedelta(minutes=16)
    retry = plan_sync(db, now=later, known=known)
    assert retry.due
    nxt = orchestrator.run_sync(trigger="daemon", now=NOW, plan=retry)
    assert nxt.status == "success" and nxt.published_at is not None
    assert stages(nxt)["predict"] == "success" and stages(nxt)["publish"] == "success"
    assert _predictions(db) > 0
    assert published_map_run_id(db) is not None
    orchestrator.remember(known, nxt, now=later)
    # (the demo provider's next run starts at 00Z, 20 minutes after NOW)
    assert not plan_sync(db, now=later + timedelta(minutes=2), known=known).due


def test_a_predict_that_fails_every_time_stops_after_the_attempt_limit(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministic predict error never publishes and does not re-sync at every check:
    after SYNC_MAX_ATTEMPTS failed attempts the run waits for the next one."""
    from app.services.surf_quality import predict

    def broken(db: Session, now: Any = None) -> Any:
        raise RuntimeError("scoring is broken (test)")

    monkeypatch.setattr(predict, "compute_pending_predictions", broken)
    monkeypatch.setattr(get_settings(), "sync_max_attempts", 2)
    # Retries 5 minutes apart: all within the demo run of NOW (its next run starts at 00Z).
    monkeypatch.setattr(get_settings(), "sync_retry_minutes", 5)
    known = empty_known()
    when = NOW
    for attempt in (1, 2):
        plan = plan_sync(db, now=when, known=known)
        assert plan.due, (attempt, plan.reasons)
        result = orchestrator.run_sync(trigger="daemon", now=NOW, plan=plan)
        assert result.status == "failed" and result.published_at is None
        assert stages(result)["predict"] == "failed" and stages(result)["publish"] == "skipped"
        orchestrator.remember(known, result, now=when)
        for part in ("wave", "atmosphere"):  # one attempt per component (design D6)
            assert known.attempts[f"demo:{part}|demo:{CYCLE:%Y%m%d%H}"][0] == attempt
        when += timedelta(minutes=6)
    final = plan_sync(db, now=when, known=known)
    assert not final.due
    demo = {c.source_code: c for c in final.components}["demo"]
    assert demo.state == "failed" and "SYNC_MAX_ATTEMPTS" in (demo.detail or "")
    assert latest_publication(db) is None


def test_a_forced_sync_publishes_even_when_predict_could_not_run(
    db: Session, synthetic_map: MapWorld
) -> None:
    """--force is the owner's override: the publication is not held back."""
    get_redis().set(PREDICT_LOCK, "killed-process", ex=1800)
    result = orchestrator.run_sync(trigger="cli", force=True, now=NOW)
    assert stages(result)["predict"] == "failed" and stages(result)["publish"] == "success"
    assert result.status == "partial" and result.published_at is not None


def test_a_stopped_sync_after_a_publication_is_published_by_the_next_check(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs stored by a sync that stopped (SIGTERM on a deploy) before publishing are newer
    than the newest publication, so they do not count as stored: the next check is due and
    publishes them (load_known's "completed before the newest publication")."""
    from app.services.surf_quality import predict
    from tests.synthetic_map_fields import add_map_hours

    first = orchestrator.run_sync(trigger="daemon", now=NOW)
    assert first.status == "success" and first.published_at is not None
    later = NOW + timedelta(hours=6)
    add_map_hours(synthetic_map.world, CYCLE + timedelta(hours=6), [0, 12, 24])
    plan = plan_sync(db, now=later)
    assert plan.due, plan.reasons
    stop = threading.Event()
    real = predict.compute_pending_predictions

    def predict_then_sigterm(db: Session, now: Any = None) -> Any:
        out = real(db, now)
        stop.set()
        return out

    monkeypatch.setattr(predict, "compute_pending_predictions", predict_then_sigterm)
    stopped = orchestrator.run_sync(trigger="daemon", now=later, stop=stop, plan=plan)
    assert stopped.status == "partial" and stopped.published_at is None
    new_keys = {
        c["run_key"]
        for s in stopped.stages
        if s.name in ("spots", "map")
        for c in s.detail["components"]
        if c["outcome"] == "stored"
    }
    assert new_keys, stopped.as_dict()
    monkeypatch.setattr(predict, "compute_pending_predictions", real)
    again = plan_sync(db, now=later)
    assert again.due, again.reasons
    nxt = orchestrator.run_sync(trigger="daemon", now=later, plan=again)
    assert nxt.status == "success" and nxt.published_at is not None
    assert nxt.published_at > first.published_at
    db.expire_all()
    stored_keys = set(db.scalars(select(ForecastRun.run_key)))
    assert new_keys <= stored_keys
    assert not plan_sync(db, now=later + timedelta(minutes=10)).due


def test_a_run_no_longer_served_at_acquisition_is_deferred_not_failed(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the check and the acquisition the provider stopped serving the run unmixed
    (Open-Meteo switching to its next run): the component is deferred, which is no
    failure and counts no attempt; the rest of the sync publishes."""
    from app.services.forecasts.demo import DemoForecastProvider
    from app.services.http import RunNotServedError

    known = empty_known()
    plan = plan_sync(db, now=NOW, known=known)
    assert plan.due and "spots" in plan.due_kinds()
    assert not hasattr(DemoForecastProvider, "ensure_servable")

    def not_served(self: Any, run: Any, now: Any = None) -> None:
        raise RunNotServedError("demo: the next run is being published")

    monkeypatch.setattr(DemoForecastProvider, "ensure_servable", not_served, raising=False)
    result = orchestrator.run_sync(trigger="daemon", now=NOW, plan=plan)
    assert result.status == "success", result.as_dict()
    spots = next(s for s in result.stages if s.name == "spots")
    parts = spots.detail["components"]
    assert [(c["component"], c["outcome"]) for c in parts] == [
        ("wave", "deferred"),
        ("atmosphere", "deferred"),
    ]
    assert all("being published" in c["message"] for c in parts)
    assert stages(result)["map"] == "success" and stages(result)["publish"] == "success"
    assert counts(db)[0] == 0  # no spot forecast stored
    orchestrator.remember(known, result, now=NOW)
    assert not [k for k in known.attempts if k.startswith("demo")]
    db.expire_all()
    row = db.get(SyncRun, result.sync_run_id)
    assert row is not None and row.status == "success" and row.error_message is None
    # "in progress" (another runner stores the same run) still counts as a failed attempt.
    assert orchestrator._outcome("skipped", "run in progress") == "failed"
    assert orchestrator._outcome("skipped", "fields of this cycle already stored") == "current"


def test_tick_syncs_when_due_and_then_not(db: Session, synthetic_map: MapWorld) -> None:
    out = orchestrator.tick(trigger="celery")
    # tick plans at the real time: the synthetic map's 18Z cycle of 2026-10-10 is not the
    # newest nominal cycle then, so only the demo spot run is new.
    assert out["due"] and out["status"] in ("success", "partial"), out
    assert out["sync_run_id"] is not None and out["published_at"] is not None
    known = state.read_known()
    assert known is not None and known.stored.get("demo")
    again = orchestrator.tick(trigger="celery")
    assert again["status"] == "not_due" and not again["due"]


# --------------------------------------------------------------------------- publish
def test_publish_without_a_sync_makes_a_published_row(db: Session) -> None:
    from app.services.sync.publish import publish

    row = publish(db, trigger="cli")
    assert row.id and row.status == "success" and row.trigger == "cli"
    assert row.published_at is not None and row.map_run_id is None and row.blend_run_id is None
    assert row.plan == {"published_by": "cli"}
    assert latest_publication(db) == row


def test_a_simulated_swell_run_never_counts_as_the_stored_run(
    db: Session, few_spots: list[str]
) -> None:
    """simulate-swell (a developer tool) stores a demo run keyed "demo-sim:...", and the dev
    endpoint's pipeline publishes it. The provider never offers that key, so it must not
    replace the real demo run as the stored one (or every check would plan a sync)."""
    from app.models import SurfSpot
    from app.services.forecasts.ingestion import simulate_swell
    from app.services.sync.detect import load_known
    from app.services.sync.publish import publish

    first = orchestrator.run_sync(trigger="cli")
    assert first.status == "success"
    real_key = load_known(db).stored["demo"]
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == few_spots[0]))
    assert spot is not None
    outcome = simulate_swell(db, spot, start=utcnow() + timedelta(days=7))
    assert outcome.status == "success" and (outcome.run_key or "").startswith("demo-sim:")
    publish(db, trigger="pipeline")
    assert load_known(db).stored["demo"] == real_key
    plan = plan_sync(db)
    assert not plan.due, plan.reasons


def test_publish_records_the_newest_scored_blend_run(db: Session) -> None:
    """blend_run_id: the newest usable blend run whose predictions are computed (a blend run
    still being scored, or a failed one, is not what the spot pages show)."""
    from app.models import ForecastSource
    from app.models.enums import ForecastRunStatus
    from app.services.sync.publish import publish

    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "blend"))
    if source is None:
        source = ForecastSource(code="blend", name="Blend (test)", provider="blend")
        db.add(source)
        db.flush()
    t0 = NOW - timedelta(hours=12)

    def blend_run(key: str, hours: int, status: ForecastRunStatus, scored: bool) -> ForecastRun:
        issued = t0 + timedelta(hours=hours)
        run = ForecastRun(
            source_id=source.id,
            run_key=key,
            issued_at=issued,
            started_at=issued,
            completed_at=issued,
            status=status,
            predictions_computed_at=issued if scored else None,
            details={},
        )
        db.add(run)
        db.flush()
        return run

    scored = blend_run("blend-test:1", 0, ForecastRunStatus.SUCCESS, scored=True)
    blend_run("blend-test:2", 6, ForecastRunStatus.SUCCESS, scored=False)  # not scored yet
    blend_run("blend-test:3", 12, ForecastRunStatus.FAILED, scored=True)
    db.commit()
    row = publish(db, trigger="cli")
    assert row.blend_run_id == scored.id


# --------------------------------------------------------------------------- commands
def test_sync_commands(
    db: Session, synthetic_map: MapWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sync", "check", "--json"]) == sync_cli.EXIT_OK
    plan = json.loads(capsys.readouterr().out)
    assert {c["source_code"] for c in plan["components"]} == {
        "demo",
        "noaa_gefs_wave",
        "noaa_gfs_fields",
    }
    assert cli.main(["sync", "check"]) == sync_cli.EXIT_OK
    text_out = capsys.readouterr().out
    assert "demo" in text_out and "noaa_gfs_fields" in text_out

    assert cli.main(["sync", "run", "--force", "--only", "spots"]) == sync_cli.EXIT_OK
    result = json.loads(capsys.readouterr().out)
    assert result["status"] in ("success", "partial")
    assert {s["name"]: s["status"] for s in result["stages"]}["map"] == "skipped"

    assert cli.main(["sync", "run", "--if-due"]) == sync_cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] in ("not_due", "success", "partial")

    assert cli.main(["sync", "publish"]) == sync_cli.EXIT_OK
    published = json.loads(capsys.readouterr().out)
    assert published["sync_run_id"] and published["published_at"]

    assert cli.main(["sync", "status", "--limit", "5"]) == sync_cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert [r["trigger"] for r in rows][:2] == ["cli", "cli"]
    assert {"id", "status", "stages", "published_at"} <= set(rows[0])


def test_sync_run_exits_1_when_nothing_could_be_stored(
    db: Session,
    few_spots: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.services.forecasts.demo import DemoForecastProvider

    def down(self: Any, spots: Any, run: Any) -> Any:
        raise RuntimeError("the demo provider is down (test)")

    monkeypatch.setattr(DemoForecastProvider, "fetch", down)
    assert cli.main(["sync", "run"]) == sync_cli.EXIT_FAILED == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed" and out["published_at"] is None
    spots = next(s for s in out["stages"] if s["name"] == "spots")
    # Both components of the demo source failed (design D6: one entry per component).
    assert [c["outcome"] for c in spots["detail"]["components"]] == ["failed", "failed"]


def test_sync_run_does_not_wait_for_a_retry_but_if_due_does(
    db: Session, few_spots: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """`sync run` acquires a component the rules hold back (here: a failed attempt a moment
    ago, SYNC_RETRY_MINUTES not over); `sync run --if-due` follows the rules and waits."""
    from app.services.forecasts.registry import build_provider

    key = build_provider("demo", get_settings()).current_run(utcnow()).run_key
    known = empty_known()
    for part in ("wave", "atmosphere"):  # attempts are per component (design D6)
        known.attempts[f"demo:{part}|{key}"] = (1, utcnow())
    state.write_known(known)
    assert cli.main(["sync", "run", "--if-due"]) == sync_cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "not_due"
    assert cli.main(["sync", "run"]) == sync_cli.EXIT_OK
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "success"
    spots = next(s for s in result["stages"] if s["name"] == "spots")
    assert [c["outcome"] for c in spots["detail"]["components"]] == ["stored", "stored"]
    after = state.read_known()
    assert after is not None and after.attempts == {}  # the stored run cleared the attempt


def test_sync_run_exits_3_while_another_sync_holds_the_lease(
    db: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    running_row(db, expires_in=timedelta(minutes=30))
    assert cli.main(["sync", "run", "--force"]) == sync_cli.EXIT_BUSY == 3
    assert json.loads(capsys.readouterr().out)["status"] == "busy"
    assert cli.main(["sync", "run"]) == sync_cli.EXIT_BUSY
    capsys.readouterr()
    assert cli.main(["sync", "run", "--if-due"]) == sync_cli.EXIT_OK  # not due: in progress
    assert json.loads(capsys.readouterr().out)["status"] == "not_due"
    db.execute(text("DELETE FROM sync_runs"))
    db.commit()
