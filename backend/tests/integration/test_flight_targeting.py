"""Targeted flight searches (spec 26/27, design D15) on a real database.

A provider is asked for flights only for a new match, a material change, an explicit
request and a fare re-check right before an alert; never again for an unchanged forecast.
The demo provider (clearly labelled mock fares) stands in for a live one and counts its
calls. Every swell here is simulated and every fare is synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import FlightOffer, FlightSearch, Notification, OpportunityMatch, SurfSpot, User
from app.models.enums import QUALITY_ORDER, MatchStatus, NotificationStatus, QualityLabel
from app.schemas.searches import SearchIn
from app.services import accounts, searches
from app.services.flights import demo, service
from app.services.flights.base import FlightQuery, NormalizedOffer, RevalidationResult
from app.services.flights.registry import AirportDirectory
from app.services.forecasts.ingestion import simulate_swell
from app.services.matching.service import match_all
from app.services.notifications import alerts
from app.services.notifications.alerts import generate_alerts
from app.services.pipeline import run_pipeline
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.swell_detection.service import detect_events
from tests.conftest import ApiClient, search_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("only_simulated_swells")]


class CallLog:
    def __init__(self) -> None:
        self.searches: list[FlightQuery] = []
        self.revalidations: list[str] = []
        # Destinations for which the (synthetic) provider has no flights at all.
        self.no_flights_to: set[str] = set()


class CountingDemo(demo.DemoFlightProvider):
    """The demo provider, counting every search and fare re-check it is asked for."""

    def __init__(self, lookup: Any, log: CallLog) -> None:
        super().__init__(lookup)
        self.log = log

    def search(self, query: FlightQuery) -> list[NormalizedOffer]:
        self.log.searches.append(query)
        if query.destination in self.log.no_flights_to:
            return []
        return super().search(query)

    def revalidate(self, offer_id: str, payload: dict[str, Any] | None) -> RevalidationResult:
        self.log.revalidations.append(offer_id)
        return super().revalidate(offer_id, payload)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> CallLog:
    log = CallLog()

    def build(
        db: Session, settings: Any = None, directory: AirportDirectory | None = None
    ) -> CountingDemo:
        return CountingDemo((directory or AirportDirectory(db)).get, log)

    monkeypatch.setattr(service, "build_flight_provider", build)
    monkeypatch.setattr(alerts, "build_flight_provider", build)
    return log


def make_user(db: Session, email: str, verified: bool = True) -> User:
    user = accounts.register(db, "Test Surfer", email, "Barrels4Days!")
    if verified:
        user.email_verified_at = utcnow()
        db.commit()
    return user


def make_search(db: Session, user: User, **overrides: object) -> Any:
    return searches.create_search(db, user, SearchIn.model_validate(search_payload(**overrides)))


def swell(db: Session, slug: str = "jeffreys-bay", days: float = 7.0, hours: int = 60) -> None:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    start = (utcnow() + timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    assert simulate_swell(db, spot, start=start, duration_hours=hours).status == "success"


def surf_matches(db: Session) -> None:
    """The pipeline up to matching (no flights yet)."""
    compute_pending_predictions(db)
    detect_events(db)
    match_all(db)


def matches_of(db: Session, user: User) -> list[OpportunityMatch]:
    db.expire_all()
    return list(
        db.scalars(
            select(OpportunityMatch)
            .where(OpportunityMatch.user_id == user.id)
            .order_by(OpportunityMatch.created_at)
        )
    )


def count(db: Session, model: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


# --------------------------------------------------------------------------- unchanged forecast
def test_an_unchanged_forecast_never_searches_or_reprices_flights_again(
    db: Session, few_spots: list[str], calls: CallLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's probe (30-minute quotes, an unchanged forecast, hourly cycles) searched
    the provider again on every cycle; now nothing is asked for after the first search."""
    monkeypatch.setattr(demo, "DEMO_OFFER_TTL", timedelta(minutes=30))  # Duffel-like quotes
    user = make_user(db, "steady@example.com")
    make_search(db, user)
    swell(db)
    first = run_pipeline(db, acquire=False)
    assert first["flights"]["flight_found"] >= 1 and first["alerts"]["queued"] >= 1
    searched, rechecked = len(calls.searches), len(calls.revalidations)
    rows = count(db, FlightSearch)
    assert searched == first["flights"]["provider_queries"] > 0
    for cycle in range(1, 4):
        now = utcnow() + timedelta(minutes=61 * cycle)  # every quote has expired by now
        detect_events(db, now)
        match_all(db, now)
        flights = service.discover_flights(db, now)
        sent = generate_alerts(db, now)
        assert flights.matches == 0 and flights.provider_queries == 0, cycle
        assert sent.revalidated == 0 and sent.invalidated == 0 and sent.researched == 0
        assert sent.queued == 0
    assert len(calls.searches) == searched and len(calls.revalidations) == rechecked
    assert count(db, FlightSearch) == rows
    (match,) = matches_of(db, user)
    assert match.status == MatchStatus.FLIGHT_FOUND


