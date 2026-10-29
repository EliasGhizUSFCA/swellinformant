"""Spots, forecasts, events, reference data, system status and opportunity endpoints."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.api.system import JOB_NAMES
from app.core.config import Settings
from app.services.pipeline import run_pipeline
from app.services.surf_quality.scoring import ALGORITHM_VERSION
from app.workers.schedule import build_beat_schedule
from tests.conftest import ApiClient, search_payload
from tests.integration.test_public_cache_headers import PUBLIC_GET_CACHE_CONTROL

pytestmark = pytest.mark.integration


def test_spot_catalogue(app_client: ApiClient) -> None:
    spots = app_client.get("/api/spots").json()
    assert len(spots) == 50
    slugs = {s["slug"] for s in spots}
    assert {"pipeline", "jeffreys-bay", "nazare", "thurso-east"} <= slugs
    detail = app_client.get("/api/spots/jeffreys-bay").json()
    assert detail["airports"][0]["airport"]["iata"] == "PLZ" and detail["airports"][0]["is_primary"]
    assert detail["calibrated"] is False
    assert app_client.get("/api/spots/nowhere").status_code == 404


def test_forecast_and_events_after_pipeline(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    empty = app_client.get("/api/spots/pipeline/forecast").json()
    assert empty["available"] is False
    run_pipeline(db)
    fc = app_client.get("/api/spots/pipeline/forecast?days=5").json()
    assert fc["available"] and fc["source"]["is_demo"] and fc["stale"] is False
    assert fc["breaking_height_method"] == "linear_wave_v2"  # the method the predictions used
    assert fc["algorithm_version"] == ALGORITHM_VERSION == "2.2.0"
    assert fc["points"][0]["uncertainty_kind"] == "heuristic"
    assert fc["points"][0]["breaking_p10_ft"] < fc["points"][0]["breaking_p90_ft"]
    assert [m["role"] for m in fc["models"]] == ["authoritative"]
    p0 = fc["points"][0]
    assert {
        "sig_wave_height_m",
        "breaking_height_min_ft",
        "score",
        "confidence",
        "explanation",
    } <= set(p0)
    assert fc["daily"] and fc["run"]["run_key"].startswith("demo:")
    spots = {s["slug"]: s for s in app_client.get("/api/spots").json()}
    assert (
        spots["pipeline"]["current"] is not None and spots["pipeline"]["best_upcoming"] is not None
    )
    events = app_client.get("/api/events?limit=50").json()
    assert events and all(e["status"] == "active" for e in events)
    one = app_client.get(f"/api/events/{events[0]['id']}").json()
    assert one["history"]
    assert isinstance(app_client.get("/api/spots/pipeline/events").json(), list)


def test_reference_data(app_client: ApiClient) -> None:
    exact = app_client.get("/api/airports?q=SFO").json()
    assert exact[0]["iata"] == "SFO"
    by_city = app_client.get("/api/airports?q=lisbon").json()
    assert any(a["iata"] == "LIS" for a in by_city)
    regions = app_client.get("/api/regions").json()
    assert sum(r["spot_count"] for r in regions) == 50
    assert any(r["region_group"] == "Indonesia" for r in regions)


def test_system_status(app_client: ApiClient) -> None:
    status = app_client.get("/api/system/status").json()
    assert status["mode"]["forecast_providers"] == ["demo"]
    # DETECTION_LEAD_MIN_DAYS: 1 since Swell 2.0 (design B; it was 5): each saved search
    # narrows the window with its own alert lead times.
    assert status["detection"]["lead_min_days"] == 1.0
    assert {j["job"] for j in status["jobs"]} >= {"forecasts.acquire", "notifications.deliver"}
    # The status page's fallback job list names every scheduled task, with every optional
    # job switched on (tides.acquire and forecasts.map_fields were once missing).
    every = Settings(
        map_fields_enabled=True,
        gefs_enabled=True,
        tide_providers="noaa_coops",
        observations_enabled=True,
        verification_enabled=True,
    )
    scheduled = {entry["task"] for entry in build_beat_schedule(every).values()}
    assert scheduled <= set(JOB_NAMES), scheduled - set(JOB_NAMES)
    assert {"tides.acquire", "forecasts.map_fields"} <= {j["job"] for j in status["jobs"]}
    ready = app_client.get("/api/health/ready").json()
    assert ready["checks"] == {"database": "ok", "redis": "ok"}


def test_opportunity_endpoints_and_ownership(
    make_client, db: Session, few_spots: list[str], only_simulated_swells: None
) -> None:  # type: ignore[no-untyped-def]
    # Without natural demo swells (tests/conftest.py): with them the simulated swell merged
    # into a natural one on some dates and no opportunity was created (seen in the review's
    # clock-shift sweep between +43 and +55 days).
    owner: ApiClient = make_client()
    owner.register("owner@example.com")
    assert owner.post("/api/searches", search_payload()).status_code == 201
    run_pipeline(db)
    sim = owner.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    opps = owner.get("/api/opportunities").json()
    assert opps, "expected at least one opportunity"
    top = opps[0]
    assert top["status"] == "flight_found" and top["is_demo"] and top["has_mock_flights"]
    # Forecast confidence comes from the real API (not only from a mock): the event's peak
    # hour, exactly as EventOut shows it. Spec 20 (2.0): the combined "local confidence"
    # (research and forecast mixed) is served by neither.
    event = owner.get(f"/api/events/{top['event_id']}").json()
    assert top["forecast_confidence_class"] == event["forecast_confidence_class"] == "low"
    for body in (top, event):
        assert "local_confidence_class" not in body and "local_confidence_reason" not in body
    assert top["stale"] is event["stale"] is False
    detail = owner.get(f"/api/opportunities/{top['id']}").json()
    assert detail["forecast_confidence_class"] == detail["event"]["forecast_confidence_class"]
    assert detail["offers"] and detail["offers"][0]["is_mock"]
    assert detail["offers"][0]["outbound"]["segments"]
    assert any(link["label"] == "Google Flights" for link in detail["offers"][0]["links"])
    assert detail["travel_window"] and detail["daily"] and detail["limitations"]
    assert any("MOCK FARES" in n for n in detail["limitations"])
    # The method the predictions used (demo forecasts are scored with linear wave theory),
    # and the low-confidence note from the forecast class (demo data is always Low).
    assert any("linear wave theory" in n for n in detail["limitations"])
    assert not any("Komar" in n for n in detail["limitations"])
    assert any("Forecast confidence is low" in n for n in detail["limitations"])
    assert owner.get(f"/api/spots/{top['spot']['slug']}/opportunities").json()

    stranger: ApiClient = make_client()
    stranger.register("stranger@example.com")
    assert stranger.get(f"/api/opportunities/{top['id']}").status_code == 404
    assert stranger.post(f"/api/opportunities/{top['id']}/dismiss").status_code == 404
    assert stranger.get("/api/opportunities").json() == []

    refreshed = owner.post(f"/api/opportunities/{top['id']}/refresh-flights")
    assert refreshed.status_code == 200
    dismissed = owner.post(f"/api/opportunities/{top['id']}/dismiss").json()
    assert dismissed["status"] == "dismissed"

    history = owner.get("/api/notifications").json()
    assert history["total"] >= 1
    item = history["items"][0]
    assert item["recipient_masked"].startswith("o***@") and item["is_demo"]
    assert item["forecast_confidence_class"] == event["forecast_confidence_class"]
    assert "local_confidence_class" not in item and "local_confidence_reason" not in item
    outbox = owner.get("/api/dev/outbox?limit=5").json()
    assert any("Swell Alert" in str(m.get("subject")) for m in outbox)


def test_map_conditions_snapshot(app_client: ApiClient, db: Session, few_spots: list[str]) -> None:
    from datetime import timedelta

    from app.core.database import utcnow

    empty = app_client.get("/api/map/conditions").json()
    assert empty["valid_time"] is None and all(s["conditions"] is None for s in empty["spots"])
    run_pipeline(db)
    r = app_client.get("/api/map/conditions")
    assert r.status_code == 200 and r.headers["cache-control"] == PUBLIC_GET_CACHE_CONTROL
    snap = r.json()
    assert {s["slug"] for s in snap["spots"]} == set(few_spots)
    assert snap["step_hours"] == 3 and snap["range_start"] < snap["range_end"]
    c = next(s["conditions"] for s in snap["spots"] if s["slug"] == "jeffreys-bay")
    # Raw model fields and our prediction fields are both present, with provenance.
    assert (
        c["sig_wave_height_m"] is not None
        and c["breaking_height_max_ft"] >= c["breaking_height_min_ft"]
    )
    assert c["source_code"] == "demo" and c["is_demo"] is True and c["run_issued_at"]
    assert c["method"] and c["algorithm_version"] and c["explanation"]

    later = (utcnow() + timedelta(days=3, minutes=17)).isoformat()
    at = app_client.get("/api/map/conditions", params={"at": later}).json()
    assert at["valid_time"].endswith(":00:00Z") or at["valid_time"].endswith(":00:00+00:00")
    jb = next(s["conditions"] for s in at["spots"] if s["slug"] == "jeffreys-bay")
    assert (
        abs(
            (
                datetime.fromisoformat(jb["valid_time"]) - datetime.fromisoformat(at["valid_time"])
            ).total_seconds()
        )
        <= 90 * 60
    )
    # Requests beyond the forecast range are clamped to it, never extrapolated.
    far = app_client.get(
        "/api/map/conditions", params={"at": (utcnow() + timedelta(days=60)).isoformat()}
    ).json()
    assert far["valid_time"] <= far["range_end"]


def test_opportunity_and_alert_confidence_get_the_events_staleness_cap(
    db: Session, few_spots: list[str], only_simulated_swells: None
) -> None:
    """Once the event's run has gone stale, the opportunity and its alert read exactly like
    the event (EventOut): the same capped classes and ``stale``."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.core.database import utcnow
    from app.models import Notification, OpportunityMatch
    from app.services.forecasts.queries import stale_after_hours
    from app.services.opportunity_views import match_summary, notification_out
    from app.services.spot_views import event_out
    from tests.integration.test_pipeline import make_search, make_user, simulate

    make_search(db, make_user(db))
    run_pipeline(db)
    simulate(db)
    match = db.scalars(select(OpportunityMatch)).first()
    note = db.scalars(select(Notification)).first()
    assert match is not None and note is not None
    for now in (utcnow(), utcnow() + timedelta(hours=stale_after_hours("demo") + 1)):
        event = event_out(match.swell_event, now)
        summary = match_summary(match, now)
        alert = notification_out(note, now)
        assert summary.stale is event.stale
        key = "forecast_confidence_class"
        assert getattr(summary, key) == getattr(event, key) == getattr(alert, key)
        # Spec 20 (2.0): no combined local class in any of the three read models.
        for out in (summary, event, alert):
            assert not {"local_confidence_class", "local_confidence_reason"} & set(
                type(out).model_fields
            )
    assert event.stale is True and event.forecast_confidence_reasons[0].startswith(
        "Forecast data is out of date"
    )


