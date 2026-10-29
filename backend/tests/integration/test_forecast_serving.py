"""The public reads serve the published forecast version (design D7, D10, D11; C3, C4).

Covered: the legacy run selection before any version exists (its freshness stale by the
run's own age rule); a version that maps every spot
to the run the legacy selection serves answers every route byte for byte as before (apart
from the additive ``freshness`` and ``version_id`` fields); a version wins over the legacy
selection; a stale spot keeps its previous run and is flagged with its last update (and its
timeline gets a new ETag, never a 304 for the old label); a
payload stored under a version's key holds that version's data; a publication (a new
version, a new map run) starts new snapshots and drops the old ones; a request whose ``?v=``
names another version is answered with no-store headers and stores nothing under its own
key; GET /api/forecasts/version reads no database statement while Redis holds the marker
and one on a miss (seeding Redis), and records the first-served time even after a Redis
error at the first answer; POST /api/forecasts/seen stores a latency sample for the
current version only; GET /api/forecasts/status; swell detection reads the served runs.

Every forecast run comes from the SYNTHETIC demo provider (``run_pipeline``); the versions
are staged by the tests themselves (versions.stage_version + publish_version): the version
the pipeline publishes is removed first (see ``two_runs``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import redis
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine, utcnow
from app.core.redis import get_redis, get_redis_bytes
from app.models import ForecastRun, ForecastVersion, SurfSpot, SwellEvent
from app.services import public_cache
from app.services.forecasts import serving, versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.queries import latest_runs_by_spot, served_runs_by_spot
from app.services.forecasts.versions import ComponentRun, SpotRun
from app.services.pipeline import run_pipeline
from app.services.swell_detection import service as detection_service
from app.services.swell_detection.service import detect_events
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

CDN = "public, s-maxage=300, stale-while-revalidate=3600"
BROWSER = "public, max-age=60"
ROUTES = [
    "/api/spots",
    "/api/map/conditions",
    "/api/spots/pipeline",
    "/api/spots/pipeline/forecast?days=10",
    "/api/spots/pipeline/timeline?days=10",
    "/api/spots/pipeline/events",
    "/api/events?limit=4&min_score=0",
    "/api/map/fields",
    "/api/system/status",
]


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def frozen(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    """The cache's clock, 25 minutes into the current hour."""
    clock = [public_cache.public_now() + timedelta(minutes=25)]
    monkeypatch.setattr(public_cache, "utcnow", lambda: clock[0])
    public_cache.clear()
    serving.clear_memo()
    versions.reset_fail_open()
    yield clock
    public_cache.clear()
    serving.clear_memo()


@pytest.fixture
def two_runs(db: Session, few_spots: list[str], frozen: list[datetime]) -> tuple[int, int]:
    """Two SYNTHETIC demo runs of the few-spots fixture, six hours apart (older, newer);
    the legacy selection serves the newer one."""
    run_pipeline(db, now=utcnow() - timedelta(hours=6), deliver=False)
    run_pipeline(db, deliver=False)
    runs = list(db.scalars(select(ForecastRun).order_by(ForecastRun.issued_at)))
    assert len(runs) == 2 and runs[0].issued_at < runs[1].issued_at
    older, newer = runs[0].id, runs[1].id
    assert {r.id for r in latest_runs_by_spot(db).values()} == {newer}
    # run_pipeline also publishes its run selection as a forecast version and writes the
    # version marker (P2 refresh). This module stages its own versions, so the runs start
    # without any version or marker, as in a database upgraded from schema 0008.
    assert versions.current_version(db) is not None
    db.execute(delete(ForecastVersion))
    db.commit()
    get_redis().delete(versions.marker_key())
    serving.clear_memo()
    assert versions.current_version(db) is None
    return older, newer


def spot_id(db: Session, slug: str) -> int:
    found = db.scalar(select(SurfSpot.id).where(SurfSpot.slug == slug))
    assert found is not None
    return found


def active_ids(db: Session) -> list[int]:
    return list(db.scalars(select(SurfSpot.id).where(SurfSpot.is_active).order_by(SurfSpot.id)))


def components(retrieved: datetime) -> list[ComponentRun]:
    """SYNTHETIC wind and wave components retrieved at ``retrieved``."""
    return [
        ComponentRun(
            component=c,
            model=f"demo-{c}",
            run_key=f"{c}:synthetic",
            init_time=retrieved - timedelta(hours=5),
            available_at=retrieved - timedelta(minutes=10),
            retrieved_at=retrieved,
            changed=True,
        )
        for c in (ATMOSPHERE, WAVE)
    ]