# --------------------------------------------------------------------------- dedupe
def test_a_new_match_searches_each_unique_route_once_across_users(
    db: Session, few_spots: list[str], calls: CallLog
) -> None:
    users = [make_user(db, f"twin{i}@example.com") for i in range(3)]
    for u in users:
        make_search(db, u)  # the same trip: SFO -> PLZ, same dates
    swell(db)
    result = run_pipeline(db, acquire=False)
    keys = {q.cache_key("demo") for q in calls.searches}
    # Exactly one provider search per unique route key; the other users read the cache.
    assert len(calls.searches) == len(keys) == count(db, FlightSearch) > 0
    assert result["flights"]["cache_hits"] >= 2 * len(keys)
    assert all(m.status == MatchStatus.FLIGHT_FOUND for u in users for m in matches_of(db, u))


# --------------------------------------------------------------------------- material change
def test_a_material_change_searches_again_and_only_that_match(
    db: Session, few_spots: list[str], calls: CallLog
) -> None:
    mover, steady = make_user(db, "mover@example.com"), make_user(db, "steady2@example.com")
    moving = make_search(db, mover)
    make_search(db, steady, origins=["LAX"])
    swell(db)
    run_pipeline(db, acquire=False)
    (other,) = matches_of(db, steady)
    other_checked = other.flight_checked_at
    before = len(calls.searches)
    # The travel dates move (two more days before the swell): a material change.
    payload = search_payload(travel={"arrival_buffer_days": 4})
    searches.update_search(db, moving, SearchIn.model_validate(payload))
    match_all(db)
    (changed,) = matches_of(db, mover)
    assert changed.status == MatchStatus.PENDING_FLIGHTS
    stats = service.discover_flights(db)
    assert stats.matches == 1 and len(calls.searches) > before
    assert {q.origin for q in calls.searches[before:]} == {"SFO"}  # only the changed trip
    (changed,) = matches_of(db, mover)
    assert changed.status == MatchStatus.FLIGHT_FOUND
    assert matches_of(db, steady)[0].flight_checked_at == other_checked

    # The forecast class crossing the search threshold is material too: the match expires
    # when the search asks for a better class than the swell's peak, and is searched again
    # (here from the cache) once it qualifies again.
    peak = QualityLabel(matches_of(db, mover)[0].peak_label)
    if peak != QUALITY_ORDER[-1]:
        above = QUALITY_ORDER[QUALITY_ORDER.index(peak) + 1].value
        strict = search_payload(min_quality=above, travel={"arrival_buffer_days": 4})
    else:  # nothing ranks above the peak: ask for bigger waves than the swell has instead
        strict = search_payload(wave_min=40, wave_max=60, travel={"arrival_buffer_days": 4})
    searches.update_search(db, moving, SearchIn.model_validate(strict))
    match_all(db)
    assert matches_of(db, mover)[0].status == MatchStatus.EXPIRED
    searches.update_search(db, moving, SearchIn.model_validate(payload))
    match_all(db)
    assert matches_of(db, mover)[0].status == MatchStatus.PENDING_FLIGHTS
    assert service.discover_flights(db).matches == 1
    assert matches_of(db, mover)[0].status == MatchStatus.FLIGHT_FOUND