def test_map_snapshot_reports_its_runs_freshness(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    run_pipeline(db)
    snap = app_client.get("/api/map/conditions").json()
    assert snap["stale"] is False
    assert snap["newest_run_issued_at"] and snap["last_updated_at"]
    fc = app_client.get("/api/spots/pipeline/forecast").json()
    assert fc["alerts_paused"] is False and fc["stale"] is False
    assert fc["run"]["members"] and fc["run"]["members"][0]["code"] == "demo"


def test_trips_and_their_event_carry_the_spots_freshness(
    make_client, db: Session, few_spots: list[str], only_simulated_swells: None
) -> None:  # type: ignore[no-untyped-def]
    """Review r2 FE-2 (spec 14): a trip to a spot whose refresh failed is not shown as
    current. The match's run is not stale by age; the published version marks the spot
    stale, and the trip, its detail and its event say so with the spot's ``freshness``."""
    from sqlalchemy import select

    from app.models import SurfSpot
    from app.services.forecasts import serving, versions

    owner: ApiClient = make_client()
    owner.register("owner@example.com")
    assert owner.post("/api/searches", search_payload()).status_code == 201
    run_pipeline(db)
    sim = owner.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    top = owner.get("/api/opportunities").json()[0]
    assert top["spot"]["slug"] == "jeffreys-bay"
    assert top["freshness"]["stale"] is False and top["freshness"]["version_id"]
    # The next version: J-Bay's refresh failed (SYNTHETIC failure), it keeps its run.
    current = versions.current_version(db)
    assert current is not None
    jbay = db.scalar(select(SurfSpot.id).where(SurfSpot.slug == "jeffreys-bay"))
    spot_runs = {
        sid: versions.SpotRun(entry.run_id, versions.STALE, entry.last_updated_at, "synthetic")
        if sid == jbay
        else entry
        for sid, entry in versions.spot_runs_of(current).items()
    }
    staged = versions.stage_version(
        db,
        provider=current.provider or "demo",
        source_code=current.source_code or "demo",
        forecast_run_id=current.forecast_run_id,
        components=versions.component_runs_of(current),
        spot_runs=spot_runs,
        previous=current,
    )
    second = versions.publish_version(db, staged)
    serving.refresh_version_marker(db)
    serving.clear_memo()
    trip = owner.get("/api/opportunities").json()[0]
    detail = owner.get(f"/api/opportunities/{trip['id']}").json()
    for fresh in (trip["freshness"], detail["freshness"], detail["event"]["freshness"]):
        assert fresh["version_id"] == second.id and fresh["stale"] is True
        assert fresh["reason"] == "refresh_failed"
    assert trip["stale"] is False  # the run itself is current


def test_the_spot_list_credits_the_sources_of_its_ratings(
    app_client: ApiClient, db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r4-spec-R4-SPEC-1 (non-negotiable 5): /spots shows every spot's rating,
    breaking height and next swell from its served run, so each spot carries the credit
    links that run's sources ask for, as events and trips do. The demo source asks for none;
    here it is credited as the Open-Meteo GFS source is (SYNTHETIC: only the links are
    borrowed). A spot nothing is served for has none."""
    from app.services.forecasts import queries
    from app.services.forecasts.registry import OPEN_METEO_NOAA_CODE

    credited = queries.SOURCE_ATTRIBUTION_LINKS[OPEN_METEO_NOAA_CODE]
    monkeypatch.setitem(queries.SOURCE_ATTRIBUTION_LINKS, "demo", credited)
    run_pipeline(db)
    want = [link.model_dump() for link in credited]
    assert any("Open-Meteo.com" in link["label"] for link in want)
    spots = app_client.get("/api/spots").json()
    served = [s for s in spots if s["slug"] in few_spots]
    assert served and all(s["best_upcoming"] is not None for s in served)
    assert all(s["attribution_links"] == want for s in served)
    assert all(s["attribution_links"] == [] for s in spots if s["slug"] not in few_spots)
    assert app_client.get("/api/spots/pipeline").json()["attribution_links"] == want


def test_a_trip_and_the_public_event_judge_the_forecast_at_the_same_instant(
    make_client, db: Session, few_spots: list[str], only_simulated_swells: None
) -> None:  # type: ignore[no-untyped-def]
    """Review r4-frontend-F1 (spec 14): the public spot page and its event cards are
    computed for the start of the UTC hour (public_cache.public_now); a trip was judged at
    the real time. A component that passes its age limit between the two showed the same
    swell "Out of date" in its trip card and current in the event card beside it. The
    published wind component here passes its limit half-way between the hour start and the
    setup (SYNTHETIC retrieval time): every read must give the same answer."""
    from dataclasses import replace

    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.database import utcnow
    from app.models import SurfSpot
    from app.services.forecasts import serving, versions
    from app.services.public_cache import public_now

    owner: ApiClient = make_client()
    owner.register("same-instant@example.com")
    assert owner.post("/api/searches", search_payload()).status_code == 201
    run_pipeline(db)
    sim = owner.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    current = versions.current_version(db)
    assert current is not None
    comps = versions.component_runs_of(current)
    now = utcnow()
    hour = public_now(now)
    limit = versions._component_limit(comps["atmosphere"], get_settings())
    retrieved = hour - limit + (now - hour) / 2  # current at the hour start, stale now
    comps["atmosphere"] = replace(comps["atmosphere"], retrieved_at=retrieved)
    staged = versions.stage_version(
        db,
        provider=current.provider or "demo",
        source_code=current.source_code or "demo",
        forecast_run_id=current.forecast_run_id,
        components=comps,
        spot_runs=versions.spot_runs_of(current),
        previous=current,
    )
    second = versions.publish_version(db, staged)
    serving.refresh_version_marker(db)
    serving.clear_memo()
    jbay = db.scalar(select(SurfSpot.id).where(SurfSpot.slug == "jeffreys-bay"))
    assert jbay is not None
    [trip] = [t for t in owner.get("/api/opportunities").json() if t["spot"]["id"] == jbay]
    detail = owner.get(f"/api/opportunities/{trip['id']}").json()
    [here] = owner.get("/api/spots/jeffreys-bay/opportunities").json()
    [event] = [
        e for e in owner.get("/api/spots/jeffreys-bay/events").json() if e["id"] == trip["event_id"]
    ]
    spot = owner.get("/api/spots/jeffreys-bay").json()
    reads = {
        "trip": trip,
        "trip detail": detail,
        "trip detail event": detail["event"],
        "spot page trip": here,
        "spot page event": event,
    }
    assert {name: r["freshness"]["version_id"] for name, r in reads.items()} == dict.fromkeys(
        reads, second.id
    )
    freshness = {name: r["freshness"]["stale"] for name, r in reads.items()}
    stale = {name: r["stale"] for name, r in reads.items()}
    confidence = {name: r["forecast_confidence_class"] for name, r in reads.items()}
    assert len(set(freshness.values())) == 1, freshness
    assert len(set(stale.values())) == 1, stale
    assert len(set(confidence.values())) == 1, confidence
    assert spot["freshness"]["stale"] == trip["freshness"]["stale"]
    if public_now() == hour:  # the hour did not turn during the test
        # The wind run is past its limit now, not at the hour start every page is judged at.
        assert versions.spot_freshness(second, utcnow())[jbay].stale is True
        assert set(freshness.values()) == {False}


def test_trip_lists_credit_the_sources_of_their_forecast(
    make_client, db: Session, few_spots: list[str], only_simulated_swells: None, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """Review r3-frontend-N4: a trip shows values derived from the forecast provider's data
    (our breaking height, rating and surf window), so every trip carries the credit links
    its swell's run asks for, as events do (Open-Meteo: "Weather data by Open-Meteo.com",
    also on paid plans). The demo source asks for none; here it is credited as the
    Open-Meteo GFS source is (SYNTHETIC: only the links are borrowed)."""
    from app.services.forecasts import queries
    from app.services.forecasts.registry import OPEN_METEO_NOAA_CODE

    credited = queries.SOURCE_ATTRIBUTION_LINKS[OPEN_METEO_NOAA_CODE]
    monkeypatch.setitem(queries.SOURCE_ATTRIBUTION_LINKS, "demo", credited)
    owner: ApiClient = make_client()
    owner.register("credits@example.com")
    assert owner.post("/api/searches", search_payload()).status_code == 201
    run_pipeline(db)
    sim = owner.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    want = [link.model_dump() for link in credited]
    assert any("Open-Meteo.com" in link["label"] for link in want)
    trips = owner.get("/api/opportunities").json()
    assert trips and all(t["attribution_links"] == want for t in trips)
    detail = owner.get(f"/api/opportunities/{trips[0]['id']}").json()
    assert detail["attribution_links"] == want == detail["event"]["attribution_links"]
    by_spot = owner.get("/api/spots/jeffreys-bay/opportunities").json()
    assert by_spot and all(t["attribution_links"] == want for t in by_spot)
    # Without credits asked for (the demo source as it is), the list carries none.
    monkeypatch.delitem(queries.SOURCE_ATTRIBUTION_LINKS, "demo")
    assert all(t["attribution_links"] == [] for t in owner.get("/api/opportunities").json())
