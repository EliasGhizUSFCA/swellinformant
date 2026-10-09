"""Amadeus Flight Offers Search adapter — for Amadeus *Enterprise* customers only.

The Amadeus Self-Service portal (test.api.amadeus.com) was decommissioned on 2026-07-17
and self-service keys no longer work. Enterprise contracts keep the same Flight Offers
Search v2 / Flight Offers Price v1 shapes on the host given by AMADEUS_BASE_URL.

* POST /v1/security/oauth2/token           (client credentials)
* GET  /v2/shopping/flight-offers          (search)
* POST /v1/shopping/flight-offers/pricing  (revalidate; needs the original offer object)

Amadeus returns local times without zone information, so the airport time zone comes from
our airports table (``tz_lookup``); offers touching an unknown airport are skipped.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.models.enums import OfferValidation
from app.services.flights.base import (
    FlightProvider,
    FlightQuery,
    NormalizedOffer,
    RevalidationResult,
    Segment,
    Slice,
    TzLookup,
    local_to_utc,
    require_tz,
)
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)

CABINS = {
    "economy": "ECONOMY",
    "premium_economy": "PREMIUM_ECONOMY",
    "business": "BUSINESS",
    "first": "FIRST",
}


class AmadeusProvider(FlightProvider):
    name = "amadeus"
    supports_revalidation = True

    def __init__(
        self,
        tz_lookup: TzLookup,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        if not (self.settings.amadeus_client_id and self.settings.amadeus_client_secret):
            raise PermanentProviderError("AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not configured")
        self.tz_lookup = tz_lookup
        self.base = self.settings.amadeus_base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=self.settings.flight_http_timeout_seconds)
        self.backoff_seconds = backoff_seconds
        self._token: str | None = None
        self._token_expiry = 0.0

    def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() > self._token_expiry - 60:
            resp = request_with_retries(
                self.client,
                "POST",
                f"{self.base}/v1/security/oauth2/token",
                provider="amadeus",
                backoff_seconds=self.backoff_seconds,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.amadeus_client_id,
                    "client_secret": self.settings.amadeus_client_secret,
                },
            )
            if resp.status_code != 200:
                raise PermanentProviderError(
                    f"Amadeus authentication failed (HTTP {resp.status_code})"
                )
            data = resp.json()
            self._token = data["access_token"]
            self._token_expiry = time.monotonic() + int(data.get("expires_in", 1799))
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _error(resp: httpx.Response) -> ProviderError:
        try:
            errors = resp.json().get("errors") or []
            msg = "; ".join(f"{e.get('title', '')} {e.get('detail', '')}".strip() for e in errors)
        except ValueError:
            msg = resp.text[:200]
        if resp.status_code >= 500:
            return TransientProviderError(f"Amadeus HTTP {resp.status_code}: {msg}")
        return PermanentProviderError(f"Amadeus HTTP {resp.status_code}: {msg}")

    def search(self, query: FlightQuery) -> list[NormalizedOffer]:
        params: dict[str, Any] = {
            "originLocationCode": query.origin,
            "destinationLocationCode": query.destination,
            "departureDate": query.departure_date.isoformat(),
            "returnDate": query.return_date.isoformat(),
            "adults": query.adults,
            "travelClass": CABINS[query.cabin_class.value],
            "currencyCode": query.currency,
            "max": self.settings.flight_max_offers_per_search,
        }
        if query.max_connections == 0:
            params["nonStop"] = "true"
        if query.preferred_airlines:
            params["includedAirlineCodes"] = ",".join(query.preferred_airlines)
        resp = request_with_retries(
            self.client,
            "GET",
            f"{self.base}/v2/shopping/flight-offers",
            provider="amadeus",
            quota=self.settings.flight_daily_quota,
            backoff_seconds=self.backoff_seconds,
            params=params,
            headers=self._auth_header(),
        )
        if resp.status_code != 200:
            raise self._error(resp)
        body = resp.json()
        carriers = (body.get("dictionaries") or {}).get("carriers") or {}
        offers: list[NormalizedOffer] = []
        for raw in body.get("data") or []:
            try:
                offers.append(self.normalize(raw, carriers))
            except (ProviderError, KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping Amadeus offer %s: %s", raw.get("id"), exc)
        return offers

    def _segment(self, seg: dict[str, Any], carriers: dict[str, str]) -> Segment:
        dep, arr = seg["departure"], seg["arrival"]
        d_tz = require_tz(dep["iataCode"], None, self.tz_lookup)
        a_tz = require_tz(arr["iataCode"], None, self.tz_lookup)
        code = seg["carrierCode"]
        return Segment(
            origin=dep["iataCode"],
            destination=arr["iataCode"],
            departure_at=local_to_utc(dep["at"], d_tz),
            arrival_at=local_to_utc(arr["at"], a_tz),
            departure_local=dep["at"],
            arrival_local=arr["at"],
            marketing_carrier=code,
            carrier_name=str(carriers.get(code, code)).title(),
            flight_number=f"{code}{seg.get('number', '')}",
            operating_carrier=(seg.get("operating") or {}).get("carrierCode"),
        )

    def normalize(self, raw: dict[str, Any], carriers: dict[str, str]) -> NormalizedOffer:
        itineraries = raw["itineraries"]
        if len(itineraries) != 2:
            raise ValueError("expected a round trip")
        out = Slice([self._segment(s, carriers) for s in itineraries[0]["segments"]])
        back = Slice([self._segment(s, carriers) for s in itineraries[1]["segments"]])
        codes: dict[str, str] = {}
        for sl in (out, back):
            for seg in sl.segments:
                codes.setdefault(seg.marketing_carrier, seg.carrier_name)
        bags = None
        try:
            fare = raw["travelerPricings"][0]["fareDetailsBySegment"][0]
            qty = (fare.get("includedCheckedBags") or {}).get("quantity")
            if qty is not None:
                bags = f"{qty} checked bag(s) per traveller"
        except (KeyError, IndexError, TypeError):
            pass
        price = raw["price"]
        return NormalizedOffer(
            provider=self.name,
            provider_offer_id=str(raw["id"]) + ":" + str(raw.get("source", "")),
            total_price=Decimal(str(price.get("grandTotal") or price["total"])),
            currency=str(price["currency"]).upper(),
            outbound=out,
            inbound=back,
            origin_timezone=require_tz(out.origin, None, self.tz_lookup),
            destination_timezone=require_tz(out.destination, None, self.tz_lookup),
            airlines=list(codes),
            airline_names=list(codes.values()),
            baggage=bags,
            expires_at=None,
            raw=raw,
        )

    def revalidate(self, offer_id: str, payload: dict[str, Any] | None) -> RevalidationResult:
        if not payload:
            return RevalidationResult(OfferValidation.UNVALIDATED)
        resp = request_with_retries(
            self.client,
            "POST",
            f"{self.base}/v1/shopping/flight-offers/pricing",
            provider="amadeus",
            quota=self.settings.flight_daily_quota,
            backoff_seconds=self.backoff_seconds,
            headers=self._auth_header(),
            json={"data": {"type": "flight-offers-pricing", "flightOffers": [payload]}},
        )
        if resp.status_code in (400, 404, 422):
            return RevalidationResult(OfferValidation.UNAVAILABLE)
        if resp.status_code != 200:
            raise self._error(resp)
        offers = (resp.json().get("data") or {}).get("flightOffers") or []
        if not offers:
            return RevalidationResult(OfferValidation.UNAVAILABLE)
        price = offers[0]["price"]
        return RevalidationResult(
            OfferValidation.VALID,
            total_price=Decimal(str(price.get("grandTotal") or price["total"])),
            currency=str(price["currency"]).upper(),
        )
