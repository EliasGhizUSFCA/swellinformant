"""Targeted flight searches, short-notice ranking, lead times and email delivery details
(design D15, spec 26-29) — pure logic, no database or network.

Every offer, airport pairing and price here is a synthetic test value.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.models.enums import CabinClass
from app.services.flights.airports import gateway_fallbacks
from app.services.flights.base import NormalizedOffer, Segment, Slice, operating_carrier_names
from app.services.flights.demo import AirportInfo
from app.services.flights.duffel import DuffelProvider
from app.services.flights.links import ROUTE_SEARCH_NOTE, inspection_links
from app.services.flights.service import (
    MAX_CONNECTIONS,
    cache_expiry,
    max_connections,
    per_match_budget,
    plan_queries,
    queue_order,
)
from app.services.flights.windows import compute_travel_window
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.matching.scoring import (
    SHORT_NOTICE_BOOST,
    height_fit,
    is_short_notice,
    match_surf_component,
    ranking_score,
    surf_component,
)
from app.services.matching.service import lead_window
from app.services.notifications.providers import EmailMessageData, ResendEmailProvider
from app.services.notifications.templates import render_alert_email
from tests.unit.test_flight_providers import DUFFEL_OFFER, TZ

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- cache expiry
def _offer(expires_at: datetime | None) -> NormalizedOffer:
    seg = Segment(
        "SFO", "PLZ", NOW, NOW + timedelta(hours=20), "2026-11-01T05:00", "x", "D1", "Demo Air", "1"
    )
    return NormalizedOffer(
        "demo", "o", Decimal("100"), "USD", Slice([seg]), Slice([seg]), "UTC", "UTC",
        expires_at=expires_at,
    )  # fmt: skip


def test_the_cache_lasts_until_the_first_offer_expires_or_the_ttl() -> None:
    ttl = timedelta(hours=6)
    assert cache_expiry([], NOW, ttl) == NOW + ttl  # no results: remembered for the TTL
    assert cache_expiry([_offer(None)], NOW, ttl) == NOW + ttl  # offers without an expiry
    # Duffel-like quotes (about 30 minutes): the cache entry ends with the first of them.
    soon = [_offer(NOW + timedelta(minutes=45)), _offer(NOW + timedelta(minutes=30))]
    assert cache_expiry(soon, NOW, ttl) == NOW + timedelta(minutes=30)
    assert cache_expiry([_offer(NOW + timedelta(hours=24))], NOW, ttl) == NOW + ttl
    # Already expired when quoted: never served from the cache.
    assert cache_expiry([_offer(NOW - timedelta(minutes=1))], NOW, ttl) == NOW


# --------------------------------------------------------------------------- query plan
def test_every_origin_gets_its_first_choice_within_the_per_match_budget() -> None:
    assert per_match_budget(5, 3) == 5  # all five origins, first choice each
    assert per_match_budget(1, 3) == 3  # one origin: its second choice fits too
    assert per_match_budget(9, 3) == 5  # never more origins than a search may have


def test_up_to_three_layovers_are_passed_to_the_provider() -> None:
    assert MAX_CONNECTIONS == 3
    assert max_connections(SimpleNamespace(direct_only=False, max_layovers=3)) == 3
    assert max_connections(SimpleNamespace(direct_only=False, max_layovers=2)) == 2
    assert max_connections(SimpleNamespace(direct_only=True, max_layovers=3)) == 0


class _Directory:
    def __init__(self, airports: dict[str, AirportInfo]) -> None:
        self.airports = airports

    def get(self, code: str) -> AirportInfo | None:
        return self.airports.get(code)


def test_a_five_origin_search_plans_one_first_choice_query_per_origin() -> None:
    airports = {
        "PLZ": AirportInfo("PLZ", -33.98, 25.62, "Africa/Johannesburg"),
        "SFO": AirportInfo("SFO", 37.62, -122.38, "America/Los_Angeles"),
        "LAX": AirportInfo("LAX", 33.94, -118.41, "America/Los_Angeles"),
        "JFK": AirportInfo("JFK", 40.64, -73.78, "America/New_York"),
        "LHR": AirportInfo("LHR", 51.47, -0.45, "Europe/London"),
        "SYD": AirportInfo("SYD", -33.95, 151.18, "Australia/Sydney"),
    }
    window = compute_travel_window(
        surf_start=NOW + timedelta(days=6),
        surf_end=NOW + timedelta(days=8),
        spot_tz="Africa/Johannesburg",
        destination_iata="PLZ",
        destination_tz="Africa/Johannesburg",
        transfer_minutes=75,
        arrival_buffer_days=2,
        departure_buffer_days=1,
        min_days_at_destination=3,
        max_trip_days=14,
        now=NOW,
    )
    travel = SimpleNamespace(
        max_flight_hours=45,
        travelers=1,
        cabin_class=CabinClass.ECONOMY,
        currency="USD",
        direct_only=False,
        max_layovers=3,
        preferred_airlines=[],
    )
    search = SimpleNamespace(travel=travel)
    origins = ["SFO", "LAX", "JFK", "LHR", "SYD"]
    plan = plan_queries(search, window, _Directory(airports), origins)  # type: ignore[arg-type]
    assert [q.origin for q in plan] == origins  # one each, in the user's order
    assert {q.max_connections for q in plan} == {3}
    assert {q.destination for q in plan} == {"PLZ"}
    one = plan_queries(search, window, _Directory(airports), ["SFO"])  # type: ignore[arg-type]
    assert len(one) == 2 and one[0].departure_date != one[1].departure_date  # 1st + 2nd choice
    first_only = plan_queries(
        search,
        window,
        _Directory(airports),  # type: ignore[arg-type]
        ["SFO"],
        first_choice_only=True,
    )
    assert first_only == one[:1]


# --------------------------------------------------------------------------- queue order
def _match(user: str, score: int, days: float, requested: bool = False) -> Any:
    return SimpleNamespace(
        user_id=user,
        overall_score=score,
        window_start=NOW + timedelta(days=days),
        flights_requested_at=NOW if requested else None,
        flight_checked_at=None,
        name=f"{user}{score}",
    )


def test_users_are_served_round_robin_explicit_requests_and_short_notice_first() -> None:
    a1, a2, a3 = _match("a", 95, 9), _match("a", 90, 9), _match("a", 85, 9)
    b1 = _match("b", 40, 9)
    c_soon = _match("c", 60, 4)  # 60 + SHORT_NOTICE_BOOST outranks 70 nine days out
    c_late = _match("c", 70, 9)
    b_asked = _match("b", 10, 9, requested=True)
    order = queue_order([a1, a2, a3, b1, c_soon, c_late, b_asked], NOW)
    names = [m.name for m in order]
    # Each user's first match before anyone's second: one account's many searches cannot
    # use up the provider's daily quota before the others are served.
    assert names[:3] == ["b10", "a95", "c60"]  # b asked explicitly: first
    assert names[3:6] == ["b40", "a90", "c70"]
    assert names[6:] == ["a85"]


def test_short_notice_band_and_ranking_boost() -> None:
    s = get_settings()
    assert (s.short_notice_min_days, s.short_notice_max_days) == (3.0, 7.0)
    assert not is_short_notice(NOW + timedelta(days=2.9), NOW)
    assert is_short_notice(NOW + timedelta(days=3), NOW)
    assert is_short_notice(NOW + timedelta(days=7), NOW)
    assert not is_short_notice(NOW + timedelta(days=7.1), NOW)
    assert ranking_score(60, NOW + timedelta(days=4), NOW) == 60 + SHORT_NOTICE_BOOST
    assert ranking_score(60, NOW + timedelta(days=9), NOW) == 60


# --------------------------------------------------------------------------- preferred height
def test_the_preferred_wave_height_is_used_in_the_surf_component() -> None:
    # Without a preference the component is unchanged.
    assert surf_component(80, 70) == 77.0
    plain = SimpleNamespace(wave_preferred_ft=None, wave_min_ft=4.0, wave_max_ft=12.0)
    assert match_surf_component(80, 70, 5.0, 7.0, plain) == 77.0
    # With one: closer to the preferred height scores higher, all else equal.
    pref = SimpleNamespace(wave_preferred_ft=6.0, wave_min_ft=4.0, wave_max_ft=12.0)
    on_target = match_surf_component(80, 70, 5.0, 7.0, pref)  # mid 6 ft
    too_big = match_surf_component(80, 70, 10.0, 12.0, pref)  # mid 11 ft
    assert on_target > too_big
    assert height_fit(5.0, 7.0, 6.0, 4.0, 12.0) == 1.0
    assert height_fit(11.0, 13.0, 6.0, 4.0, 12.0) == 0.0  # at the far end of the range
    assert 0 < height_fit(7.0, 9.0, 6.0, 4.0, 12.0) < 1


# --------------------------------------------------------------------------- lead time
def test_the_search_lead_days_apply_inside_the_global_detection_window() -> None:
    s = get_settings()
    assert s.detection_lead_min_days == 1.0 and s.detection_lead_max_days == 10.0
    low, high = lead_window(SimpleNamespace(alert_lead_min_days=2, alert_lead_max_days=10), s)
    assert (low, high) == (timedelta(days=2), timedelta(days=10))
    # A search asking for 0 days still waits for the global 1-day floor; 16 days is capped
    # at the 10-day detection horizon.
    low, high = lead_window(SimpleNamespace(alert_lead_min_days=0, alert_lead_max_days=16), s)
    assert (low, high) == (timedelta(days=1), timedelta(days=10))
    low, high = lead_window(SimpleNamespace(alert_lead_min_days=3, alert_lead_max_days=5), s)
    assert (low, high) == (timedelta(days=3), timedelta(days=5))


# --------------------------------------------------------------------------- gateways
def test_regional_airports_fall_back_to_the_spots_own_gateway() -> None:
    lakey = [
        SimpleNamespace(airport_iata="BMU", transfer_minutes=150),
        SimpleNamespace(airport_iata="DPS", transfer_minutes=270),
    ]
    assert [a.airport_iata for a in gateway_fallbacks(lakey, "BMU", None)] == ["DPS"]
    assert gateway_fallbacks(lakey, "BMU", 240) == []  # outside the search's transfer limit
    assert gateway_fallbacks(lakey, "DPS", None) == []  # DPS is not a regional airport
    bocas = [
        SimpleNamespace(airport_iata="BOC", transfer_minutes=25),
        SimpleNamespace(airport_iata="PTY", transfer_minutes=120),
    ]
    assert [a.airport_iata for a in gateway_fallbacks(bocas, "BOC", None)] == ["PTY"]


# --------------------------------------------------------------------------- carriers
def test_duffel_operating_carrier_full_names_are_stored_and_listed() -> None:
    raw = copy.deepcopy(DUFFEL_OFFER)
    raw["slices"][0]["segments"][1]["operating_carrier"] = {  # type: ignore[index]
        "iata_code": "4Z",
        "name": "Airlink",
    }
    s = Settings(duffel_access_token="duffel_test_x", flight_provider="duffel")
    offer = DuffelProvider(TZ.get, s).normalize(raw)
    seg = offer.outbound.segments[1]
    assert seg.operating_carrier == "4Z" and seg.operating_carrier_name == "Airlink"
    assert seg.as_dict("outbound")["operating_carrier_name"] == "Airlink"
    # The marketing carrier's name where Duffel names no other operating airline.
    assert offer.operating_carriers == ["United Airlines", "Airlink"]
    stored = [s.as_dict("outbound") for s in offer.outbound.segments]
    assert operating_carrier_names(stored) == ["United Airlines", "Airlink"]


# --------------------------------------------------------------------------- links
def test_route_search_links_are_labelled_as_such() -> None:
    links = inspection_links("SFO", "PLZ", date(2026, 11, 5), date(2026, 11, 12), 2)
    assert [link["label"] for link in links] == ["Google Flights", "Skyscanner"]
    assert all(link["kind"] == "route_search" for link in links)
    assert all(
        link["note"] == ROUTE_SEARCH_NOTE == "route search — fares may differ" for link in links
    )
    assert "for%202%20adults" in links[0]["url"] and "adultsv2=2" in links[1]["url"]


# --------------------------------------------------------------------------- email
SNAP: dict[str, Any] = {
    "kind": "new_opportunity",
    "spot_name": "Uluwatu",
    "spot_slug": "uluwatu",
    "country": "Indonesia",
    "window_label": "Nov 5–7",
    "lead_days": 4.2,
    "short_notice": True,
    "height_min_ft": 6,
    "height_max_ft": 9,
    "peak_label": "very_good",
    "peak_score": 78,
    "confidence": 55,
    "confidence_label": "moderate",
    "swell_height_m": 2.1,
    "swell_period_s": 15,
    "swell_direction": "SW",
    "wind_relation": "offshore",
    "wind_speed_kmh": 12,
    "wind_direction": "E",
    "forecast_confidence_class": "moderate",
    "peak_in_hours": 100.0,
    "research_confidence_class": "high",
    "arrival_date": "2026-11-03",
    "departure_date": "2026-11-08",
    "arrival_buffer_days": 2,
    "departure_buffer_days": 1,
    "price": "1180.00",
    "currency": "AUD",
    "quoted_price": "772.30",
    "quoted_currency": "USD",
    "origin": "SYD",
    "destination": "DPS",
    "origin_airport": {"name": "Sydney Kingsford Smith Airport", "city": "Sydney"},
    "destination_airport": {"name": "Ngurah Rai International Airport", "city": "Denpasar"},
    "transfer_minutes": 45,
    "transfer_mode": "car",
    "airline": "Demo Air",
    "operating_carriers": ["Demo Air", "Coral Demo Air"],
    "outbound_departure_local": "2026-11-03T06:15:00",
    "outbound_arrival_local": "2026-11-03T11:40:00",
    "inbound_departure_local": "2026-11-08T19:10:00",
    "inbound_arrival_local": "2026-11-09T05:05:00",
    "outbound_hours": 6.4,
    "inbound_hours": 6.9,
    "is_mock_flight": True,
    "price_checked_label": "re-validated 2026-11-01 12:00 UTC",
}
LINKS = {
    "opportunity": "https://app/o/1",
    "forecast": "https://app/spots/uluwatu",
    "dashboard": "https://app/dashboard",
    "manage": "https://app/settings#alerts",
    "unsubscribe": "https://app/unsubscribe?token=t",
    "booking": "https://provider/book/1",
    "flights": "https://www.google.com/travel/flights?q=x",
    "flights_alt": "https://www.skyscanner.com/x",
    "flights_note": ROUTE_SEARCH_NOTE,
}


def test_the_alert_email_carries_every_spec_29_field() -> None:
    _, text, html = render_alert_email(SNAP, LINKS)
    for expected in (
        "Destination: Uluwatu, Indonesia",
        "Swell dates: Nov 5–7 (starts in about 4 days, short notice)",
        "Predicted quality: Very Good (7.8/10)",
        "Predicted breaking waves (our estimate): 6–9 ft faces",
        "Wind: offshore, 12 km/h from E",
        "Forecast confidence: Moderate",
        "Research confidence: High (how well this break's ideal conditions are documented",
        # The converted fare says so and names the provider's own fare.
        "Round-trip flight: A$1,180 per traveller (converted from $772 with a configured "
        "exchange rate; the provider charges in USD) (MOCK FARE — not a real price)",
        "Flight dates: leave SYD Tue, Nov 3 06:15, land DPS Tue, Nov 3 11:40; fly home "
        "Sun, Nov 8 19:10, land Mon, Nov 9 05:05 (local times)",
        "Departure airport: SYD — Sydney Kingsford Smith Airport",
        "Destination airport: DPS — Ngurah Rai International Airport, Denpasar",
        "Ground transfer: about 0.8 h by car from the airport to the break",
        "Operated by: Demo Air, Coral Demo Air",
        "Book this fare: https://provider/book/1",
        "Google Flights (route search — fares may differ): https://www.google.com/travel/",
        "Skyscanner (route search — fares may differ): https://www.skyscanner.com/x",
        "Your dashboard: https://app/dashboard",
        "Unsubscribe from email alerts: https://app/unsubscribe?token=t",
    ):
        assert expected in text, expected
    # Forecast and research confidence are separate rows, never one number.
    assert "Confidence at this break" not in text
    assert "Book this fare" in html and "route search — fares may differ" in html
    # No "useless after day 5" cut-off in the disclaimer (spec 28).
    assert "more than 5 days ahead" not in text


# --------------------------------------------------------------------------- idempotency
def _resend(handler: Any) -> ResendEmailProvider:
    s = Settings(_env_file=None, email_provider="resend", resend_api_key="re_test_key")  # type: ignore[call-arg]
    return ResendEmailProvider(
        s, client=httpx.Client(transport=httpx.MockTransport(handler)), backoff_seconds=0
    )


def test_resend_requests_carry_the_messages_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "email_1"})

    msg = EmailMessageData(
        to="someone@example.com",
        subject="s",
        text="t",
        headers={"List-Unsubscribe": "<https://app/u>"},
        idempotency_key="notification-123",
    )
    assert _resend(handler).send(msg).message_id == "email_1"
    assert seen[0].headers["Idempotency-Key"] == "notification-123"
    body = json.loads(seen[0].content)
    assert body["headers"] == {"List-Unsubscribe": "<https://app/u>"}  # not in the body
    _resend(handler).send(EmailMessageData(to="someone@example.com", subject="s", text="t"))
    assert "Idempotency-Key" not in seen[1].headers


def test_a_retried_send_reuses_the_same_key() -> None:
    """A timeout followed by a retry (services/http.py retries POSTs) repeats the key, so
    Resend sends the message once."""
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        if len(keys) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"id": "email_2"})

    msg = EmailMessageData(to="a@example.com", subject="s", text="t", idempotency_key="k-1")
    assert _resend(handler).send(msg).message_id == "email_2"
    assert keys == ["k-1", "k-1"]


def test_a_concurrent_request_with_the_same_key_is_retried_later_not_failed() -> None:
    """Resend answers 409 concurrent_idempotent_requests while the first request with the
    key (e.g. one that timed out here) is still in progress: "it is safe to retry this
    request later". The message may be on its way, so it must not be marked failed."""

    def busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "statusCode": 409,
                "name": "concurrent_idempotent_requests",
                "message": "Another request with the same idempotency key is in progress.",
            },
        )

    msg = EmailMessageData(to="a@example.com", subject="s", text="t", idempotency_key="k-2")
    with pytest.raises(TransientProviderError):
        _resend(busy).send(msg)

    def reused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"statusCode": 409, "name": "invalid_idempotent_request", "message": "x"}
        )

    # The key was used for a different payload: retrying is useless (a permanent error).
    with pytest.raises(PermanentProviderError):
        _resend(reused).send(msg)


def test_how_far_ahead_the_swell_starts_is_rounded_conventionally() -> None:
    def dates(lead: float) -> str:
        _, text, _ = render_alert_email({**SNAP, "lead_days": lead, "short_notice": False}, LINKS)
        line = next(x for x in text.splitlines() if x.startswith("Swell dates:"))
        return line.removeprefix("Swell dates: Nov 5–7 ")

    assert dates(4.5) == "(starts in about 5 days)"  # not banker's rounding (4)
    assert dates(1.2) == "(starts in about 1 day)"
    assert dates(0.4) == "(starts within a day)"


# --------------------------------------------------------------------------- settings
def test_flight_refresh_hours_is_gone_and_old_env_files_still_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 12-hour periodic re-search is removed (design D15). Settings ignore unknown keys,
    so an env file that still sets FLIGHT_REFRESH_HOURS keeps working."""
    monkeypatch.setenv("FLIGHT_REFRESH_HOURS", "12")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not hasattr(s, "flight_refresh_hours")
    assert s.flight_user_daily_quota == 20
