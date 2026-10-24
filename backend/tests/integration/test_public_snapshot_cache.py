"""The public snapshot cache (app.services.public_cache) behind the heavy public GETs.

Forecasts, predictions and swell events come from the SYNTHETIC demo provider
(``run_pipeline`` over the few-spots fixture). Every cached body is compared byte for byte
with the body FastAPI's own serialization gives for the same data and hour.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.api import spots as spots_api
from app.core.config import get_settings
from app.core.database import SessionLocal, get_engine, utcnow
from app.core.redis import get_redis_bytes
from app.models import ForecastRun, SurfSpot, SwellEvent, SyncRun
from app.models.enums import ConfidenceLabel, QualityLabel, SwellEventStatus
from app.schemas.spots import EventOut, MapSnapshotOut, SpotDetail, SpotForecastOut, SpotSummary
from app.schemas.timeline import SpotTimelineOut
from app.services import public_cache
from app.services.pipeline import run_pipeline
from app.services.research.assess import seed_research
from app.services.seed import DEFAULT_DATA_DIR
from app.services.spot_views import (
    map_snapshot,
    spot_detail,
    spot_events,
    spot_forecast,
    spot_summaries,
    upcoming_events,
)
from app.services.timeline import spot_timeline
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

CDN = "public, s-maxage=300, stale-while-revalidate=3600"


@pytest.fixture
def frozen(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    """The cache's clock, 25 minutes into the current hour (settable through the list)."""
    clock = [public_cache.public_now() + timedelta(minutes=25)]
    monkeypatch.setattr(public_cache, "utcnow", lambda: clock[0])
    public_cache.clear()
    yield clock
    public_cache.clear()


@pytest.fixture
def published(db: Session, few_spots: list[str], frozen: list[datetime]) -> Session:
    """Published demo data and no stored marker: run_pipeline calls the publication hook
    (tested in test_free_tier_egress), which is undone here, so the tests below start, as
    they always did, from the database marker and store one themselves where they test it."""
    run_pipeline(db)
    get_redis_bytes().delete(public_cache.MARKER_KEY)
    return db


def _spot(db: Session, slug: str = "pipeline") -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def reference_body(model: Any, value: Any) -> bytes:
    """The body FastAPI itself sends for ``value`` under ``response_model=model``."""
    app = FastAPI()

    @app.get("/reference", response_model=model)
    def reference() -> Any:
        return value

    with TestClient(app) as client:
        return client.get("/reference").content


def expected_bodies(db: Session, at: str) -> dict[str, bytes]:
    """Every snapshot route's body computed directly for the start of the cache's hour."""
    hour = public_cache.public_now()
    spot = _spot(db)
    at_time = datetime.fromisoformat(at)
    return {
        "/api/spots": reference_body(list[SpotSummary], spot_summaries(db, hour)),
        "/api/map/conditions": reference_body(MapSnapshotOut, map_snapshot(db, None, hour)),
        f"/api/map/conditions?at={quote(at)}": reference_body(
            MapSnapshotOut, map_snapshot(db, at_time, hour)
        ),
        "/api/spots/pipeline": reference_body(SpotDetail, spot_detail(db, spot, hour)),
        "/api/spots/pipeline/forecast?days=10": reference_body(
            SpotForecastOut, spot_forecast(db, spot, 10, hour)
        ),
        "/api/spots/pipeline/timeline?days=10": reference_body(
            SpotTimelineOut, spot_timeline(db, spot, 10, hour)
        ),
        "/api/spots/pipeline/events": reference_body(list[EventOut], spot_events(db, spot, hour)),
        "/api/events?limit=4&min_score=0": reference_body(
            list[EventOut], upcoming_events(db, hour, limit=4, min_score=0)
        ),
    }


def _refusing_redis() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)