# --------------------------------------------------------------------------- budget
def test_the_daily_user_budget_and_round_robin(
    db: Session, few_spots: list[str], calls: CallLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "flight_user_daily_quota", 2)
    busy, other = make_user(db, "busy@example.com"), make_user(db, "other@example.com")
    make_search(db, busy, name="from SFO", origins=["SFO"])
    make_search(db, busy, name="from LAX", origins=["LAX"])
    make_search(db, other, name="from JFK", origins=["JFK"])
    swell(db)
    surf_matches(db)
    assert {m.status for u in (busy, other) for m in matches_of(db, u)} == {
        MatchStatus.PENDING_FLIGHTS
    }
    # Two matches searched in this run: one of each user, not two of the busy one.
    first = service.discover_flights(db, limit=2)
    assert first.matches == 2
    assert [m.status for m in matches_of(db, other)] == [MatchStatus.FLIGHT_FOUND]
    assert sorted(m.status for m in matches_of(db, busy)) == sorted(
        [MatchStatus.FLIGHT_FOUND, MatchStatus.PENDING_FLIGHTS]
    )
    # The busy user's two searches for today are used up: the other trip waits.
    before = len(calls.searches)
    second = service.discover_flights(db)
    assert second.budget_deferred == 1 and second.provider_queries == 0
    assert len(calls.searches) == before
    waiting = next(m for m in matches_of(db, busy) if m.status == MatchStatus.PENDING_FLIGHTS)
    assert "daily flight-search limit reached" in (waiting.status_reason or "")
    # After 00:00 UTC (the budget is per UTC day) it is searched.
    tomorrow = utcnow() + timedelta(days=1)
    third = service.discover_flights(db, tomorrow)
    assert third.provider_queries >= 1 and third.budget_deferred == 0
    assert {m.status for m in matches_of(db, busy)} == {MatchStatus.FLIGHT_FOUND}


# --------------------------------------------------------------------------- revalidation
def test_fares_are_rechecked_only_right_before_an_alert_is_sent(
    db: Session, few_spots: list[str], calls: CallLog
) -> None:
    verified = make_user(db, "verified@example.com")
    unverified = make_user(db, "unverified@example.com", verified=False)
    make_search(db, verified)
    make_search(db, unverified)  # same trip: its alert is withheld (email not verified)
    swell(db)
    result = run_pipeline(db, acquire=False, deliver=False)
    queued = db.scalar(
        select(func.count(Notification.id)).where(Notification.status == NotificationStatus.QUEUED)
    )
    assert queued == 1 and result["alerts"]["suppressed"] >= 1
    # One re-check, for the alert that is actually sent; none for the withheld one.
    assert len(calls.revalidations) == 1 == result["alerts"]["revalidated"]
    again = generate_alerts(db)
    assert again.queued == 0 and again.revalidated == 0 and len(calls.revalidations) == 1


# --------------------------------------------------------------------------- expired offers
def test_expired_quotes_end_the_cache_and_are_never_shown_as_bookable(
    db: Session, few_spots: list[str], calls: CallLog, make_client: Callable[[], ApiClient]
) -> None:
    client = make_client()
    client.register("expiry@example.com")
    assert client.post("/api/searches", search_payload()).status_code == 201
    swell(db)
    run_pipeline(db, acquire=False)
    fs = db.scalars(select(FlightSearch)).all()
    assert fs and all(
        s.expires_at
        == min(min(o.offer_expires_at for o in s.offers), s.searched_at + timedelta(hours=6))
        for s in fs
        if s.offers
    )
    (match,) = db.scalars(select(OpportunityMatch)).all()
    # Time passes: every quote and every cache entry expires (the demo offers carry a
    # booking link here so its hiding can be checked).
    past = utcnow() - timedelta(minutes=1)
    db.execute(update(FlightOffer).values(offer_expires_at=past, booking_url="https://book/1"))
    db.execute(update(FlightSearch).values(expires_at=past))
    db.commit()
    detail = client.get(f"/api/opportunities/{match.id}").json()
    assert detail["best_offer_expired"] is True and detail["offers"]
    for offer in detail["offers"]:
        assert offer["expired"] is True
        assert all(link["kind"] == "route_search" for link in offer["links"])
        assert all(link["note"] == "route search — fares may differ" for link in offer["links"])
    # An explicit request asks the provider again (the expired cache is never used).
    before = len(calls.searches)
    refreshed = client.post(f"/api/opportunities/{match.id}/refresh-flights")
    assert refreshed.status_code == 200, refreshed.text
    assert len(calls.searches) > before
    fresh = client.get(f"/api/opportunities/{match.id}").json()
    assert fresh["status"] == "flight_found" and fresh["best_offer_expired"] is False
    assert fresh["offers"] and not any(o["expired"] for o in fresh["offers"])


