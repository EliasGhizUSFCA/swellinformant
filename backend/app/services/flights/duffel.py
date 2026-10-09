"""Duffel Flights API adapter (primary live provider).

Endpoints used (API version header ``Duffel-Version: v2``):
* POST /air/offer_requests?return_offers=true&supplier_timeout=…  — search
* GET  /air/offers/{id}                                          — re-price / revalidate

Duffel returns segment times as local wall-clock times with each airport's IANA
``time_zone``; we convert them to UTC instants. Duffel does not expose consumer booking
URLs; bookings go through Duffel's Orders API or Duffel Links (not part of v1 of this app),
so offers carry an inspection link instead (see links.py).
"""

from __future__ import annotations

import logging
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
    parse_iso_utc,
    require_tz,
)
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)


class DuffelProvider(FlightProvider):
    name = "duffel"
    supports_revalidation = True

    def __init__(
        self,
        tz_lookup: TzLookup,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.duffel_access_token:
            raise PermanentProviderError("DUFFEL_ACCESS_TOKEN is not configured")
        self.tz_lookup = tz_lookup
        self.base = self.settings.duffel_base_url.rstrip("/")
        self.backoff_seconds = backoff_seconds
        self.client = client or httpx.Client(timeout=self.settings.flight_http_timeout_seconds)
        self.headers = {
            "Authorization": f"Bearer {self.settings.duffel_access_token}",
            "Duffel-Version": "v2",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }

    def _raise_for(self, resp: httpx.Response) -> None:
        try:
            errors = resp.json().get("errors") or []
            message = (
                "; ".join(e.get("message") or e.get("title") or "" for e in errors)
                or resp.text[:200]
            )
        except ValueError:
            message = resp.text[:200]
        if resp.status_code in (401, 403):
            raise PermanentProviderError(f"Duffel authentication failed: {message}")
        if resp.status_code >= 500:
            raise TransientProviderError(f"Duffel HTTP {resp.status_code}: {message}")
        raise PermanentProviderError(f"Duffel HTTP {resp.status_code}: {message}")

    def search(self, query: FlightQuery) -> list[NormalizedOffer]:
        body = {
            "data": {
                "slices": [
                    {
                        "origin": query.origin,
                        "destination": query.destination,
                        "departure_date": query.departure_date.isoformat(),
                    },
                    {
                        "origin": query.destination,
                        "destination": query.origin,
                        "departure_date": query.return_date.isoformat(),
                    },
                ],
                "passengers": [{"type": "adult"} for _ in range(query.adults)],
                "cabin_class": query.cabin_class.value,
                "max_connections": query.max_connections,
            }
        }
        resp = request_with_retries(
            self.client,
            "POST",
            f"{self.base}/air/offer_requests",
            provider="duffel",
            quota=self.settings.flight_daily_quota,
            backoff_seconds=self.backoff_seconds,
            params={"return_offers": "true", "supplier_timeout": "20000"},
            headers=self.headers,
            json=body,
        )
        if resp.status_code not in (200, 201):
            self._raise_for(resp)
        offers_raw = (resp.json().get("data") or {}).get("offers") or []
        offers: list[NormalizedOffer] = []
        for raw in offers_raw[: self.settings.flight_max_offers_per_search]:
            try:
                offers.append(self.normalize(raw))
            except (ProviderError, KeyError, ValueError, TypeError) as exc:
                logger.warning("Skipping malformed Duffel offer %s: %s", raw.get("id"), exc)
        return offers

    def _segment(self, seg: dict[str, Any]) -> Segment:
        origin, dest = seg["origin"], seg["destination"]
        o_tz = require_tz(origin["iata_code"], origin.get("time_zone"), self.tz_lookup)
        d_tz = require_tz(dest["iata_code"], dest.get("time_zone"), self.tz_lookup)
        marketing = seg.get("marketing_carrier") or {}
        operating = seg.get("operating_carrier") or {}
        return Segment(
            origin=origin["iata_code"],
            destination=dest["iata_code"],
            departure_at=local_to_utc(seg["departing_at"], o_tz),
            arrival_at=local_to_utc(seg["arriving_at"], d_tz),
            departure_local=seg["departing_at"],
            arrival_local=seg["arriving_at"],
            marketing_carrier=marketing.get("iata_code") or "",
            carrier_name=marketing.get("name") or "",
            flight_number=(
                f"{marketing.get('iata_code', '')}{seg.get('marketing_carrier_flight_number', '')}"
            ),
            operating_carrier=operating.get("iata_code"),
        )

    @staticmethod
    def _baggage(raw: dict[str, Any]) -> str | None:
        checked = carry = 0
        for sl in raw.get("slices", [])[:1]:
            for seg in sl.get("segments", [])[:1]:
                for pax in seg.get("passengers", [])[:1]:
                    for bag in pax.get("baggages", []):
                        if bag.get("type") == "checked":
                            checked += int(bag.get("quantity", 0))
                        elif bag.get("type") == "carry_on":
                            carry += int(bag.get("quantity", 0))
        if not (checked or carry):
            return None
        return f"{checked} checked, {carry} carry-on per traveller"

    def normalize(self, raw: dict[str, Any]) -> NormalizedOffer:
        slices = raw["slices"]
        if len(slices) != 2:
            raise ValueError("expected a round trip with two slices")
        out = Slice([self._segment(s) for s in slices[0]["segments"]])
        back = Slice([self._segment(s) for s in slices[1]["segments"]])
        carriers: dict[str, str] = {}
        for sl in (out, back):
            for seg in sl.segments:
                carriers.setdefault(seg.marketing_carrier, seg.carrier_name)
        owner = raw.get("owner") or {}
        return NormalizedOffer(
            provider=self.name,
            provider_offer_id=raw["id"],
            total_price=Decimal(str(raw["total_amount"])),
            currency=str(raw["total_currency"]).upper(),
            outbound=out,
            inbound=back,
            origin_timezone=require_tz(
                out.origin, slices[0]["segments"][0]["origin"].get("time_zone"), self.tz_lookup
            ),
            destination_timezone=require_tz(
                out.destination,
                slices[0]["segments"][-1]["destination"].get("time_zone"),
                self.tz_lookup,
            ),
            airlines=list(carriers) or ([owner["iata_code"]] if owner.get("iata_code") else []),
            airline_names=list(carriers.values()) or ([owner.get("name", "")] if owner else []),
            baggage=self._baggage(raw),
            expires_at=parse_iso_utc(raw.get("expires_at")),
            is_mock=False,
            raw=None,
        )

    def revalidate(self, offer_id: str, payload: dict[str, Any] | None) -> RevalidationResult:
        resp = request_with_retries(
            self.client,
            "GET",
            f"{self.base}/air/offers/{offer_id}",
            provider="duffel",
            quota=self.settings.flight_daily_quota,
            backoff_seconds=self.backoff_seconds,
            headers=self.headers,
        )
        if resp.status_code in (404, 410, 422):
            return RevalidationResult(OfferValidation.UNAVAILABLE)
        if resp.status_code != 200:
            self._raise_for(resp)
        data = resp.json().get("data") or {}
        return RevalidationResult(
            OfferValidation.VALID,
            total_price=Decimal(str(data["total_amount"])),
            currency=str(data["total_currency"]).upper(),
            expires_at=parse_iso_utc(data.get("expires_at")),
        )
