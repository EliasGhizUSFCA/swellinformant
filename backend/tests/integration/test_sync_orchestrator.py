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
from app.core.database import utcnow
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
        "blend": "success",  # one model family: nothing to blend, skipped inside the job
        "predict": "success",
        "publish": "success",
        "detect": "success",
        "match": "success",
        "flights": "success",
        "alerts": "success",
        "deliver": "success",
        "prune": "skipped",  # SYNC_PRUNE_EACH_PASS=false
    }
    spots = next(s for s in result.stages if s.name == "spots")
    assert spots.detail["components"] == [
        {
            "source_code": "demo",
            "provider": "demo",
            "outcome": "stored",
            "message": "",
            "run_key": f"demo:{CYCLE:%Y%m%d%H}",
            "retry_after_s": None,
        }
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
    assert outcomes == ["current", "current"]
    assert counts(db) == before  # no row added or duplicated
    # The map stage only checked the cycle (.idx HEADs), it downloaded no field.
    new = synthetic_map.world.requests[requests:]
    assert new and {r.method for r in new} == {"HEAD"}
    assert second.published_at is not None and second.published_at > first.published_at


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
    from app.services.forecasts import blend

    def crash(*_a: Any, **_k: Any) -> Any:
        raise _Crash()

    with monkeypatch.context() as m:
        m.setattr(blend, "build_blend", crash)
        m.setattr(orchestrator._Lease, "release", lambda *a, **k: True)  # never reached
        m.setattr(orchestrator, "_close_log", lambda *a, **k: None)
        with pytest.raises(_Crash):
            orchestrator.run_sync(trigger="daemon", now=NOW)
    db.expire_all()
    crashed = db.scalar(select(SyncRun))
    assert crashed is not None and crashed.status == "running"
    assert [s["name"] for s in crashed.stages] == ["spots", "ensemble", "map"]
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
    assert outcomes == ["current", "current"]
    assert stages(nxt)["publish"] == "success" and stages(nxt)["blend"] == "success"
    assert published_map_run_id(db) is not None


def test_a_sync_that_lost_its_lease_stops(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync stalled past SYNC_LEASE_MINUTES whose lease another runner took over stops at
    the next stage boundary and leaves the row to the new holder."""
    from app.services.forecasts import blend

    real = blend.build_blend

    def taken_over(db: Session, now: Any = None) -> Any:
        db.execute(update(SyncRun).values(status="abandoned", holder="other:1:x"))
        db.commit()
        return real(db, now)

    monkeypatch.setattr(blend, "build_blend", taken_over)
    result = orchestrator.run_sync(trigger="daemon", now=NOW)
    assert result.status == "abandoned" and result.published_at is None
    assert [s.name for s in result.stages if s.detail.get("reason") != "lease lost"] == [
        "spots",
        "ensemble",
        "map",
        "blend",
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
    (component,) = spots.detail["components"]
    assert component["outcome"] == "deferred"
    assert "being published" in component["message"]
    assert stages(result)["map"] == "success" and stages(result)["publish"] == "success"
    assert counts(db)[0] == 0  # no spot forecast stored
    orchestrator.remember(known, result, now=NOW)
    assert not [k for k in known.attempts if k.startswith("demo|")]
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