def test_an_explicit_request_is_refused_once_the_daily_budget_is_used(
    db: Session, few_spots: list[str], calls: CallLog, make_client: Callable[[], ApiClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(get_settings(), "flight_user_daily_quota", 2)
    client = make_client()
    client.register("limit@example.com")
    assert client.post("/api/searches", search_payload()).status_code == 201
    swell(db)
    run_pipeline(db, acquire=False)  # two searches (two departure dates from SFO)
    (match,) = db.scalars(select(OpportunityMatch)).all()
    refused = client.post(f"/api/opportunities/{match.id}/refresh-flights")
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "flight_budget_reached"


# --------------------------------------------------------------------------- origins, layovers
def test_all_five_origins_are_searched_and_three_layovers_reach_the_provider(
    db: Session, few_spots: list[str], calls: CallLog
) -> None:
    user = make_user(db, "five@example.com")
    origins = ["SFO", "LAX", "JFK", "SEA", "ORD"]
    make_search(db, user, origins=origins, travel={"max_layovers": 3})
    swell(db)
    run_pipeline(db, acquire=False)
    assert [q.origin for q in calls.searches] == origins  # one first-choice date each
    assert {q.max_connections for q in calls.searches} == {3}
    (match,) = matches_of(db, user)
    assert match.status == MatchStatus.FLIGHT_FOUND and match.origin_iata in origins


# --------------------------------------------------------------------------- gateway airports
def test_a_regional_airport_with_no_flights_falls_back_to_the_gateway_once(
    db: Session, few_spots: list[str], calls: CallLog
) -> None:
    db.execute(text("UPDATE surf_spots SET is_active = true WHERE slug = 'lakey-peak'"))
    db.commit()
    calls.no_flights_to.add("BMU")  # the regional airport returns nothing at all
    user = make_user(db, "lakey@example.com")
    make_search(
        db,
        user,
        destinations={"spot_slugs": ["lakey-peak"]},
        origins=["SYD"],
        min_quality="fair",
        wave_min=1,
        wave_max=30,
    )
    swell(db, "lakey-peak")
    result = run_pipeline(db, acquire=False)
    assert result["flights"]["gateway_fallbacks"] == 1
    (match,) = matches_of(db, user)
    assert match.status == MatchStatus.FLIGHT_FOUND
    assert match.destination_iata == "DPS" and match.transfer_minutes == 270
    assert "searched via DPS" in (match.status_reason or "")
    assert {q.destination for q in calls.searches} == {"BMU", "DPS"}
    # The trip stays planned via the gateway: an unchanged forecast searches nothing.
    before = len(calls.searches)
    match_all(db)
    assert service.discover_flights(db).matches == 0 and len(calls.searches) == before
    (match,) = matches_of(db, user)
    assert match.destination_iata == "DPS" and match.status == MatchStatus.FLIGHT_FOUND


# --------------------------------------------------------------------------- lead time
def _short_notice_window(db: Session) -> datetime:
    # Nominally 4 days out. The demo swell builds up over the hours before its start (a
    # Gaussian edge, sigma 8 h), so its surf window can begin up to about half a day
    # earlier, and daylight can push it later: 3.5-4.6 days ahead, inside the 3-7 day
    # short-notice band and short of the old 5-day floor whatever the hour the test runs.
    swell(db, days=4.0, hours=48)
    surf_matches(db)
    return utcnow()


def test_short_notice_swells_match_by_the_searchs_own_lead_time(
    db: Session, few_spots: list[str], calls: CallLog, make_client: Callable[[], ApiClient]
) -> None:
    """Before Swell 2.0 a swell 4 days out never matched (a global 5-day floor); now each
    search's alert lead time decides, and short-notice trips are flagged and ranked first."""
    client = make_client()
    client.register("soon@example.com")
    default = client.post("/api/searches", search_payload(name="default lead"))
    later_only = client.post(
        "/api/searches", search_payload(name="5+ days only", alert_lead_min_days=5)
    )
    assert default.status_code == later_only.status_code == 201
    assert default.json()["alert_lead_min_days"] == 2  # ALERT_LEAD_DEFAULT_MIN_DAYS
    _short_notice_window(db)
    rows = db.scalars(select(OpportunityMatch)).all()
    assert [str(m.search_id) for m in rows] == [default.json()["id"]]
    opps = client.get("/api/opportunities").json()
    assert len(opps) == 1
    assert opps[0]["short_notice"] is True, (opps[0]["lead_days"], opps[0]["window_start"])
    assert 3 <= opps[0]["lead_days"] <= 7