def test_every_route_serves_the_bytes_fastapi_would(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss, a Redis hit (new instance), a memo hit and a request without Redis all send
    the body FastAPI's response-model serialization gives for the same data and hour."""
    at = (public_cache.public_now() + timedelta(hours=7)).isoformat()
    expected = expected_bodies(published, at)
    for url, body in expected.items():
        miss = app_client.get(url)
        assert miss.status_code == 200, (url, miss.text)
        assert miss.headers["content-type"] == "application/json", url
        assert miss.content == body, url
        public_cache.clear_memo()
        assert app_client.get(url).content == body, url  # from Redis
        assert app_client.get(url).content == body, url  # from this process's memo
    monkeypatch.setattr(public_cache, "get_redis_bytes", _refusing_redis)
    public_cache.clear_memo()
    for url, body in expected.items():
        assert app_client.get(url).content == body, url  # computed directly, Redis down


def test_every_map_time_is_answered_from_its_utc_hours_snapshot(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?at= values the map never sends (not a whole UTC hour, another UTC offset, no offset,
    far from now) get the bytes of their UTC hour, clamped to the cached window, from that
    hour's snapshot: one key each, so a visitor varying the second, the offset or the date
    cannot make every request read the database (each one used to compute unstored)."""
    hour = public_cache.public_now()
    store = public_cache.get_redis_bytes()
    computed: list[datetime | None] = []
    real = public_cache.map_snapshot

    def counted(db: Session, at: datetime | None, now: datetime | None = None) -> MapSnapshotOut:
        computed.append(at)
        return real(db, at, now)

    monkeypatch.setattr(public_cache, "map_snapshot", counted)

    def keys() -> set[bytes]:
        return {k for k in store.scan_iter(f"{public_cache.PREFIX}:*") if b":map-conditions:" in k}

    step = hour + timedelta(hours=7)
    utc_plus_1 = timezone(timedelta(hours=1))
    earliest = hour + public_cache.MAP_AT_CACHED_WINDOW[0]
    latest = hour + public_cache.MAP_AT_CACHED_WINDOW[1]
    cases = [
        (step, step),
        (step + timedelta(minutes=30), step),
        (step + timedelta(seconds=1), step),
        (step + timedelta(seconds=2, microseconds=5), step),
        (step.astimezone(utc_plus_1), step),  # the same instant, not in UTC
        (step.replace(tzinfo=None), step),  # no offset: read as UTC
        (hour + timedelta(days=40), latest),
        (hour + timedelta(days=41, minutes=7), latest),
        (hour - timedelta(days=3), earliest),
        (hour - timedelta(days=30, seconds=9), earliest),
    ]
    for at, answered in cases:
        assert public_cache.map_time(at, hour) == answered, at
        before = keys()
        response = app_client.get(f"/api/map/conditions?at={quote(at.isoformat())}")
        assert response.status_code == 200, (at, response.text)
        assert response.headers.get_list("cdn-cache-control") == [CDN], at
        monkeypatch.setattr(public_cache, "map_snapshot", real)
        assert response.content == reference_body(
            MapSnapshotOut, map_snapshot(published, answered, hour)
        ), at
        # The answer the time itself gets, in UTC (map_snapshot answers whole hours and
        # clamps to the forecast range); requested_at names the hour answered.
        in_utc = at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)
        own = json.loads(reference_body(MapSnapshotOut, map_snapshot(published, in_utc, hour)))
        served = response.json()
        assert served["requested_at"] == answered.isoformat().replace("+00:00", "Z"), at
        assert {**served, "requested_at": None} == {**own, "requested_at": None}, at
        monkeypatch.setattr(public_cache, "map_snapshot", counted)
        assert len(keys() - before) <= 1, at
    # One computation per answered hour: the step, the window's two ends.
    assert computed == [step, latest, earliest]
    assert len(keys()) == 3


def test_a_visitors_cache_misses_are_rate_limited(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each request that misses the cache (and so reads the database) counts against its
    client IP; hits never count, other visitors and the sync's own snapshots are not
    limited, and the limit answers 429 (never cached by the CDN)."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "public_cache_miss_limit_per_hour", 3)
    # One window for the whole test (an hourly window could turn while it runs).
    monkeypatch.setattr(public_cache, "MISS_WINDOW_SECONDS", 10**9)
    hour = public_cache.public_now()
    visitor = {"X-Forwarded-For": "198.51.100.7"}  # SYNTHETIC documentation addresses
    other = {"X-Forwarded-For": "198.51.100.8"}

    def at(hours: int) -> str:
        return f"/api/map/conditions?at={quote((hour + timedelta(hours=hours)).isoformat())}"

    for hours in (1, 2, 3):
        assert app_client.get(at(hours), headers=visitor).status_code == 200
        assert app_client.get(at(hours), headers=visitor).status_code == 200  # a hit
    public_cache.clear_memo()
    assert app_client.get(at(2), headers=visitor).status_code == 200  # from Redis: a hit
    limited = app_client.get(at(4), headers=visitor)
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    assert "public" not in limited.headers.get("cache-control", "")
    assert "cdn-cache-control" not in limited.headers
    assert app_client.get("/api/spots/pipeline", headers=visitor).status_code == 429
    assert app_client.get(at(1), headers=visitor).status_code == 200  # hits still answer
    assert app_client.get(at(4), headers=other).status_code == 200
    assert app_client.get(at(4), headers=visitor).status_code == 200  # now a hit
    public_cache.note_published(9)  # new snapshots: the sync's prewarm is never limited
    assert public_cache.prewarm(published) is True
    assert app_client.get("/api/spots", headers=visitor).status_code == 200
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    assert app_client.get(at(5), headers=visitor).status_code == 200


def test_a_preview_deployment_never_shares_productions_snapshots(
    published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production (VERCEL_ENV production or unset: the API, the daemon) and a preview on the
    same Redis keep apart: the preview computes its own payloads, never reads production's
    marker, and production never serves what the preview computed."""
    monkeypatch.delenv("VERCEL_ENV", raising=False)
    store = public_cache.get_redis_bytes()

    def built(body: bytes) -> Callable[[datetime], public_cache.Snapshot]:
        return lambda now: public_cache.Snapshot(body)

    assert public_cache.note_published(3) is True
    prod = public_cache.snapshot(published, "test-namespace", "", built(b'"production"'))
    assert prod.body == b'"production"'
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_DEPLOYMENT_ID", "dpl_synthetic_preview")
    public_cache.clear_memo()
    assert store.get(public_cache.marker_key()) is None  # the daemon's marker is not seen
    preview = public_cache.snapshot(published, "test-namespace", "", built(b'"preview"'))
    assert preview.body == b'"preview"'
    keys = {k.decode() for k in store.scan_iter(f"{public_cache.PREFIX}:*test-namespace")}
    assert len(keys) == 2
    assert sum(k.startswith(f"{public_cache.PREFIX}:preview:") for k in keys) == 1
    monkeypatch.setenv("VERCEL_ENV", "production")
    public_cache.clear_memo()
    again = public_cache.snapshot(published, "test-namespace", "", built(b'"recomputed"'))
    assert again.body == b'"production"'  # from Redis, production's own


def test_the_stored_marker_does_not_expire(published: Session) -> None:
    """The marker is replaced at every publication; without an expiry a pause in the syncs
    (the daemon's CPU quota, unreachable map layers) does not send every request back to
    the database marker (the hour in each key still bounds every payload)."""
    store = public_cache.get_redis_bytes()
    assert public_cache.note_published(11) is True
    assert store.ttl(public_cache.MARKER_KEY) == -1
    first = store.get(public_cache.MARKER_KEY)
    assert public_cache.note_published(12) is True
    assert store.get(public_cache.MARKER_KEY) != first
    assert store.ttl(public_cache.MARKER_KEY) == -1


def test_a_key_is_computed_once(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime] = []
    real = public_cache.spot_summaries

    def counted(db: Session, now: datetime | None = None) -> list[SpotSummary]:
        calls.append(now or utcnow())
        return real(db, now)

    monkeypatch.setattr(public_cache, "spot_summaries", counted)
    first = app_client.get("/api/spots")
    public_cache.clear_memo()
    second = app_client.get("/api/spots")
    third = app_client.get("/api/spots")
    assert first.content == second.content == third.content
    assert calls == [public_cache.public_now()]  # once, for the start of the hour


def statements(fn: Callable[[], Any]) -> list[str]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(get_engine(), "before_cursor_execute", record)
    try:
        fn()
    finally:
        event.remove(get_engine(), "before_cursor_execute", record)
    return seen


def test_a_hit_reads_one_small_statement_or_none_after_a_publication_hook(
    app_client: ApiClient, published: Session
) -> None:
    """Without a stored marker a hit reads only the database marker; once the sync's hook
    has stored one, a hit reads nothing from the database (Neon can stay asleep)."""
    urls = ["/api/spots", "/api/map/conditions", "/api/spots/pipeline/forecast?days=10"]
    for url in urls:
        app_client.get(url)
    for url in urls:
        sql = statements(lambda u=url: app_client.get(u))
        assert len(sql) == 1 and "sync_runs" in sql[0], (url, sql)
    assert public_cache.note_published(42) is True
    for url in urls:
        app_client.get(url)  # computed afresh under the new marker
        assert statements(lambda u=url: app_client.get(u)) == [], url


def test_a_publication_starts_new_snapshots(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = public_cache.spot_summaries
    monkeypatch.setattr(
        public_cache, "spot_summaries", lambda db, now=None: calls.append(1) or real(db, now)
    )
    app_client.get("/api/spots")
    app_client.get("/api/spots")
    assert len(calls) == 1
    now = utcnow()  # a new publication: the database marker changes
    published.add(SyncRun(trigger="cli", status="success", started_at=now, published_at=now))
    published.commit()
    app_client.get("/api/spots")
    assert len(calls) == 2
    public_cache.note_published(7)  # the sync's hook: a stored marker wins
    app_client.get("/api/spots")
    app_client.get("/api/spots")
    assert len(calls) == 3
    public_cache.note_published(7)  # every call of the hook is a new marker
    app_client.get("/api/spots")
    assert len(calls) == 4


def _synthetic_event(db: Session, slug: str, start: datetime) -> SwellEvent:
    """A SYNTHETIC swell event at a spot outside the few-spots fixture (no overlap)."""
    event = SwellEvent(
        spot_id=_spot(db, slug).id,
        status=SwellEventStatus.ACTIVE,
        start_time=start,
        end_time=start + timedelta(hours=12),
        peak_time=start + timedelta(hours=6),
        peak_score=70,
        avg_score=65.0,
        peak_label=QualityLabel.GOOD,
        peak_breaking_height_min_ft=4.0,
        peak_breaking_height_max_ft=6.0,
        qualifying_hours=12,
        confidence=60,
        confidence_label=ConfidenceLabel.MODERATE,
        source_code="demo",
        is_demo=True,
    )
    db.add(event)
    db.commit()
    return event


def test_the_database_marker_follows_the_published_data(published: Session) -> None:
    """Without the sync's hook the marker changes with a publication and its finish, a run
    scored, a swell event detected or updated, a spot taken off the list and a research
    assessment: the data the snapshots show."""
    db = published
    seen = {public_cache.derived_marker(db)}

    def changed() -> bool:
        marker = public_cache.derived_marker(db)
        new = marker not in seen
        seen.add(marker)
        return new

    assert not changed()  # stable while nothing changes
    now = utcnow()
    row = SyncRun(trigger="cli", status="running", started_at=now, published_at=now)
    db.add(row)
    db.commit()
    assert changed()
    row.status, row.finished_at = "success", now + timedelta(seconds=30)
    db.commit()
    assert changed()
    db.execute(update(ForecastRun).values(predictions_computed_at=now + timedelta(seconds=1)))
    db.commit()
    assert changed()
    event = _synthetic_event(db, "thurso-east", now + timedelta(days=6))
    assert changed()
    event.last_updated_at = now + timedelta(minutes=5)
    db.commit()
    assert changed()
    db.execute(update(SurfSpot).where(SurfSpot.slug == "nazare").values(is_active=False))
    db.commit()
    assert changed()
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    assert changed()


def test_the_hour_bounds_every_snapshot(
    app_client: ApiClient, published: Session, frozen: list[datetime]
) -> None:
    public_cache.note_published(1)
    first = app_client.get("/api/spots/pipeline/timeline?days=10").json()
    frozen[0] = public_cache.public_now() + timedelta(minutes=59, seconds=59)  # same hour
    assert app_client.get("/api/spots/pipeline/timeline?days=10").json() == first
    frozen[0] = public_cache.public_now() + timedelta(hours=1)
    later = app_client.get("/api/spots/pipeline/timeline?days=10").json()
    assert first["generated_at"] != later["generated_at"]
    assert datetime.fromisoformat(later["generated_at"]) == public_cache.public_now()


def test_concurrent_misses_compute_once(
    published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[int] = []

    def slow(now: datetime) -> public_cache.Snapshot:
        builds.append(1)
        time.sleep(0.3)
        return public_cache.Snapshot(b'{"synthetic":true}')

    results: list[bytes] = []

    def request() -> None:
        with SessionLocal() as db:
            results.append(public_cache.snapshot(db, "test-route", "x", slow).body)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert builds == [1]
    assert results == [b'{"synthetic":true}'] * 4


def _hour_reads(seen: list[str]) -> int:
    """How many of ``seen`` read a spot's hours with their forecasts (spot_views.spot_hours:
    predictions joined to their forecasts for one run and spot, ordered by time)."""
    return sum(
        1
        for s in seen
        if "JOIN wave_forecasts" in s
        and "surf_quality_predictions.valid_time >=" in s
        and "ORDER BY surf_quality_predictions.valid_time" in s
    )


def test_a_spot_pages_forecast_and_timeline_are_computed_together(
    app_client: ApiClient, published: Session
) -> None:
    """A miss of either computes both from one read of the hours, and the other is then a
    hit; the bodies are those FastAPI gives for each route on its own."""
    public_cache.note_published(1)  # the sync's hook: a hit then reads no database marker
    expected = expected_bodies(published, public_cache.public_now().isoformat())
    forecast_url = "/api/spots/pipeline/forecast?days=10"
    timeline_url = "/api/spots/pipeline/timeline?days=10"
    responses: list[Any] = []
    seen = statements(lambda: responses.append(app_client.get(forecast_url)))
    assert _hour_reads(seen) == 1
    public_cache.clear_memo()  # another instance: from Redis
    assert statements(lambda: responses.append(app_client.get(timeline_url))) == []
    assert [r.content for r in responses] == [expected[forecast_url], expected[timeline_url]]
    # The other way round, after a new epoch.
    public_cache.clear()
    seen = statements(lambda: app_client.get(timeline_url))
    assert _hour_reads(seen) == 1
    assert statements(lambda: app_client.get(forecast_url)) == []


def test_side_by_side_spot_page_requests_read_the_hours_once(published: Session) -> None:
    from app.api.spots import spot_page

    seen: list[str] = []
    lock = threading.Lock()

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        with lock:
            seen.append(statement)

    bodies: dict[str, bytes] = {}

    def request(route: str) -> None:
        with SessionLocal() as db:
            bodies[route] = spot_page(db, "pipeline", 10).snapshot(db, route).body

    event.listen(get_engine(), "before_cursor_execute", record)
    try:
        threads = [
            threading.Thread(target=request, args=(route,))
            for route in ("spot-forecast", "spot-timeline")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        event.remove(get_engine(), "before_cursor_execute", record)
    assert _hour_reads(seen) == 1
    expected = expected_bodies(published, public_cache.public_now().isoformat())
    assert bodies["spot-forecast"] == expected["/api/spots/pipeline/forecast?days=10"]
    assert bodies["spot-timeline"] == expected["/api/spots/pipeline/timeline?days=10"]


def test_a_failing_companion_is_left_to_its_own_request(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = spots_api.build_timeline

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(spots_api, "build_timeline", failing)
    expected = expected_bodies(published, public_cache.public_now().isoformat())
    forecast = app_client.get("/api/spots/pipeline/forecast?days=10")
    assert forecast.status_code == 200
    assert forecast.content == expected["/api/spots/pipeline/forecast?days=10"]
    monkeypatch.setattr(spots_api, "build_timeline", real)
    timeline = app_client.get("/api/spots/pipeline/timeline?days=10")  # not stored: computed
    assert timeline.content == expected["/api/spots/pipeline/timeline?days=10"]


def test_a_failed_computation_frees_its_waiters_at_once(published: Session) -> None:
    """A holder whose computation fails (a 404) releases the lock without a result: a
    waiter sees the lock gone at its first poll and computes, instead of waiting out
    WAIT_PAUSES_SECONDS."""
    started = threading.Event()
    outcome: list[str] = []

    def failing(now: datetime) -> public_cache.Snapshot:
        started.set()
        time.sleep(0.2)
        raise LookupError("no such spot")

    def holder() -> None:
        with SessionLocal() as db:
            try:
                public_cache.snapshot(db, "test-fail", "x", failing)
            except LookupError:
                outcome.append("holder failed")

    thread = threading.Thread(target=holder)
    thread.start()
    assert started.wait(5)
    began = time.monotonic()
    with SessionLocal() as db:
        body = public_cache.snapshot(
            db, "test-fail", "x", lambda now: public_cache.Snapshot(b"[]")
        ).body
    thread.join()
    assert body == b"[]" and outcome == ["holder failed"]
    assert time.monotonic() - began < 1.5  # well under the 6.35 s of pauses


def test_prewarm_computes_the_first_page_snapshots(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_cache.note_published(3)
    with SessionLocal() as db:
        assert public_cache.prewarm(db) is True
    public_cache.clear_memo()
    for url in ("/api/spots", "/api/map/conditions"):
        assert statements(lambda u=url: app_client.get(u)) == [], url  # stored by prewarm


def test_prewarm_without_redis_reads_nothing(
    published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshots that cannot be stored are not worth their reads during a sync."""
    refusing = _refusing_redis()
    monkeypatch.setattr(public_cache, "get_redis_bytes", lambda: refusing)
    with SessionLocal() as db:
        assert statements(lambda: public_cache.prewarm(db)) == []
        assert public_cache._fail_open is not None  # and Redis then rests
        assert statements(lambda: public_cache.prewarm(db)) == []


def test_without_redis_every_request_computes_and_redis_rests(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[redis.Redis] = []

    def refusing() -> redis.Redis:
        if not clients:
            clients.append(_refusing_redis())
        return clients[0]

    calls: list[int] = []
    real = public_cache.spot_summaries
    monkeypatch.setattr(
        public_cache, "spot_summaries", lambda db, now=None: calls.append(1) or real(db, now)
    )
    monkeypatch.setattr(public_cache, "get_redis_bytes", refusing)
    assert app_client.get("/api/spots").status_code == 200
    assert public_cache._fail_open is not None and public_cache._fail_open[0] is clients[0]
    asked: list[str] = []
    monkeypatch.setattr(clients[0], "mget", lambda *a, **k: asked.append("mget"))
    assert app_client.get("/api/spots").status_code == 200
    assert asked == []  # within FAIL_OPEN_SECONDS Redis is not asked again
    assert len(calls) == 2


def test_errors_are_not_cached(app_client: ApiClient, published: Session) -> None:
    for url in ("/api/spots/no-such-spot", "/api/spots/no-such-spot/forecast"):
        for _ in range(2):
            resp = app_client.get(url)
            assert resp.status_code == 404, url
            assert resp.headers.get_list("cache-control") == ["no-store"], url
            assert "cdn-cache-control" not in resp.headers


def test_the_timeline_etag_and_304_come_from_the_snapshot(
    app_client: ApiClient, published: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[int] = []
    real = spots_api.build_timeline
    monkeypatch.setattr(
        spots_api, "build_timeline", lambda *a, **k: builds.append(1) or real(*a, **k)
    )
    first = app_client.get("/api/spots/pipeline/timeline?days=10")
    again = app_client.get(
        "/api/spots/pipeline/timeline?days=10", headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304 and again.headers["etag"] == first.headers["etag"]
    assert again.headers["cdn-cache-control"] == CDN
    assert builds == [1]


def test_every_public_get_and_the_index_share_the_cdn_windows(
    app_client: ApiClient, published: Session
) -> None:
    """After a publication the edge switches the spot list, the conditions, the spot pages
    and the map-field index together (about 5 minutes), not some of them 10 times later."""
    urls = [
        "/api/spots",
        "/api/map/conditions",
        "/api/map/fields",
        "/api/events",
        "/api/spots/pipeline",
        "/api/spots/pipeline/forecast",
        "/api/spots/pipeline/timeline",
        "/api/spots/pipeline/events",
        "/api/spots/pipeline/tides",
        "/api/spots/pipeline/observations",
    ]
    for url in urls:
        resp = app_client.get(url)
        assert resp.status_code == 200, (url, resp.text)
        assert resp.headers.get_list("cdn-cache-control") == [CDN], url
        assert resp.headers.get_list("cache-control") == ["public, max-age=60"], url


def test_a_flushed_redis_starts_a_new_epoch(published: Session) -> None:
    """The memo cannot hand out a payload stored under an earlier epoch (a Redis that lost
    its data), even when the marker and the hour are the same."""
    with SessionLocal() as db:
        one = public_cache.snapshot(
            db, "test-epoch", "", lambda now: public_cache.Snapshot(b"[1]")
        ).body
        public_cache.get_redis_bytes().flushdb()
        two = public_cache.snapshot(
            db, "test-epoch", "", lambda now: public_cache.Snapshot(b"[2]")
        ).body
    assert (one, two) == (b"[1]", b"[2]")


def test_snapshots_round_trip_with_their_etag() -> None:
    snap = public_cache.Snapshot(b'{"a":"\\n\xc3\xa9"}\n{"raw newline":1}', etag='W/"abc"')
    assert public_cache.decode(public_cache.encode(snap)) == snap
    assert public_cache.decode(public_cache.encode(public_cache.Snapshot(b""))).etag is None


def test_query_values_are_hashed_in_keys() -> None:
    hour = datetime(2026, 10, 16, 6, tzinfo=public_cache.public_now().tzinfo)
    key = public_cache.cache_key("e", "m", hour, "map-conditions", "x" * 500 + "\n:*")
    assert key.startswith("public:v1:e:m:20261016T06:map-conditions:")
    assert len(key) < 80 and "\n" not in key and "*" not in key