def publish(
    db: Session,
    runs: dict[int, int],
    *,
    updated_at: dict[int, datetime] | None = None,
    stale: frozenset[int] = frozenset(),
    retrieved: datetime | None = None,
) -> ForecastVersion:
    """Stage and publish a SYNTHETIC version serving ``runs`` (spot -> run id); spots in
    ``stale`` failed this refresh (they keep the previous version's run)."""
    retrieved = retrieved or utcnow()
    spot_runs = {
        sid: (
            SpotRun(None, versions.STALE, error="synthetic failure")
            if sid in stale
            else SpotRun(rid, versions.UPDATED, (updated_at or {}).get(sid, retrieved))
        )
        for sid, rid in runs.items()
    }
    version = versions.stage_version(
        db,
        provider="demo",
        source_code="demo",
        forecast_run_id=max(runs.values()),
        components=components(retrieved),
        spot_runs=spot_runs,
        acquired_at=retrieved,
    )
    return versions.publish_version(db, version)


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


def without_version_fields(value: Any) -> Any:
    """``value`` with the version ids of the additive freshness fields removed."""
    if isinstance(value, dict):
        out = {k: without_version_fields(v) for k, v in value.items() if k != "version_id"}
        return out
    if isinstance(value, list):
        return [without_version_fields(v) for v in value]
    return value


def snapshot_keys() -> set[bytes]:
    return {
        k
        for k in get_redis_bytes().scan_iter(f"{public_cache.PREFIX}:*")
        if not k.endswith((b":epoch", b":marker", b":forecast-version:v1"))
        and b":forecast-version:" not in k
    }


# --------------------------------------------------------------------------- legacy
def test_before_any_version_the_legacy_selection_is_served(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    _, newer = two_runs
    assert versions.current_version(db) is None
    served = {s: r.id for s, r in served_runs_by_spot(db).items()}
    assert served == {s: r.id for s, r in latest_runs_by_spot(db).items()}
    spots = app_client.get("/api/spots").json()
    assert spots and all(s["freshness"]["version_id"] is None for s in spots)
    assert all(s["freshness"]["stale"] is False for s in spots)
    assert all(s["freshness"]["last_updated_at"] is not None for s in spots)
    forecast = app_client.get("/api/spots/pipeline/forecast?days=10")
    assert forecast.headers["x-forecast-version"] == "0"
    run = db.get(ForecastRun, newer)
    assert run is not None and forecast.json()["run"]["run_key"] == run.run_key
    body = app_client.get("/api/forecasts/version").json()
    assert body["version_id"] is None and body["attribution"] == []


def test_before_any_version_an_old_forecast_is_flagged_stale(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int], frozen: list[datetime]
) -> None:
    """Before the first publication the freshness follows the served run's own staleness
    rule (``queries.is_stale``): a day past FORECAST_STALE_HOURS every spot is stale by age,
    with the run's last update; never labelled current."""
    _, newer = two_runs
    run = db.get(ForecastRun, newer)
    assert run is not None and run.completed_at is not None
    frozen[0] = run.issued_at + timedelta(hours=get_settings().forecast_stale_hours + 24)
    public_cache.clear()
    spots = app_client.get("/api/spots").json()
    assert spots
    for spot in spots:
        fresh = spot["freshness"]
        assert fresh["version_id"] is None and fresh["stale"] is True, spot["slug"]
        assert fresh["reason"] == "age", spot["slug"]
        assert datetime.fromisoformat(fresh["last_updated_at"]) == run.completed_at
    forecast = app_client.get("/api/spots/pipeline/forecast?days=10").json()
    assert forecast["freshness"]["stale"] is True and forecast["stale"] is True


