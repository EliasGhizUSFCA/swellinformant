"""Demo flight provider — clearly labelled MOCK offers for development and tests.

Offers are generated deterministically from the route and dates, with plausible
great-circle durations, real airport time zones (so date-line and overnight logic is
exercised) and fictional carriers ("Demo Air", codes D1–D5) so a mock offer can never be
mistaken for a real fare. Every offer has ``is_mock = True``.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.models.enums import CabinClass, OfferValidation
from app.services.flights.base import (
    FlightProvider,
    FlightQuery,
    NormalizedOffer,
    RevalidationResult,
    Segment,
    Slice,
)
from app.services.geo import haversine_km
from app.services.http import PermanentProviderError

DEMO_CARRIERS = [
    ("D1", "Demo Air"),
    ("D2", "Pacific Demo Airlines"),
    ("D3", "Atlantic Demo Airways"),
    ("D4", "Coral Demo Air"),
    ("D5", "Southern Demo Airlines"),
]
HUBS = [
    "LAX",
    "SFO",
    "HNL",
    "DFW",
    "JFK",
    "MIA",
    "LHR",
    "CDG",
    "FRA",
    "MAD",
    "LIS",
    "DXB",
    "DOH",
    "IST",
    "JNB",
    "SIN",
    "HKG",
    "NRT",
    "SYD",
    "AKL",
    "SCL",
    "LIM",
    "BOG",
    "PTY",
    "MEX",
    "DPS",
    "CGK",
    "MNL",
    "NAN",
    "PPT",
    "PER",
    "MEL",
    "CPT",
    "BNE",
]
DEPARTURE_SLOTS = [
    time(6, 15),
    time(8, 40),
    time(11, 5),
    time(13, 50),
    time(16, 25),
    time(19, 10),
    time(21, 45),
    time(23, 30),
]
CABIN_MULTIPLIER = {
    CabinClass.ECONOMY: 1.0,
    CabinClass.PREMIUM_ECONOMY: 1.7,
    CabinClass.BUSINESS: 3.6,
    CabinClass.FIRST: 5.5,
}
CRUISE_KMH = 820.0
DEMO_OFFER_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class AirportInfo:
    iata: str
    latitude: float
    longitude: float
    timezone: str


AirportLookup = Callable[[str], AirportInfo | None]


def _leg_minutes(km: float) -> int:
    minutes = (km / CRUISE_KMH + 0.6) * 60
    return int(round(minutes / 5) * 5)


class DemoFlightProvider(FlightProvider):
    name = "demo"
    is_mock = True
    supports_revalidation = True

    def __init__(
        self, airport_lookup: AirportLookup, now: Callable[[], datetime] | None = None
    ) -> None:
        self.lookup = airport_lookup
        self.now = now or (lambda: datetime.now(UTC))

    def _airport(self, code: str) -> AirportInfo:
        info = self.lookup(code)
        if info is None:
            raise PermanentProviderError(f"Demo provider has no data for airport {code}")
        return info

    def _route(
        self, rng: random.Random, a: AirportInfo, b: AirportInfo, stops: int
    ) -> list[AirportInfo]:
        if stops == 0:
            return [a, b]
        hubs = [h for code in HUBS if code not in (a.iata, b.iata) and (h := self.lookup(code))]
        direct = haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
        ranked = sorted(
            hubs,
            key=lambda h: (
                haversine_km(a.latitude, a.longitude, h.latitude, h.longitude)
                + haversine_km(h.latitude, h.longitude, b.latitude, b.longitude)
            ),
        )
        viable = [
            h
            for h in ranked[:6]
            if haversine_km(a.latitude, a.longitude, h.latitude, h.longitude)
            + haversine_km(h.latitude, h.longitude, b.latitude, b.longitude)
            < direct * 1.6 + 600
        ] or ranked[:2]
        first = rng.choice(viable[:3])
        if stops == 1:
            return [a, first, b]
        second_options = [h for h in ranked[:8] if h.iata != first.iata] or [first]
        second = rng.choice(second_options[:3])
        return [a, first, second, b]

    def _slice(
        self, rng: random.Random, path: list[AirportInfo], day: Any, carrier: tuple[str, str]
    ) -> Slice:
        slot = rng.choice(DEPARTURE_SLOTS)
        depart = datetime.combine(day, slot, tzinfo=ZoneInfo(path[0].timezone)).astimezone(UTC)
        segments: list[Segment] = []
        for a, b in zip(path, path[1:], strict=False):
            minutes = _leg_minutes(haversine_km(a.latitude, a.longitude, b.latitude, b.longitude))
            arrive = depart + timedelta(minutes=minutes)
            segments.append(
                Segment(
                    origin=a.iata,
                    destination=b.iata,
                    departure_at=depart,
                    arrival_at=arrive,
                    departure_local=depart.astimezone(ZoneInfo(a.timezone))
                    .replace(tzinfo=None)
                    .isoformat(),
                    arrival_local=arrive.astimezone(ZoneInfo(b.timezone))
                    .replace(tzinfo=None)
                    .isoformat(),
                    marketing_carrier=carrier[0],
                    carrier_name=carrier[1],
                    flight_number=f"{carrier[0]}{rng.randint(100, 999)}",
                )
            )
            depart = arrive + timedelta(minutes=int(rng.uniform(75, 270) // 5 * 5))
        return Slice(segments)

    def search(self, query: FlightQuery) -> list[NormalizedOffer]:
        origin, dest = self._airport(query.origin), self._airport(query.destination)
        dist = haversine_km(origin.latitude, origin.longitude, dest.latitude, dest.longitude)
        rng = random.Random(query.cache_key("demo"))
        offers: list[NormalizedOffer] = []
        for i in range(rng.randint(3, 8)):
            if dist < 2500 and rng.random() < 0.7 or dist < 9000 and rng.random() < 0.35:
                stops = 0
            else:
                stops = rng.choice([1, 1, 1, 2])
            stops = min(stops, query.max_connections)
            carrier = rng.choice(DEMO_CARRIERS)
            out = self._slice(
                rng, self._route(rng, origin, dest, stops), query.departure_date, carrier
            )
            back = self._slice(
                rng, self._route(rng, dest, origin, stops), query.return_date, carrier
            )
            per_person = (120 + 0.075 * dist) * rng.uniform(0.7, 1.55) * (0.85 if stops else 1.0)
            per_person *= CABIN_MULTIPLIER[query.cabin_class]
            total = Decimal(str(per_person * query.adults)).quantize(Decimal("0.01"), ROUND_HALF_UP)
            offers.append(
                NormalizedOffer(
                    provider=self.name,
                    provider_offer_id="demo_"
                    + hashlib.sha256(f"{query.cache_key('demo')}:{i}".encode()).hexdigest()[:16],
                    total_price=total,
                    currency=query.currency,
                    outbound=out,
                    inbound=back,
                    origin_timezone=origin.timezone,
                    destination_timezone=dest.timezone,
                    airlines=[carrier[0]],
                    airline_names=[carrier[1]],
                    baggage="1 checked, 1 carry-on per traveller (demo)",
                    expires_at=self.now() + DEMO_OFFER_TTL,
                    is_mock=True,
                    raw={"total_price": str(total), "currency": query.currency},
                )
            )
        return sorted(offers, key=lambda o: o.total_price)

    def revalidate(self, offer_id: str, payload: dict[str, Any] | None) -> RevalidationResult:
        if not payload:
            return RevalidationResult(OfferValidation.UNAVAILABLE)
        return RevalidationResult(
            OfferValidation.VALID,
            total_price=Decimal(payload["total_price"]),
            currency=payload["currency"],
            expires_at=self.now() + DEMO_OFFER_TTL,
        )
