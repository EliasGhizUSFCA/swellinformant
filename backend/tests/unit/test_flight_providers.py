"""Flight provider adapters (Duffel, Amadeus Enterprise, demo) — no network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.models.enums import OfferValidation
from app.services.flights.amadeus import AmadeusProvider
from app.services.flights.base import FlightQuery
from app.services.flights.demo import DEMO_CARRIERS, AirportInfo, DemoFlightProvider
from app.services.flights.duffel import DuffelProvider
from app.services.http import PermanentProviderError, TransientProviderError

TZ = {
    "SFO": "America/Los_Angeles",
    "PLZ": "Africa/Johannesburg",
    "JNB": "Africa/Johannesburg",
    "HNL": "Pacific/Honolulu",
    "NAN": "Pacific/Fiji",
    "LAX": "America/Los_Angeles",
}
QUERY = FlightQuery("SFO", "PLZ", date(2026, 8, 6), date(2026, 8, 14), adults=1)


def seg(
    o: str, d: str, dep: str, arr: str, carrier: str = "UA", num: str = "100"
) -> dict[str, object]:
    return {
        "origin": {"iata_code": o, "time_zone": TZ[o]},
        "destination": {"iata_code": d, "time_zone": TZ[d]},
        "departing_at": dep,
        "arriving_at": arr,
        "marketing_carrier": {"iata_code": carrier, "name": "United Airlines"},
        "marketing_carrier_flight_number": num,
        "operating_carrier": {"iata_code": carrier},
        "passengers": [
            {"baggages": [{"type": "checked", "quantity": 1}, {"type": "carry_on", "quantity": 1}]}
        ],
    }


DUFFEL_OFFER = {
    "id": "off_123",
    "total_amount": "812.40",
    "total_currency": "USD",
    "expires_at": "2026-07-21T12:00:00Z",
    "owner": {"iata_code": "UA", "name": "United Airlines"},
    "slices": [
        {
            "segments": [
                seg("SFO", "JNB", "2026-08-06T20:00:00", "2026-08-07T22:30:00"),
                seg("JNB", "PLZ", "2026-08-08T09:00:00", "2026-08-08T10:40:00", "SA", "403"),
            ]
        },
        {
            "segments": [
                seg("PLZ", "JNB", "2026-08-14T11:00:00", "2026-08-14T12:40:00", "SA", "404"),
                seg("JNB", "SFO", "2026-08-14T20:00:00", "2026-08-15T09:30:00"),
            ]
        },
    ],
}


def duffel(handler) -> DuffelProvider:  # type: ignore[no-untyped-def]
    s = Settings(duffel_access_token="duffel_test_x", flight_provider="duffel")
    return DuffelProvider(
        TZ.get, s, client=httpx.Client(transport=httpx.MockTransport(handler)), backoff_seconds=0
    )


class TestDuffel:
    def test_requires_token(self) -> None:
        with pytest.raises(PermanentProviderError):
            DuffelProvider(TZ.get, Settings(duffel_access_token=None))

    def test_search_request_and_normalisation(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = request.headers
            seen["params"] = dict(request.url.params)
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"data": {"id": "orq_1", "offers": [DUFFEL_OFFER]}})

        offers = duffel(handler).search(QUERY)
        headers = seen["headers"]
        assert headers["Duffel-Version"] == "v2"  # type: ignore[index]
        assert headers["Authorization"] == "Bearer duffel_test_x"  # type: ignore[index]
        assert seen["params"] == {"return_offers": "true", "supplier_timeout": "20000"}
        body = seen["body"]["data"]  # type: ignore[index]
        assert body["slices"][1] == {
            "origin": "PLZ",
            "destination": "SFO",
            "departure_date": "2026-08-14",
        }
        assert body["passengers"] == [{"type": "adult"}]
        o = offers[0]
        assert o.total_price == Decimal("812.40") and o.currency == "USD" and not o.is_mock
        # 20:00 PDT = 03:00 UTC next day; 10:40 SAST = 08:40 UTC
        assert o.outbound.departure_at == datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
        assert o.outbound.arrival_at == datetime(2026, 8, 8, 8, 40, tzinfo=UTC)
        assert o.outbound.stops == 1 and o.inbound.stops == 1
        assert o.airlines == ["UA", "SA"]
        assert o.baggage == "1 checked, 1 carry-on per traveller"
        assert o.expires_at == datetime(2026, 7, 21, 12, tzinfo=UTC)

    def test_auth_failure_is_permanent_and_5xx_is_transient(self) -> None:
        with pytest.raises(PermanentProviderError, match="authentication"):
            duffel(
                lambda r: httpx.Response(401, json={"errors": [{"message": "invalid token"}]})
            ).search(QUERY)
        with pytest.raises(TransientProviderError):
            duffel(lambda r: httpx.Response(503)).search(QUERY)

    def test_invalid_route_is_permanent(self) -> None:
        with pytest.raises(PermanentProviderError, match="422"):
            duffel(
                lambda r: httpx.Response(
                    422, json={"errors": [{"message": "airport not supported"}]}
                )
            ).search(QUERY)

    def test_revalidation(self) -> None:
        p = duffel(
            lambda r: httpx.Response(200, json={"data": {**DUFFEL_OFFER, "total_amount": "850.00"}})
        )
        result = p.revalidate("off_123", None)
        assert result.status == OfferValidation.VALID and result.total_price == Decimal("850.00")
        gone = duffel(lambda r: httpx.Response(404, json={"errors": [{"message": "not found"}]}))
        assert gone.revalidate("off_123", None).status == OfferValidation.UNAVAILABLE

    def test_malformed_offers_are_skipped(self) -> None:
        bad = {**DUFFEL_OFFER, "id": "off_bad", "slices": DUFFEL_OFFER["slices"][:1]}
        offers = duffel(
            lambda r: httpx.Response(200, json={"data": {"offers": [bad, DUFFEL_OFFER]}})
        ).search(QUERY)
        assert [o.provider_offer_id for o in offers] == ["off_123"]


AMADEUS_OFFER = {
    "id": "1",
    "source": "GDS",
    "itineraries": [
        {
            "segments": [
                {
                    "departure": {"iataCode": "SFO", "at": "2026-08-06T20:00:00"},
                    "arrival": {"iataCode": "PLZ", "at": "2026-08-08T10:40:00"},
                    "carrierCode": "UA",
                    "number": "1",
                }
            ]
        },
        {
            "segments": [
                {
                    "departure": {"iataCode": "PLZ", "at": "2026-08-14T11:00:00"},
                    "arrival": {"iataCode": "SFO", "at": "2026-08-15T09:30:00"},
                    "carrierCode": "UA",
                    "number": "2",
                }
            ]
        },
    ],
    "price": {"currency": "USD", "total": "799.00", "grandTotal": "799.00"},
    "travelerPricings": [{"fareDetailsBySegment": [{"includedCheckedBags": {"quantity": 2}}]}],
}


class TestAmadeus:
    def make(self, handler) -> AmadeusProvider:  # type: ignore[no-untyped-def]
        s = Settings(amadeus_client_id="id", amadeus_client_secret="secret")
        return AmadeusProvider(
            TZ.get,
            s,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            backoff_seconds=0,
        )

    def test_oauth_then_search(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 1799})
            assert request.headers["Authorization"] == "Bearer tok"
            assert request.url.params["currencyCode"] == "USD"
            return httpx.Response(
                200,
                json={
                    "data": [AMADEUS_OFFER],
                    "dictionaries": {"carriers": {"UA": "UNITED AIRLINES"}},
                },
            )

        offers = self.make(handler).search(QUERY)
        assert calls == ["/v1/security/oauth2/token", "/v2/shopping/flight-offers"]
        o = offers[0]
        assert (
            o.airline_names == ["United Airlines"] and o.baggage == "2 checked bag(s) per traveller"
        )
        assert o.outbound.arrival_at == datetime(2026, 8, 8, 8, 40, tzinfo=UTC)
        assert o.raw == AMADEUS_OFFER  # kept for Flight Offers Price revalidation

    def test_unknown_airport_timezone_skips_offer(self) -> None:
        odd = json.loads(json.dumps(AMADEUS_OFFER))
        odd["itineraries"][0]["segments"][0]["arrival"]["iataCode"] = "ZZZ"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(200, json={"access_token": "tok"})
            return httpx.Response(200, json={"data": [odd]})

        assert self.make(handler).search(QUERY) == []

    def test_bad_credentials(self) -> None:
        with pytest.raises(PermanentProviderError, match="authentication"):
            self.make(lambda r: httpx.Response(401)).search(QUERY)


AIRPORTS = {
    "SFO": AirportInfo("SFO", 37.6213, -122.379, "America/Los_Angeles"),
    "PLZ": AirportInfo("PLZ", -33.9849, 25.6173, "Africa/Johannesburg"),
    "JNB": AirportInfo("JNB", -26.1392, 28.246, "Africa/Johannesburg"),
    "LHR": AirportInfo("LHR", 51.47, -0.4543, "Europe/London"),
    "DXB": AirportInfo("DXB", 25.2532, 55.3657, "Asia/Dubai"),
}


class TestDemoFlights:
    def test_mock_labelling_and_fictional_carriers(self) -> None:
        p = DemoFlightProvider(AIRPORTS.get)
        offers = p.search(QUERY)
        assert offers and all(o.is_mock and o.provider == "demo" for o in offers)
        codes = {c for c, _ in DEMO_CARRIERS}
        assert all(set(o.airlines) <= codes for o in offers)
        assert offers == sorted(offers, key=lambda o: o.total_price)

    def test_deterministic(self) -> None:
        a = DemoFlightProvider(AIRPORTS.get).search(QUERY)
        b = DemoFlightProvider(AIRPORTS.get).search(QUERY)
        assert [(o.provider_offer_id, o.total_price) for o in a] == [
            (o.provider_offer_id, o.total_price) for o in b
        ]

    def test_respects_max_connections(self) -> None:
        direct = FlightQuery("SFO", "PLZ", date(2026, 8, 6), date(2026, 8, 14), max_connections=0)
        assert all(
            o.outbound.stops == 0 and o.inbound.stops == 0
            for o in DemoFlightProvider(AIRPORTS.get).search(direct)
        )

    def test_unknown_airport(self) -> None:
        with pytest.raises(PermanentProviderError):
            DemoFlightProvider(AIRPORTS.get).search(
                FlightQuery("SFO", "ZZZ", date(2026, 8, 6), date(2026, 8, 14))
            )
