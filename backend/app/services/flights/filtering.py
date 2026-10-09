"""Offer eligibility and ranking (pure functions over normalised offers).

Budget semantics: ``max_price`` is the maximum round-trip fare PER TRAVELLER in the
search currency; offers are compared on total_price / travellers. Offers quoted in a
different currency are only considered when FX_RATES_USD_JSON provides both rates, and
the conversion is recorded; otherwise they are rejected (never silently mis-compared).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models.enums import RankingPriority
from app.services.flights.base import NormalizedOffer
from app.services.flights.windows import TravelWindow


@dataclass(frozen=True)
class OfferConstraints:
    max_price_per_traveler: Decimal
    currency: str
    travelers: int
    max_layovers: int
    direct_only: bool
    max_flight_hours: float
    min_days_at_destination: int
    max_trip_days: int
    date_start: date | None = None  # fixed-date searches: earliest day to leave home
    date_end: date | None = None  # fixed-date searches: latest day to be back home
    preferred_airlines: tuple[str, ...] = ()


@dataclass
class EligibleOffer:
    offer: NormalizedOffer
    price_per_traveler: Decimal
    total_converted: Decimal
    affordability: float
    convenience: float
    rank_score: float = 0.0


@dataclass
class FilterResult:
    eligible: list[EligibleOffer] = field(default_factory=list)
    rejections: Counter[str] = field(default_factory=Counter)
    cheapest_rejected_per_traveler: Decimal | None = None

    def summary(self) -> str:
        if not self.rejections:
            return "no offers returned by the flight provider"
        parts = [f"{n}× {reason}" for reason, n in self.rejections.most_common(3)]
        return "no eligible offers (" + ", ".join(parts) + ")"


def convert(
    amount: Decimal, from_ccy: str, to_ccy: str, usd_rates: dict[str, float]
) -> Decimal | None:
    if from_ccy == to_ccy:
        return amount
    rates = {"USD": 1.0, **usd_rates}
    if from_ccy not in rates or to_ccy not in rates:
        return None
    usd = amount * Decimal(str(rates[from_ccy]))
    return (usd / Decimal(str(rates[to_ccy]))).quantize(Decimal("0.01"))


def affordability_score(price: Decimal, max_price: Decimal) -> float:
    """100 for a free flight, 50 at exactly the budget (only in-budget offers are scored)."""
    ratio = float(price / max_price) if max_price > 0 else 1.0
    return max(0.0, min(100.0, 100.0 - 50.0 * ratio))


def convenience_score(
    offer: NormalizedOffer, max_flight_hours: float, transfer_minutes: int
) -> float:
    longest = max(offer.outbound.duration_minutes, offer.inbound.duration_minutes) / 60.0
    stops = offer.outbound.stops + offer.inbound.stops
    score = 100.0 - 40.0 * min(1.0, longest / max(max_flight_hours, 1.0)) - 8.0 * stops
    score -= min(20.0, transfer_minutes / 30.0)
    return max(0.0, min(100.0, score))


def _local_date(dt: datetime, tz: str) -> date:
    return dt.astimezone(ZoneInfo(tz)).date()


def filter_offers(
    offers: list[NormalizedOffer],
    window: TravelWindow,
    c: OfferConstraints,
    *,
    now: datetime,
    usd_rates: dict[str, float] | None = None,
) -> FilterResult:
    result = FilterResult()
    max_stops = 0 if c.direct_only else c.max_layovers
    for offer in offers:
        reason: str | None = None
        total = convert(offer.total_price, offer.currency, c.currency, usd_rates or {})
        per_traveler = (
            (total / c.travelers).quantize(Decimal("0.01")) if total is not None else None
        )
        if offer.expires_at is not None and offer.expires_at <= now:
            reason = "offer expired"
        elif total is None or per_traveler is None:
            reason = f"quoted in {offer.currency} (no FX rate configured)"
        elif per_traveler > c.max_price_per_traveler:
            reason = "over budget"
        elif max(offer.outbound.stops, offer.inbound.stops) > max_stops:
            reason = "too many layovers"
        elif (
            max(offer.outbound.duration_minutes, offer.inbound.duration_minutes)
            > c.max_flight_hours * 60
        ):
            reason = "flight too long"
        elif not (window.arrive_after <= offer.outbound.arrival_at <= window.arrive_by):
            reason = "arrives outside the arrival window"
        elif not (window.depart_after <= offer.inbound.departure_at <= window.depart_before):
            reason = "returns outside the departure window"
        elif (
            c.date_start
            and _local_date(offer.outbound.departure_at, offer.origin_timezone) < c.date_start
        ):
            reason = "leaves home before your available dates"
        elif (
            c.date_end and _local_date(offer.inbound.arrival_at, offer.origin_timezone) > c.date_end
        ):
            reason = "returns home after your available dates"
        else:
            days = (
                _local_date(offer.inbound.departure_at, window.destination_tz)
                - _local_date(offer.outbound.arrival_at, window.destination_tz)
            ).days
            if days < c.min_days_at_destination:
                reason = "too few days at destination"
            elif days > c.max_trip_days:
                reason = "trip too long"
        if reason:
            result.rejections[reason] += 1
            if (
                reason == "over budget"
                and per_traveler is not None
                and (
                    result.cheapest_rejected_per_traveler is None
                    or per_traveler < result.cheapest_rejected_per_traveler
                )
            ):
                result.cheapest_rejected_per_traveler = per_traveler
            continue
        assert total is not None and per_traveler is not None
        result.eligible.append(
            EligibleOffer(
                offer=offer,
                price_per_traveler=per_traveler,
                total_converted=total,
                affordability=affordability_score(per_traveler, c.max_price_per_traveler),
                convenience=convenience_score(offer, c.max_flight_hours, window.transfer_minutes),
            )
        )
    return result


def rank_offers(
    eligible: list[EligibleOffer],
    priority: RankingPriority,
    preferred_airlines: tuple[str, ...] = (),
) -> list[EligibleOffer]:
    """Order offers for display. Preferred airlines get a small boost (soft preference)."""
    for e in eligible:
        bonus = (
            5.0 if preferred_airlines and set(e.offer.airlines) & set(preferred_airlines) else 0.0
        )
        if priority == RankingPriority.CHEAPEST:
            e.rank_score = e.affordability * 0.85 + e.convenience * 0.15 + bonus
        elif priority == RankingPriority.SHORTEST:
            e.rank_score = e.convenience * 0.85 + e.affordability * 0.15 + bonus
        else:
            e.rank_score = e.affordability * 0.55 + e.convenience * 0.45 + bonus
    return sorted(
        eligible,
        key=lambda e: (-e.rank_score, e.price_per_traveler, e.offer.total_duration_minutes),
    )
