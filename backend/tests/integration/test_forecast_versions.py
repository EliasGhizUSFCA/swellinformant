"""app.services.forecasts.versions against PostgreSQL and Redis (contracts C2-C4): staging,
the one-transaction publication, at most one published version under concurrent publishes,
the refusal of a version without an updated spot, the runs served per spot (stale spots on
their previous run, a retry carrying the other spots), the legacy run selection before any
version exists, freshness from a stored version, the version payload and the Redis helpers
(round trip, capped latency list, fail open). Migration 0009's search columns are checked
at the end. Every forecast run below is SYNTHETIC (rows written by the test, no provider).
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.redis import get_redis
from app.models import ForecastRun, ForecastSource, ForecastVersion, SurfSpot
from app.models.enums import ForecastRunStatus
from app.services.forecasts import versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.queries import latest_runs_by_spot
from app.services.forecasts.versions import (
    ComponentRun,
    MapRunRef,
    SpotRun,
    VersionPublishError,
    current_version,
    fail_version,
    publish_version,
    serving_runs,
    spot_freshness,
    stage_version,
    version_payload,
)

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
CYCLE = NOW - timedelta(hours=6)


# --------------------------------------------------------------------------- helpers
def demo_source(db: Session) -> ForecastSource:
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "demo"))
    if source is None:
        source = ForecastSource(
            code="demo", name="DEMO synthetic forecast", provider="demo", is_demo=True
        )
        db.add(source)
        db.commit()
    return source


def make_run(
    db: Session,
    spot_ids: list[int],
    issued: datetime,
    key: str,
    *,
    predictions: bool = True,
    status: ForecastRunStatus = ForecastRunStatus.SUCCESS,
) -> ForecastRun:
    run = ForecastRun(
        source_id=demo_source(db).id,
        run_key=key,
        issued_at=issued,
        wave_model_run_at=issued,
        atmosphere_model_run_at=issued,
        started_at=issued + timedelta(hours=5),
        completed_at=issued + timedelta(hours=5, minutes=3),
        status=status,
        spot_count=len(spot_ids),
        details={"spot_ids": spot_ids},
        predictions_computed_at=issued + timedelta(hours=5, minutes=4) if predictions else None,
    )
    db.add(run)
    db.commit()
    return run


def spot_ids(db: Session, n: int) -> list[int]:
    return list(db.scalars(select(SurfSpot.id).order_by(SurfSpot.id).limit(n)))


def comps(retrieved: datetime, *, wave_changed: bool = True) -> list[ComponentRun]:
    return [
        ComponentRun(
            component=WAVE,
            model="demo-swell-generator",
            run_key=f"wave:{CYCLE:%Y%m%d%H}",
            init_time=CYCLE,
            available_at=CYCLE + timedelta(hours=5),
            retrieved_at=retrieved,
            changed=wave_changed,
        ),
        ComponentRun(
            component=ATMOSPHERE,
            model="demo-diurnal-wind",
            run_key=f"atmosphere:{CYCLE:%Y%m%d%H}",
            init_time=CYCLE,
            available_at=CYCLE + timedelta(hours=4, minutes=30),
            retrieved_at=retrieved,
            estimated=True,
        ),
    ]


def stage(
    db: Session, run: ForecastRun, spots: dict[int, SpotRun], **kw: object
) -> ForecastVersion:
    return stage_version(
        db,
        provider="demo",
        source_code="demo",
        forecast_run_id=run.id,
        components=kw.pop("components", comps(NOW)),
        spot_runs=spots,
        acquired_at=kw.pop("acquired_at", NOW),
        **kw,  # type: ignore[arg-type]
    )


def updated(*ids: int) -> dict[int, SpotRun]:
    return {i: SpotRun(None) for i in ids}


def statuses(*ids: int) -> list[str]:
    with SessionLocal() as fresh:
        return [fresh.get(ForecastVersion, i).status for i in ids]  # type: ignore[union-attr]


def published_count() -> int:
    with SessionLocal() as fresh:
        return int(
            fresh.scalar(
                select(func.count())
                .select_from(ForecastVersion)
                .where(ForecastVersion.status == "published")
            )
            or 0
        )


# --------------------------------------------------------------------------- staging
def test_staging_fills_counts_models_and_the_failed_spots_previous_run(db: Session) -> None:
    a, b, c, d = spot_ids(db, 4)
    old = make_run(db, [a, b, c], CYCLE - timedelta(hours=6), "demo:old")
    new = make_run(db, [a, b], CYCLE, "demo:new")
    version = stage(
        db,
        new,
        {
            **updated(a, b),
            c: SpotRun(None, "stale", error="HTTP 502 from https://x.test/?apikey=SECRET123"),
            d: SpotRun(None, "stale", error="timeout"),
        },
        sync_run_id=None,
        timings={"acquire_ms": 1200},
        detected_at=NOW - timedelta(minutes=9),
        scored_at=NOW + timedelta(minutes=1),
        now=NOW + timedelta(minutes=2),
    )
    assert version.status == "staging" and version.staged_at == NOW + timedelta(minutes=2)
    assert version.published_at is None and version.previous_version_id is None
    assert (version.spots_total, version.spots_updated, version.spots_stale) == (4, 2, 1)
    assert version.spots_failed == 2
    assert version.wind_model == "demo-diurnal-wind" and version.wind_model_run == CYCLE
    assert version.wind_run_estimated is True and version.wave_run_estimated is False
    assert version.wave_model == "demo-swell-generator" and version.wave_model_run == CYCLE
    # The newest availability of a changed component.
    assert version.upstream_available_at == CYCLE + timedelta(hours=5)
    assert version.timings == {"acquire_ms": 1200}
    runs = versions.spot_runs_of(version)
    assert runs[a] == SpotRun(new.id, "updated", NOW)
    # Before any version: the failed spot keeps the run the legacy selection served.
    assert runs[c].run_id == old.id and runs[c].status == "stale"
    assert runs[c].last_updated_at == old.completed_at
    assert "SECRET123" not in (runs[c].error or "") and "[REDACTED]" in (runs[c].error or "")
    # No run ever served spot d: missing, not stale.
    assert runs[d] == SpotRun(None, "missing", None, "timeout")
    stored = db.get(ForecastVersion, version.id)
    assert stored is not None and stored.components["wave"]["run_key"].startswith("wave:")


def test_an_updated_spot_needs_a_run() -> None:
    with SessionLocal() as session, pytest.raises(ValueError, match="names no forecast run"):
        stage_version(
            session,
            provider="demo",
            source_code="demo",
            forecast_run_id=None,
            components=[],
            spot_runs={1: SpotRun(None)},
        )


# --------------------------------------------------------------------------- publishing
def test_publishing_supersedes_the_previous_version(db: Session) -> None:
    a, b = spot_ids(db, 2)
    r1 = make_run(db, [a, b], CYCLE - timedelta(hours=6), "demo:1")
    r2 = make_run(db, [a, b], CYCLE, "demo:2")
    v1 = publish_version(db, stage(db, r1, updated(a, b)), now=NOW)
    assert v1.status == "published" and v1.published_at == NOW
    assert current_version(db) is not None and current_version(db).id == v1.id  # type: ignore[union-attr]
    v2 = stage(db, r2, updated(a, b))
    assert v2.previous_version_id == v1.id  # staged on top of the published version
    publish_version(db, v2, now=NOW + timedelta(minutes=5))
    db.expire_all()
    assert (v1.status, v1.superseded_at) == ("superseded", NOW + timedelta(minutes=5))
    assert (v2.status, v2.published_at) == ("published", NOW + timedelta(minutes=5))
    assert current_version(db).id == v2.id  # type: ignore[union-attr]
    assert published_count() == 1


def test_publishing_is_one_transaction(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = spot_ids(db, 2)
    v1 = publish_version(db, stage(db, make_run(db, [a, b], CYCLE, "demo:1"), updated(a, b)))
    v2 = stage(db, make_run(db, [a, b], NOW, "demo:2"), updated(a, b))
    real_commit = db.commit

    def crash() -> None:
        # Both rows were flushed (old superseded, new published) when the commit fails.
        raise RuntimeError("synthetic crash at commit")

    monkeypatch.setattr(db, "commit", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        publish_version(db, v2)
    monkeypatch.setattr(db, "commit", real_commit)
    # Nothing of the failed publication is visible: the old version is still the one.
    assert statuses(v1.id, v2.id) == ["published", "staging"]
    with SessionLocal() as fresh:
        old = fresh.get(ForecastVersion, v1.id)
        assert old is not None and old.superseded_at is None
    # The same staged version publishes once the database is reachable again.
    publish_version(db, v2)
    assert statuses(v1.id, v2.id) == ["superseded", "published"]


def test_a_version_without_an_updated_spot_is_never_published(db: Session) -> None:
    a, b = spot_ids(db, 2)
    v1 = publish_version(db, stage(db, make_run(db, [a, b], CYCLE, "demo:1"), updated(a, b)))
    failed_refresh = stage(
        db,
        make_run(db, [], NOW, "demo:2", status=ForecastRunStatus.FAILED),
        {a: SpotRun(None, "stale", error="HTTP 500"), b: SpotRun(None, "stale", error="HTTP 500")},
    )
    assert failed_refresh.spots_updated == 0 and failed_refresh.spots_stale == 2
    with pytest.raises(VersionPublishError, match="updates no spot"):
        publish_version(db, failed_refresh)
    assert statuses(v1.id, failed_refresh.id) == ["published", "staging"]
    fail_version(db, failed_refresh, "whole-component failure: wave (HTTP 500)")
    assert statuses(failed_refresh.id) == ["failed"]


def test_only_a_staged_and_newest_version_is_published(db: Session) -> None:
    a = spot_ids(db, 1)[0]
    run = make_run(db, [a], CYCLE, "demo:1")
    v1 = publish_version(db, stage(db, run, updated(a)))
    with pytest.raises(VersionPublishError, match="only a staged version"):
        publish_version(db, v1)
    v2 = stage(db, run, updated(a))
    v3 = stage(db, run, updated(a))
    publish_version(db, v3)
    # v2 was staged before v3: publishing it now would roll the website back.
    with pytest.raises(VersionPublishError, match="staged after"):
        publish_version(db, v2)
    assert statuses(v1.id, v2.id, v3.id) == ["superseded", "staging", "published"]
    # A version staged on v3 publishes; one staged on the superseded v1 never does (its
    # carried spots would undo v3).
    on_v1 = stage(db, run, updated(a), previous=v1)
    with pytest.raises(VersionPublishError, match="stage it again"):
        publish_version(db, on_v1)
    v4 = publish_version(db, stage(db, run, updated(a)))
    assert v4.previous_version_id == v3.id
    assert statuses(v3.id, on_v1.id, v4.id) == ["superseded", "staging", "published"]
    failed = fail_version(db, stage(db, run, updated(a)), "validation: 0.4 of hours valid")
    with pytest.raises(VersionPublishError, match="is failed"):
        publish_version(db, failed)
    with pytest.raises(ValueError, match="cannot fail"):
        fail_version(db, v3, "late error")


def _publish_in_thread(version_id: int, barrier: threading.Barrier, out: dict[int, str]) -> None:
    with SessionLocal() as session:
        version = session.get(ForecastVersion, version_id)
        assert version is not None
        barrier.wait(timeout=30)
        try:
            publish_version(session, version)
            out[version_id] = "published"
        except VersionPublishError as exc:
            out[version_id] = f"refused: {exc}"


@pytest.mark.parametrize("attempt", range(5))
def test_two_concurrent_publishes_leave_one_published_version(db: Session, attempt: int) -> None:
    a, b = spot_ids(db, 2)
    run = make_run(db, [a, b], CYCLE, f"demo:{attempt}")
    v1 = publish_version(db, stage(db, run, updated(a, b)))
    v2, v3 = stage(db, run, updated(a, b)), stage(db, run, updated(a, b))
    barrier = threading.Barrier(2)
    out: dict[int, str] = {}
    threads = [
        threading.Thread(target=_publish_in_thread, args=(v.id, barrier, out)) for v in (v2, v3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert set(out) == {v2.id, v3.id}, out
    assert published_count() == 1
    # Both were staged on v1: whichever publishes first supersedes it, and the other is
    # refused (v3 staged after v2, or v2 builds on a version no longer published).
    assert sorted(out.values())[0] == "published", out
    assert sum(1 for r in out.values() if r.startswith("refused")) == 1, out
    winner = next(i for i, r in out.items() if r == "published")
    loser = ({v2.id, v3.id} - {winner}).pop()
    assert statuses(v1.id, winner, loser) == ["superseded", "published", "staging"]


def test_the_database_allows_one_published_version(db: Session) -> None:
    a = spot_ids(db, 1)[0]
    run = make_run(db, [a], CYCLE, "demo:1")
    publish_version(db, stage(db, run, updated(a)))
    other = stage(db, run, updated(a))
    with pytest.raises(IntegrityError, match="uq_forecast_versions_published"):
        db.execute(
            text("UPDATE forecast_versions SET status = 'published' WHERE id = :id"),
            {"id": other.id},
        )
        db.flush()
    db.rollback()
    with pytest.raises(IntegrityError, match="ck_forecast_versions_status"):
        db.execute(
            text("UPDATE forecast_versions SET status = 'live' WHERE id = :id"), {"id": other.id}
        )
    db.rollback()


# --------------------------------------------------------------------------- serving
def test_stale_spots_are_served_from_their_previous_run(db: Session) -> None:
    a, b, c = spot_ids(db, 3)
    r1 = make_run(db, [a, b, c], CYCLE - timedelta(hours=6), "demo:1")
    v1 = publish_version(db, stage(db, r1, updated(a, b, c), acquired_at=NOW - timedelta(hours=6)))
    assert serving_runs(db) == {a: r1.id, b: r1.id, c: r1.id}
    # 2 of 3 spots succeed: the failed one keeps its run, marked stale.
    r2 = make_run(db, [a, b], CYCLE, "demo:2", status=ForecastRunStatus.PARTIAL)
    v2 = stage(db, r2, {**updated(a, b), c: SpotRun(None, "stale", error="masked cells")})
    publish_version(db, v2)
    assert serving_runs(db) == {a: r2.id, b: r2.id, c: r1.id}
    entry = versions.spot_runs_of(v2)[c]
    assert entry.status == "stale" and entry.last_updated_at == NOW - timedelta(hours=6)
    assert (v2.spots_updated, v2.spots_stale, v2.spots_failed) == (2, 1, 1)
    # The retry of the failed spot alone publishes a version that updates just that spot.
    r3 = make_run(db, [c], CYCLE, "demo:3")
    v3 = publish_version(db, stage(db, r3, updated(c)))
    assert serving_runs(db) == {a: r2.id, b: r2.id, c: r3.id}
    assert (v3.spots_total, v3.spots_updated, v3.spots_stale) == (3, 1, 0)
    assert v3.previous_version_id == v2.id
    # An explicit version is served as it was, whatever is current.
    assert serving_runs(db, v1) == {a: r1.id, b: r1.id, c: r1.id}


def test_without_a_version_the_legacy_selection_is_served(db: Session) -> None:
    a, b, c, d = spot_ids(db, 4)
    make_run(db, [a, b, c, d], NOW - timedelta(days=3), "demo:stale-old")
    full = make_run(db, [a, b, c], CYCLE - timedelta(hours=6), "demo:full")
    partial = make_run(db, [a], CYCLE, "demo:partial", status=ForecastRunStatus.PARTIAL)
    make_run(db, [b], CYCLE, "demo:unscored", predictions=False)
    make_run(db, [c], CYCLE, "demo:failed", status=ForecastRunStatus.FAILED)
    legacy = {spot: run.id for spot, run in latest_runs_by_spot(db, NOW).items()}
    assert current_version(db) is None
    assert serving_runs(db, now=NOW) == legacy
    # The selection is not trivial here: several runs serve, the unscored and failed ones not.
    assert legacy[a] == partial.id and legacy[b] == legacy[c] == full.id
    assert d in legacy and legacy[d] not in (full.id, partial.id)
    # Once a version is published, it alone decides.
    v = publish_version(db, stage(db, full, updated(a, b, c)))
    assert serving_runs(db, now=NOW) == {a: full.id, b: full.id, c: full.id, d: legacy[d]}
    assert versions.spot_runs_of(v)[d].status == "updated"  # carried from the legacy selection


def test_freshness_and_payload_of_a_stored_version(db: Session) -> None:
    a, b, c = spot_ids(db, 3)
    r1 = make_run(db, [a, b, c], CYCLE, "demo:1")
    v = publish_version(
        db,
        stage(
            db,
            r1,
            {**updated(a, b), c: SpotRun(None, "stale", error="HTTP 503")},
            components=comps(NOW - timedelta(hours=1)),
            acquired_at=NOW - timedelta(hours=1),
        ),
        now=NOW,
    )
    with SessionLocal() as fresh:
        stored = fresh.get(ForecastVersion, v.id)
        assert stored is not None
        out = spot_freshness(stored, NOW)
        assert out[a].stale is False and out[a].last_updated_at == NOW - timedelta(hours=1)
        # Spot c failed and keeps r1 (the legacy selection served it): stale, whatever its age.
        assert out[c].stale and out[c].reason == "refresh_failed"
        # 12 h 1 min later the updated spots are stale too (6 h interval + 360 min).
        later = spot_freshness(stored, NOW + timedelta(hours=12, minutes=1))
        assert later[a].stale and later[a].reason in ("atmosphere_age", "wave_age")
        payload = version_payload(stored, MapRunRef(run_id=r1.id, cycle=CYCLE))
    assert payload["version_id"] == v.id and payload["published_at"] == NOW.isoformat()
    assert payload["provider"] == "demo" and payload["wind_run_estimated"] is True
    assert (payload["spots_total"], payload["spots_updated"], payload["spots_stale"]) == (3, 2, 1)
    assert payload["map_run_id"] == r1.id and payload["map_cycle"] == CYCLE.isoformat()


# --------------------------------------------------------------------------- Redis
def test_the_version_marker_round_trips(db: Session) -> None:
    a = spot_ids(db, 1)[0]
    v = publish_version(db, stage(db, make_run(db, [a], CYCLE, "demo:1"), updated(a)), now=NOW)
    payload = version_payload(v)
    assert versions.read_version_marker() is None
    assert versions.write_version_marker(payload) is True
    assert versions.read_version_marker() == payload
    # A marker that is not a JSON object reads as none.
    get_redis().set(versions.marker_key(), "[1]")
    assert versions.read_version_marker() is None
    get_redis().set(versions.marker_key(), "{not json")
    assert versions.read_version_marker() is None


def test_first_served_and_seen_latencies(db: Session) -> None:
    a = spot_ids(db, 1)[0]
    v = publish_version(db, stage(db, make_run(db, [a], CYCLE, "demo:1"), updated(a)), now=NOW)
    assert versions.note_first_served(v.id, NOW + timedelta(seconds=4)) is True
    assert versions.note_first_served(v.id, NOW + timedelta(seconds=9)) is False  # SET NX
    assert versions.first_served_at(v.id) == NOW + timedelta(seconds=4)
    assert versions.first_served_at(v.id + 1) is None
    # Without a marker the id cannot be checked: ignored.
    assert versions.record_seen(v.id, NOW + timedelta(seconds=30)) is None
    versions.write_version_marker(version_payload(v))
    assert versions.record_seen(v.id, NOW + timedelta(seconds=30)) == 30.0
    assert versions.record_seen(v.id + 1, NOW + timedelta(seconds=31)) is None  # unknown id
    assert versions.record_seen(v.id, NOW - timedelta(seconds=5)) == 0.0  # clock skew
    assert versions.seen_latencies() == [0.0, 30.0]  # newest first
    for i in range(versions.SEEN_LATENCY_SAMPLES + 5):
        versions.record_seen(v.id, NOW + timedelta(seconds=i), published_at=NOW)
    latencies = versions.seen_latencies()
    assert len(latencies) == versions.SEEN_LATENCY_SAMPLES
    assert latencies[0] == float(versions.SEEN_LATENCY_SAMPLES + 4)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_with_redis_down_the_helpers_fail_open(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreachable = redis.Redis(
        host="127.0.0.1", port=_closed_port(), socket_connect_timeout=0.5, decode_responses=True
    )
    monkeypatch.setattr(versions, "get_redis", lambda: unreachable)
    calls: list[Callable[[], object]] = [
        lambda: versions.write_version_marker({"version_id": 1}),
        versions.read_version_marker,
        lambda: versions.note_first_served(1),
        lambda: versions.first_served_at(1),
        lambda: versions.record_seen(1, NOW, published_at=NOW),
        versions.seen_latencies,
    ]
    results = []
    for call in calls:
        versions.reset_fail_open()
        results.append(call())  # never raises
    versions.reset_fail_open()
    assert results == [False, None, False, None, None, []]
    # The database path keeps working without Redis.
    a = spot_ids(db, 1)[0]
    v = publish_version(db, stage(db, make_run(db, [a], CYCLE, "demo:1"), updated(a)))
    assert serving_runs(db) == {a: v.forecast_run_id}


# --------------------------------------------------------------------------- migration 0009
def test_searches_get_alert_lead_days_within_bounds(db: Session) -> None:
    from app.schemas.searches import SearchIn
    from app.services import accounts, searches
    from tests.conftest import search_payload

    user = accounts.register(db, "Lead Time Tester", "lead-times@example.com", "Barrels4Days!")
    search = searches.create_search(db, user, SearchIn.model_validate(search_payload()))
    db.refresh(search)
    assert (search.alert_lead_min_days, search.alert_lead_max_days) == (2, 10)
    for low, high in ((11, 10), (-1, 10), (2, 17)):
        with pytest.raises(IntegrityError, match="ck_saved_searches_alert_lead_days"):
            db.execute(
                text(
                    "UPDATE saved_searches SET alert_lead_min_days = :low,"
                    " alert_lead_max_days = :high WHERE id = :id"
                ),
                {"low": low, "high": high, "id": search.id},
            )
        db.rollback()
    db.execute(
        text("UPDATE saved_searches SET alert_lead_min_days = 0, alert_lead_max_days = 16"),
    )
    db.commit()
