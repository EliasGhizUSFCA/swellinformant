"""Spots, forecasts, events, reference data, system status and opportunity endpoints."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.services.pipeline import run_pipeline
from tests.conftest import ApiClient, search_payload

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
    assert fc["breaking_height_method"] == "komar_gaughan_v1_uncalibrated"
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
    assert status["detection"]["lead_min_days"] == 5.0
    assert {j["job"] for j in status["jobs"]} >= {"forecasts.acquire", "notifications.deliver"}
    ready = app_client.get("/api/health/ready").json()
    assert ready["checks"] == {"database": "ok", "redis": "ok"}


def test_opportunity_endpoints_and_ownership(
    make_client, db: Session, few_spots: list[str]
) -> None:  # type: ignore[no-untyped-def]
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
    detail = owner.get(f"/api/opportunities/{top['id']}").json()
    assert detail["offers"] and detail["offers"][0]["is_mock"]
    assert detail["offers"][0]["outbound"]["segments"]
    assert any(link["label"] == "Google Flights" for link in detail["offers"][0]["links"])
    assert detail["travel_window"] and detail["daily"] and detail["limitations"]
    assert any("MOCK FARES" in n for n in detail["limitations"])
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
    outbox = owner.get("/api/dev/outbox?limit=5").json()
    assert any("Swell Alert" in str(m.get("subject")) for m in outbox)
