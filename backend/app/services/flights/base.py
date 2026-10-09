"""Flight provider abstraction and normalised offer types.

Providers return local wall-clock times plus airport time zones; everything is
normalised to timezone-aware UTC instants here so date-line crossings, overnight
flights and DST are handled by arithmetic on real instants, never by date subtraction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.models.enums import CabinClass, OfferValidation
from app.services.http import PermanentProviderError

TzLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class FlightQuery:
    origin: str
    destination: str
    departure_date: date
    return_date: date
    adults: int = 1
    cabin_class: CabinClass = CabinClass.ECONOMY
    currency: str = "USD"
    max_connections: int = 1
    preferred_airlines: tuple[str, ...] = ()

    def cache_key(self, provider: str) -> str:
        return ":".join(
            [
                provider,
                self.origin,
                self.destination,
                self.departure_date.isoformat(),
                self.return_date.isoformat(),
                str(self.adults),
                self.cabin_class.value,
                self.currency,
                str(self.max_connections),
            ]
        )


@dataclass
class Segment:
    origin: str
    destination: str
    departure_at: datetime  # UTC
    arrival_at: datetime  # UTC
    departure_local: str  # ISO wall-clock time at origin
    arrival_local: str
    marketing_carrier: str
    carrier_name: str
    flight_number: str
    operating_carrier: str | None = None

    @property
    def duration_minutes(self) -> int:
        return int((self.arrival_at - self.departure_at).total_seconds() // 60)

    def as_dict(self, direction: str) -> dict[str, Any]:
        return {
            "direction": direction,
            "origin": self.origin,
            "destination": self.destination,
            "departure_at": self.departure_at.isoformat(),
            "arrival_at": self.arrival_at.isoformat(),
            "departure_local": self.departure_local,
            "arrival_local": self.arrival_local,
            "marketing_carrier": self.marketing_carrier,
            "carrier_name": self.carrier_name,
            "flight_number": self.flight_number,
            "operating_carrier": self.operating_carrier,
            "duration_minutes": self.duration_minutes,
        }


@dataclass
class Slice:
    segments: list[Segment]

    @property
    def departure_at(self) -> datetime:
        return self.segments[0].departure_at

    @property
    def arrival_at(self) -> datetime:
        return self.segments[-1].arrival_at

    @property
    def duration_minutes(self) -> int:
        return int((self.arrival_at - self.departure_at).total_seconds() // 60)

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination


@dataclass
class NormalizedOffer:
    provider: str
    provider_offer_id: str
    total_price: Decimal
    currency: str
    outbound: Slice
    inbound: Slice
    origin_timezone: str
    destination_timezone: str
    airlines: list[str] = field(default_factory=list)
    airline_names: list[str] = field(default_factory=list)
    baggage: str | None = None
    booking_url: str | None = None
    expires_at: datetime | None = None
    is_mock: bool = False
    raw: dict[str, Any] | None = None

    @property
    def total_duration_minutes(self) -> int:
        return self.outbound.duration_minutes + self.inbound.duration_minutes


@dataclass
class RevalidationResult:
    status: OfferValidation
    total_price: Decimal | None = None
    currency: str | None = None
    expires_at: datetime | None = None


class FlightProvider(ABC):
    name: str
    is_mock: bool = False
    supports_revalidation: bool = False

    @abstractmethod
    def search(self, query: FlightQuery) -> list[NormalizedOffer]: ...

    def revalidate(self, offer_id: str, payload: dict[str, Any] | None) -> RevalidationResult:
        return RevalidationResult(OfferValidation.UNVALIDATED)


def local_to_utc(local_iso: str, tz_name: str) -> datetime:
    """Interpret a provider's local wall-clock time in the airport's IANA zone."""
    naive = datetime.fromisoformat(local_iso)
    if naive.tzinfo is not None:
        return naive.astimezone(UTC)
    return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def require_tz(code: str, explicit: str | None, lookup: TzLookup) -> str:
    tz = explicit or lookup(code)
    if not tz:
        raise PermanentProviderError(f"unknown time zone for airport {code}")
    return tz


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