def test_a_version_of_the_legacy_runs_answers_every_route_as_before(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    """The responses of 18a5bee's selection and of a version mapping every spot to the same
    runs are identical, apart from the additive version ids (freshness included: the
    version carries each run's own completion time)."""
    legacy_runs = latest_runs_by_spot(db)
    before = {url: app_client.get(url) for url in ROUTES}
    for url, resp in before.items():
        assert resp.status_code == 200, (url, resp.text)
    version = publish(
        db,
        {s: r.id for s, r in legacy_runs.items()},
        updated_at={s: r.completed_at for s, r in legacy_runs.items() if r.completed_at},
    )
    serving.refresh_version_marker(db)
    for url, old in before.items():
        new = app_client.get(url)
        assert new.status_code == 200, (url, new.text)
        assert new.headers["x-forecast-version"] == str(version.id), url
        assert without_version_fields(new.json()) == without_version_fields(old.json()), url
        if "/timeline" in url:
            assert new.headers["etag"] == old.headers["etag"]
    spots = app_client.get("/api/spots").json()
    assert {s["freshness"]["version_id"] for s in spots} == {version.id}
    conditions = app_client.get("/api/map/conditions").json()
    assert conditions["version_id"] == version.id


# --------------------------------------------------------------------------- serving
def test_the_version_wins_and_a_stale_spot_keeps_its_previous_run(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, newer = two_runs
    ids = active_ids(db)
    pipeline = spot_id(db, "pipeline")
    first = publish(db, dict.fromkeys(ids, older), retrieved=utcnow() - timedelta(hours=6))
    second = publish(db, dict.fromkeys(ids, newer), stale=frozenset({pipeline}))
    serving.refresh_version_marker(db)
    served = {s: r.id for s, r in served_runs_by_spot(db).items()}
    assert served == {**dict.fromkeys(ids, newer), pipeline: older}
    keys = {r.id: r.run_key for r in db.scalars(select(ForecastRun))}
    forecast = app_client.get("/api/spots/pipeline/forecast?days=10").json()
    assert forecast["run"]["run_key"] == keys[older]  # not the legacy selection's newer run
    fresh = forecast["freshness"]
    assert fresh["version_id"] == second.id and fresh["stale"] is True
    assert fresh["reason"] == "refresh_failed"
    entry = versions.spot_runs_of(first)[pipeline]
    assert entry.last_updated_at is not None
    assert datetime.fromisoformat(fresh["last_updated_at"]) == entry.last_updated_at
    timeline = app_client.get("/api/spots/pipeline/timeline?days=10").json()
    assert timeline["freshness"] == fresh
    other = app_client.get("/api/spots/uluwatu/forecast?days=10").json()
    assert other["run"]["run_key"] == keys[newer] and other["freshness"]["stale"] is False
    listed = {s["slug"]: s["freshness"] for s in app_client.get("/api/spots").json()}
    assert listed["pipeline"] == fresh and listed["uluwatu"]["stale"] is False
    marks = {
        s["slug"]: s["freshness"] for s in app_client.get("/api/map/conditions").json()["spots"]
    }
    assert marks["pipeline"]["stale"] is True and marks["nazare"]["stale"] is False
    status = app_client.get("/api/forecasts/status").json()
    assert status["version"]["version_id"] == second.id
    assert [s["slug"] for s in status["stale_spots"]] == ["pipeline"]
    assert status["stale_spots"][0]["reason"] == "refresh_failed"
    assert [c["component"] for c in status["components"]] == ["atmosphere", "wave"]
    assert all(c["model"] and c["model_run"] for c in status["components"])
    assert status["spots_served"] == len(ids)


def test_event_cards_carry_the_spots_freshness(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    """Review r2 FE-2 (spec 14): an event whose spot's refresh failed is not shown as
    current. Its run is not stale by age (``stale`` stays false), but the version marks the
    spot stale, and every event payload says so with the spot's ``freshness``."""
    _, newer = two_runs
    ids = active_ids(db)
    publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)
    events = app_client.get("/api/events?limit=50&min_score=0").json()
    assert events, "the SYNTHETIC demo runs detect at least one swell"
    assert all(e["freshness"]["stale"] is False for e in events)
    slug = events[0]["spot"]["slug"]
    second = publish(db, dict.fromkeys(ids, newer), stale=frozenset({spot_id(db, slug)}))
    serving.refresh_version_marker(db)

    def check(event: dict[str, Any]) -> None:
        fresh = event["freshness"]
        assert fresh["version_id"] == second.id
        assert fresh["stale"] is (event["spot"]["slug"] == slug), event["spot"]["slug"]
        if fresh["stale"]:
            assert fresh["reason"] == "refresh_failed" and fresh["last_updated_at"]
            assert event["stale"] is False  # the run itself is current

    listed = app_client.get("/api/events?limit=50&min_score=0").json()
    assert {e["spot"]["slug"] for e in listed} >= {slug}
    for e in listed:
        check(e)
    for e in app_client.get(f"/api/spots/{slug}/events").json():
        check(e)
    one = app_client.get(f"/api/events/{events[0]['id']}").json()
    check(one)
    summary = next(s for s in app_client.get("/api/spots").json() if s["slug"] == slug)
    if summary["next_event"] is not None:
        check(summary["next_event"])


def test_a_spot_whose_refresh_failed_gets_a_new_timeline_etag(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    """Same run, same steps, same ratings: once a version flags the spot stale (its latest
    refresh failed), a revalidation with the previous ETag gets the body with the stale
    freshness, not a 304 that would keep the old label."""
    _, newer = two_runs
    ids = active_ids(db)
    publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)
    url = "/api/spots/pipeline/timeline?days=10"
    before = app_client.get(url)
    assert before.status_code == 200 and before.json()["freshness"]["stale"] is False
    publish(db, dict.fromkeys(ids, newer), stale=frozenset({spot_id(db, "pipeline")}))
    serving.refresh_version_marker(db)
    after = app_client.get(url, headers={"If-None-Match": before.headers["etag"]})
    assert after.status_code == 200
    assert after.json()["freshness"]["stale"] is True
    assert after.json()["freshness"]["reason"] == "refresh_failed"
    assert after.json()["provenance"] == before.json()["provenance"]  # the same run
    assert after.headers["etag"] != before.headers["etag"]


def test_swell_detection_reads_the_served_runs(
    db: Session, two_runs: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detection (whose events feed the opportunities) reads the version's runs: every spot
    is evaluated on the older run the version serves, not the newer run the legacy
    selection would pick, and the events found from scratch name it."""
    older, _ = two_runs
    db.execute(delete(SwellEvent))  # the pipelines' own detections (setup)
    db.commit()
    publish(db, dict.fromkeys(active_ids(db), older))
    evaluated: dict[int, int] = {}
    real = detection_service.detect_for_spot

    def recording(db: Session, spot: SurfSpot, run: ForecastRun, *args: Any) -> Any:
        evaluated[spot.id] = run.id
        return real(db, spot, run, *args)

    monkeypatch.setattr(detection_service, "detect_for_spot", recording)
    stats = detect_events(db)
    assert stats.spots == len(active_ids(db))
    assert evaluated == dict.fromkeys(active_ids(db), older)
    assert set(db.scalars(select(SwellEvent.latest_run_id))) <= {older}


def test_a_payload_under_a_versions_key_holds_that_versions_data(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    """A version published in the database while Redis still names the previous one: the
    payloads keep serving the version their key names until the marker moves."""
    older, newer = two_runs
    ids = active_ids(db)
    keys = {r.id: r.run_key for r in db.scalars(select(ForecastRun))}
    publish(db, dict.fromkeys(ids, older))
    serving.refresh_version_marker(db)
    public_cache.note_published(1)
    url = "/api/spots/pipeline/forecast?days=10"
    assert app_client.get(url).json()["run"]["run_key"] == keys[older]
    second = publish(db, dict.fromkeys(ids, newer))  # the marker is not moved yet
    public_cache.clear_memo()
    get_redis_bytes().delete(*snapshot_keys())  # recomputed under the old version's key
    stale_key = app_client.get(url)
    assert stale_key.json()["run"]["run_key"] == keys[older]
    assert stale_key.headers["x-forecast-version"] != str(second.id)
    serving.refresh_version_marker(db)
    moved = app_client.get(url)
    assert moved.json()["run"]["run_key"] == keys[newer]
    assert moved.headers["x-forecast-version"] == str(second.id)


# --------------------------------------------------------------------------- invalidation
def test_a_publication_starts_new_snapshots_and_drops_the_old_ones(
    app_client: ApiClient,
    db: Session,
    two_runs: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older, newer = two_runs
    ids = active_ids(db)
    builds: list[int] = []
    real = public_cache.spot_summaries
    monkeypatch.setattr(
        public_cache, "spot_summaries", lambda db, now=None: builds.append(1) or real(db, now)
    )
    first = publish(db, dict.fromkeys(ids, older))
    serving.refresh_version_marker(db)
    public_cache.note_published(1)
    one = app_client.get("/api/spots")
    assert app_client.get("/api/spots").content == one.content
    assert len(builds) == 1
    old_generation = public_cache._memo.generation
    assert old_generation is not None and f".v{first.id}." in old_generation
    assert statements(lambda: app_client.get("/api/spots")) == []  # a hit: no database

    second = publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)  # the publisher's marker; no publication hook
    two = app_client.get("/api/spots")
    assert len(builds) == 2 and two.headers["x-forecast-version"] == str(second.id)
    assert {s["freshness"]["version_id"] for s in two.json()} == {second.id}
    assert not any(k.startswith(old_generation) for k in public_cache._memo.entries)
    assert app_client.get("/api/spots").content == two.content and len(builds) == 2

    # A new map run alone (the version marker names it) starts new snapshots too.
    marker = json.loads(get_redis().get(versions.marker_key()) or "{}")
    versions.write_version_marker({**marker, "map_run_id": 987654})
    app_client.get("/api/spots")
    assert len(builds) == 3
    # So does every publication hook (swell detection after the publication).
    public_cache.note_published(2)
    app_client.get("/api/spots")
    assert len(builds) == 4


def test_a_lost_version_marker_is_read_once_and_seeded(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, _ = two_runs
    version = publish(db, dict.fromkeys(active_ids(db), older))
    public_cache.note_published(1)  # the token, but no version marker in Redis
    app_client.get("/api/spots")
    seeded = get_redis().get(versions.marker_key())
    assert seeded is not None and json.loads(seeded)["version_id"] == version.id
    assert 0 < get_redis().ttl(versions.marker_key()) <= serving.SEEDED_MARKER_TTL_SECONDS
    public_cache.clear_memo()
    assert statements(lambda: app_client.get("/api/spots")) == []


def test_a_corrupt_version_marker_is_replaced(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, _ = two_runs
    version = publish(db, dict.fromkeys(active_ids(db), older))
    public_cache.note_published(1)
    get_redis().set(versions.marker_key(), '{"version_id": "not a number"}')
    app_client.get("/api/spots")
    stored = json.loads(get_redis().get(versions.marker_key()) or "{}")
    assert stored["version_id"] == version.id
    public_cache.clear_memo()
    assert statements(lambda: app_client.get("/api/spots")) == []


def test_a_seeded_marker_never_replaces_the_publishers(
    db: Session, two_runs: tuple[int, int]
) -> None:
    older, newer = two_runs
    publish(db, dict.fromkeys(active_ids(db), older))
    stale_read = serving.load_version_payload(db)  # read just before the next publication
    second = publish(db, dict.fromkeys(active_ids(db), newer))
    serving.refresh_version_marker(db)
    assert serving.seed_version_marker(stale_read) is False
    stored = json.loads(get_redis().get(versions.marker_key()) or "{}")
    assert stored["version_id"] == second.id
    assert get_redis().ttl(versions.marker_key()) == -1  # the publisher's marker: no expiry


# --------------------------------------------------------------------------- ?v=
def test_a_request_for_another_version_is_not_cached(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, newer = two_runs
    ids = active_ids(db)
    first = publish(db, dict.fromkeys(ids, older))
    second = publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)
    public_cache.note_published(1)
    for url in ROUTES:
        sep = "&" if "?" in url else "?"
        current = app_client.get(f"{url}{sep}v={second.id}")
        assert current.status_code == 200, (url, current.text)
        assert current.headers.get_list("cdn-cache-control") == [CDN], url
        assert current.headers.get_list("cache-control") == [BROWSER], url
        assert current.headers["x-forecast-version"] == str(second.id), url
        plain = app_client.get(url)
        assert plain.headers.get_list("cdn-cache-control") == [CDN], url
        keys = snapshot_keys()
        for wrong in (str(first.id), str(second.id + 1), "0", "garbage"):
            resp = app_client.get(f"{url}{sep}v={wrong}")
            assert resp.status_code == 200, (url, wrong)
            assert resp.headers.get_list("cache-control") == ["no-store"], (url, wrong)
            assert resp.headers.get_list("cdn-cache-control") == ["no-store"], (url, wrong)
            assert resp.content == current.content, (url, wrong)  # the current data
        public_cache.clear_memo()
        resp = app_client.get(f"{url}{sep}v={first.id}")  # from Redis, still no-store
        assert resp.headers.get_list("cdn-cache-control") == ["no-store"], url
        assert snapshot_keys() == keys, url  # nothing stored under another key
    timeline = app_client.get(f"/api/spots/pipeline/timeline?days=10&v={second.id}")
    not_modified = app_client.get(
        f"/api/spots/pipeline/timeline?days=10&v={first.id}",
        headers={"If-None-Match": timeline.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.headers.get_list("cdn-cache-control") == ["no-store"]


def test_before_any_version_only_the_legacy_values_are_shared(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    public_cache.note_published(1)
    for v in ("", "0", "null"):
        resp = app_client.get(f"/api/spots?v={v}")
        assert resp.headers.get_list("cdn-cache-control") == [CDN], v
    resp = app_client.get("/api/spots?v=1")
    assert resp.headers.get_list("cdn-cache-control") == ["no-store"]


def test_without_redis_a_versioned_request_is_not_cached(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    older, _ = two_runs
    version = publish(db, dict.fromkeys(active_ids(db), older))
    refusing = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    monkeypatch.setattr(public_cache, "get_redis_bytes", lambda: refusing)
    plain = app_client.get("/api/spots")
    assert plain.status_code == 200 and plain.headers.get_list("cdn-cache-control") == [CDN]
    assert "x-forecast-version" not in plain.headers
    assert {s["freshness"]["version_id"] for s in plain.json()} == {version.id}
    versioned = app_client.get(f"/api/spots?v={version.id}")
    assert versioned.headers.get_list("cdn-cache-control") == ["no-store"]
    assert versioned.content == plain.content


# --------------------------------------------------------------------------- endpoints
def test_the_version_endpoint_reads_redis_only_while_it_holds_the_marker(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, _ = two_runs
    version = publish(db, dict.fromkeys(active_ids(db), older))
    serving.refresh_version_marker(db)
    resp: list[Any] = []
    assert statements(lambda: resp.append(app_client.get("/api/forecasts/version"))) == []
    body = resp[0].json()
    assert body["version_id"] == version.id and body["provider"] == "demo"
    assert body["spots_total"] == len(active_ids(db)) and body["attribution"]
    assert resp[0].headers["cache-control"] == "no-cache"
    seconds = get_settings().forecast_version_cdn_seconds
    assert resp[0].headers["cdn-cache-control"] == (
        f"public, s-maxage={seconds}, stale-while-revalidate={seconds}"
    )
    assert versions.first_served_at(version.id) is not None

    get_redis().delete(versions.marker_key())
    serving.clear_memo()
    missed = statements(lambda: resp.append(app_client.get("/api/forecasts/version")))
    assert len(missed) == 1 and "forecast_versions" in missed[0], missed
    assert resp[1].json() == body
    assert get_redis().get(versions.marker_key()) is not None  # seeded
    serving.clear_memo()
    assert statements(lambda: app_client.get("/api/forecasts/version")) == []


def test_the_version_endpoint_answers_from_memory_for_a_few_seconds(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    older, _ = two_runs
    publish(db, dict.fromkeys(active_ids(db), older))
    serving.refresh_version_marker(db)
    app_client.get("/api/forecasts/version")
    asked: list[str] = []
    real = versions.read_version_marker
    monkeypatch.setattr(versions, "read_version_marker", lambda: asked.append("get") or real())
    for _ in range(5):
        assert app_client.get("/api/forecasts/version").status_code == 200
    assert asked == []  # within VERSION_MEMO_SECONDS: neither Redis nor the database


def test_without_redis_the_forecast_endpoints_still_answer(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis down: the version comes from one database statement per memo window (never an
    error for the visitor), a seen report is still 204 (and stored nowhere), the status is
    computed directly."""
    older, _ = two_runs
    version = publish(db, dict.fromkeys(active_ids(db), older))
    refusing = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    monkeypatch.setattr(versions, "get_redis", lambda: refusing)
    monkeypatch.setattr(public_cache, "get_redis_bytes", lambda: refusing)
    resp: list[Any] = []
    first = statements(lambda: resp.append(app_client.get("/api/forecasts/version")))
    assert resp[0].status_code == 200 and resp[0].json()["version_id"] == version.id
    assert len(first) == 1 and "forecast_versions" in first[0], first
    assert statements(lambda: resp.append(app_client.get("/api/forecasts/version"))) == []
    assert resp[1].json() == resp[0].json()
    seen = app_client.client.post("/api/forecasts/seen", json={"version_id": version.id})
    assert seen.status_code == 204
    status = app_client.get("/api/forecasts/status")
    assert status.status_code == 200 and status.json()["version"]["version_id"] == version.id
    assert "x-forecast-version" not in status.headers
    monkeypatch.setattr(versions, "get_redis", get_redis)
    versions.reset_fail_open()
    assert versions.seen_latencies() == []


def test_first_served_is_recorded_once_redis_answers(
    db: Session, frozen: list[datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis error at a version's first answer does not stop this process from recording
    the version's first-served time at a later answer, once Redis is back."""
    refusing = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    monkeypatch.setattr(versions, "get_redis", lambda: refusing)
    serving.note_served(41)
    monkeypatch.setattr(versions, "get_redis", get_redis)
    versions.reset_fail_open()
    assert versions.first_served_at(41) is None
    serving.note_served(41)
    first = versions.first_served_at(41)
    assert first is not None
    serving.note_served(41)  # noted now: the recorded time stays
    assert versions.first_served_at(41) == first


def test_the_seen_endpoint_stores_a_latency_sample_for_the_current_version_only(
    app_client: ApiClient, db: Session, two_runs: tuple[int, int]
) -> None:
    older, newer = two_runs
    ids = active_ids(db)
    first = publish(db, dict.fromkeys(ids, older))
    second = publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)
    raw = app_client.client  # no CSRF header: navigator.sendBeacon cannot send one
    assert raw.post("/api/forecasts/seen", json={"version_id": second.id}).status_code == 204
    samples = versions.seen_latencies()
    assert len(samples) == 1
    assert second.published_at is not None
    assert 0 <= samples[0] <= (utcnow() - second.published_at).total_seconds() + 1
    for body in (
        {"version_id": first.id},  # an old version
        {"version_id": second.id + 50},  # unknown
        {"version_id": "x"},
        [1, 2],
    ):
        assert raw.post("/api/forecasts/seen", json=body).status_code == 204, body
    assert raw.post("/api/forecasts/seen", content=b"{broken").status_code == 204
    assert raw.post("/api/forecasts/seen", content=b"x" * 4096).status_code == 204
    assert len(versions.seen_latencies()) == 1
    beacon = raw.post(
        "/api/forecasts/seen",
        content=json.dumps({"version_id": second.id}).encode(),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    assert beacon.status_code == 204
    assert len(versions.seen_latencies()) == 2
    assert beacon.headers["cache-control"] == "no-store"


def test_the_seen_endpoint_is_rate_limited_per_client(
    app_client: ApiClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import forecasts as forecasts_api

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    raw = app_client.client
    visitor = {"X-Forwarded-For": "198.51.100.21"}  # SYNTHETIC documentation addresses
    for _ in range(forecasts_api.SEEN_LIMIT_PER_HOUR):
        resp = raw.post("/api/forecasts/seen", json={"version_id": 1}, headers=visitor)
        assert resp.status_code == 204
    limited = raw.post("/api/forecasts/seen", json={"version_id": 1}, headers=visitor)
    assert limited.status_code == 429
    other = raw.post(
        "/api/forecasts/seen", json={"version_id": 1}, headers={"X-Forwarded-For": "198.51.100.22"}
    )
    assert other.status_code == 204


def test_the_status_is_computed_once_per_version(
    app_client: ApiClient,
    db: Session,
    two_runs: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older, newer = two_runs
    ids = active_ids(db)
    builds: list[int] = []
    real = serving.status_payload
    monkeypatch.setattr(
        serving, "status_payload", lambda db, now: builds.append(1) or real(db, now)
    )
    first = publish(db, dict.fromkeys(ids, older))
    serving.refresh_version_marker(db)
    public_cache.note_published(1)
    one = app_client.get("/api/forecasts/status")
    assert one.status_code == 200 and one.json()["version"]["version_id"] == first.id
    assert one.headers.get_list("cdn-cache-control") == [CDN]
    public_cache.clear_memo()
    assert statements(lambda: app_client.get("/api/forecasts/status")) == []
    assert builds == [1]
    second = publish(db, dict.fromkeys(ids, newer))
    serving.refresh_version_marker(db)
    two = app_client.get("/api/forecasts/status").json()
    assert two["version"]["version_id"] == second.id and builds == [1, 1]
    assert two["stale_spots"] == []
    assert datetime.fromisoformat(two["generated_at"]) == public_cache.public_now()
