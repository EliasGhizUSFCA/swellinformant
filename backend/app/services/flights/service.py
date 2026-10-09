"""Job E — search flights only for surf-matched opportunities.

Flight searches are expensive, so they run only after a swell matched a search, are
capped per match (FLIGHT_MAX_QUERIES_PER_MATCH) and per day (FLIGHT_DAILY_QUOTA), and
are cached by (provider, route, dates, passengers, cabin, currency) for
FLIGHT_CACHE_TTL_HOURS so many users chasing the same swell share one provider call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.redis import QuotaExceeded
from app.models import (
    FlightOffer,
    FlightSearch,
    OpportunityMatch,
    OpportunityOffer,
    SavedSearch,
)
from app.models.enums import (
    FlightSearchStatus,
    MatchStatus,
    OfferValidation,
    SearchStatus,
)
from app.services.flights.base import FlightProvider, FlightQuery, NormalizedOffer, Segment, Slice
from app.services.flights.filtering import OfferConstraints, filter_offers, rank_offers
from app.services.flights.registry import AirportDirectory, build_flight_provider
from app.services.flights.windows import (
    InfeasibleWindow,
    TravelWindow,
    compute_travel_window,
    outbound_query_dates,
)
from app.services.geo import haversine_km
from app.services.http import PermanentProviderError, ProviderError, TransientProviderError
from app.services.matching.scoring import opportunity_scores, surf_component

logger = logging.getLogger(__name__)

MAX_ORIGINS = 3
TOP_OFFERS = 5
FAILED_SEARCH_TTL = timedelta(hours=3)


@dataclass
class FlightStats:
    matches: int = 0
    provider_queries: int = 0
    cache_hits: int = 0
    flight_found: int = 0
    surf_only: int = 0
    errors: int = 0
    quota_exhausted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


# ---------------------------------------------------------------- persistence helpers
def offer_to_row(
    offer: NormalizedOffer, search: FlightSearch, adults: int, now: datetime
) -> FlightOffer:
    segments = [s.as_dict("outbound") for s in offer.outbound.segments] + [
        s.as_dict("inbound") for s in offer.inbound.segments
    ]
    return FlightOffer(
        flight_search_id=search.id,
        provider=offer.provider,
        provider_offer_id=offer.provider_offer_id,
        total_price=offer.total_price,
        currency=offer.currency,
        price_per_traveler=(offer.total_price / adults).quantize(Decimal("0.01")),
        airlines=offer.airlines,
        airline_names=offer.airline_names,
        origin_iata=offer.outbound.origin,
        destination_iata=offer.outbound.destination,
        outbound_departure_at=offer.outbound.departure_at,
        outbound_arrival_at=offer.outbound.arrival_at,
        outbound_duration_minutes=offer.outbound.duration_minutes,
        outbound_stops=offer.outbound.stops,
        inbound_departure_at=offer.inbound.departure_at,
        inbound_arrival_at=offer.inbound.arrival_at,
        inbound_duration_minutes=offer.inbound.duration_minutes,
        inbound_stops=offer.inbound.stops,
        origin_timezone=offer.origin_timezone,
        destination_timezone=offer.destination_timezone,
        segments=segments,
        baggage=offer.baggage,
        booking_url=offer.booking_url,
        offer_expires_at=offer.expires_at,
        quoted_at=now,
        is_mock=offer.is_mock,
        provider_payload=offer.raw,
        validation_status=OfferValidation.UNVALIDATED,
    )


def row_to_offer(row: FlightOffer) -> NormalizedOffer:
    def seg(d: dict[str, Any]) -> Segment:
        return Segment(
            origin=d["origin"],
            destination=d["destination"],
            departure_at=datetime.fromisoformat(d["departure_at"]),
            arrival_at=datetime.fromisoformat(d["arrival_at"]),
            departure_local=d["departure_local"],
            arrival_local=d["arrival_local"],
            marketing_carrier=d["marketing_carrier"],
            carrier_name=d["carrier_name"],
            flight_number=d["flight_number"],
            operating_carrier=d.get("operating_carrier"),
        )

    out = [seg(d) for d in row.segments if d["direction"] == "outbound"]
    back = [seg(d) for d in row.segments if d["direction"] == "inbound"]
    return NormalizedOffer(
        provider=row.provider,
        provider_offer_id=row.provider_offer_id,
        total_price=Decimal(row.total_price),
        currency=row.currency,
        outbound=Slice(out),
        inbound=Slice(back),
        origin_timezone=row.origin_timezone,
        destination_timezone=row.destination_timezone,
        airlines=list(row.airlines),
        airline_names=list(row.airline_names),
        baggage=row.baggage,
        booking_url=row.booking_url,
        expires_at=row.offer_expires_at,
        is_mock=row.is_mock,
    )


def get_or_search(
    db: Session, provider: FlightProvider, query: FlightQuery, now: datetime, stats: FlightStats
) -> FlightSearch:
    settings = get_settings()
    key = query.cache_key(provider.name)
    cached = db.scalar(
        select(FlightSearch)
        .where(FlightSearch.cache_key == key, FlightSearch.expires_at > now)
        .order_by(FlightSearch.searched_at.desc())
        .limit(1)
    )
    if cached is not None:
        stats.cache_hits += 1
        return cached
    stats.provider_queries += 1
    search = FlightSearch(
        provider=provider.name,
        cache_key=key,
        origin_iata=query.origin,
        destination_iata=query.destination,
        departure_date=query.departure_date,
        return_date=query.return_date,
        adults=query.adults,
        cabin_class=query.cabin_class,
        currency=query.currency,
        max_connections=query.max_connections,
        searched_at=now,
        is_mock=provider.is_mock,
    )
    try:
        offers = provider.search(query)
    except PermanentProviderError as exc:
        # e.g. unsupported route/airport: remember the failure briefly to avoid hammering.
        search.status = FlightSearchStatus.FAILED
        search.error_message = str(exc)[:1000]
        search.expires_at = now + FAILED_SEARCH_TTL
        db.add(search)
        db.flush()
        logger.warning("Flight search %s failed permanently: %s", key, exc)
        return search
    search.status = FlightSearchStatus.SUCCESS if offers else FlightSearchStatus.NO_RESULTS
    search.result_count = len(offers)
    search.expires_at = now + timedelta(hours=settings.flight_cache_ttl_hours)
    db.add(search)
    db.flush()
    seen: set[str] = set()
    for o in offers:
        if o.provider_offer_id in seen:
            continue
        seen.add(o.provider_offer_id)
        db.add(offer_to_row(o, search, query.adults, now))
    db.flush()
    return search


# ---------------------------------------------------------------- per-match logic
def origin_airports(
    search: SavedSearch, directory: AirportDirectory, destination: str
) -> list[str]:
    origins = [o.airport_iata for o in search.origins if o.airport_iata != destination]
    if search.max_origin_ground_km and origins:
        base = [directory.get(c) for c in origins]
        nearby: list[tuple[float, str]] = []
        for info in directory.all():
            if info.iata in origins or info.iata == destination:
                continue
            dist = min(
                (
                    haversine_km(b.latitude, b.longitude, info.latitude, info.longitude)
                    for b in base
                    if b
                ),
                default=1e9,
            )
            if dist <= search.max_origin_ground_km:
                nearby.append((dist, info.iata))
        origins += [code for _, code in sorted(nearby)]
    return origins[:MAX_ORIGINS]


def plan_queries(
    search: SavedSearch, window: TravelWindow, directory: AirportDirectory, origins: list[str]
) -> list[FlightQuery]:
    settings = get_settings()
    travel = search.travel
    dest = directory.get(window.destination_iata)
    per_origin: list[list[FlightQuery]] = []
    for code in origins:
        info = directory.get(code)
        if info is None or dest is None:
            continue
        dist = haversine_km(info.latitude, info.longitude, dest.latitude, dest.longitude)
        dates = outbound_query_dates(
            window, info.timezone, dist, travel.max_flight_hours, max_dates=2
        )
        per_origin.append(
            [
                FlightQuery(
                    origin=code,
                    destination=window.destination_iata,
                    departure_date=d,
                    return_date=window.recommended_departure_date,
                    adults=travel.travelers,
                    cabin_class=travel.cabin_class,
                    currency=travel.currency,
                    max_connections=0 if travel.direct_only else min(travel.max_layovers, 2),
                    preferred_airlines=tuple(travel.preferred_airlines or ()),
                )
                for d in dates
            ]
        )
    plan: list[FlightQuery] = []
    for round_idx in range(2):  # first choice for every origin, then second choices
        for queries in per_origin:
            if round_idx < len(queries):
                plan.append(queries[round_idx])
    return plan[: settings.flight_max_queries_per_match]


def _set_surf_only(match: OpportunityMatch, reason: str, now: datetime) -> None:
    match.status = MatchStatus.SURF_ONLY
    match.status_reason = reason
    match.best_offer = None
    match.best_price = None
    match.origin_iata = None
    match.offers.clear()
    components, overall = opportunity_scores(
        match.search.priority,
        surf=surf_component(match.peak_score, match.avg_score),
        confidence=match.confidence,
        affordability=None,
        convenience=None,
    )
    match.scores = components
    match.overall_score = overall
    match.flight_checked_at = now


def process_match(
    db: Session,
    provider: FlightProvider,
    directory: AirportDirectory,
    match: OpportunityMatch,
    now: datetime,
    stats: FlightStats,
) -> None:
    settings = get_settings()
    search = match.search
    travel = search.travel
    if not match.destination_iata:
        _set_surf_only(match, match.status_reason or "no practical destination airport", now)
        return
    dest_tz = directory.tz(match.destination_iata) or match.spot.timezone
    try:
        window = compute_travel_window(
            surf_start=match.window_start,
            surf_end=match.window_end,
            spot_tz=match.spot.timezone,
            destination_iata=match.destination_iata,
            destination_tz=dest_tz,
            transfer_minutes=match.transfer_minutes or 0,
            arrival_buffer_days=travel.arrival_buffer_days,
            departure_buffer_days=travel.departure_buffer_days,
            min_days_at_destination=travel.min_days_at_destination,
            max_trip_days=travel.max_trip_days,
            now=now,
        )
    except InfeasibleWindow as exc:
        _set_surf_only(match, str(exc), now)
        return
    origins = origin_airports(search, directory, match.destination_iata)
    if not origins:
        _set_surf_only(match, "no departure airport configured for this search", now)
        return
    plan = plan_queries(search, window, directory, origins)
    if not plan:
        _set_surf_only(match, "no departure dates can reach the arrival window", now)
        return

    offers: list[tuple[NormalizedOffer, FlightOffer]] = []
    errors: list[str] = []
    for query in plan:
        fs = get_or_search(db, provider, query, now, stats)
        if fs.status == FlightSearchStatus.FAILED:
            errors.append(fs.error_message or "search failed")
            continue
        for row in db.scalars(
            select(FlightOffer).where(FlightOffer.flight_search_id == fs.id)
        ).all():
            offers.append((row_to_offer(row), row))

    constraints = OfferConstraints(
        max_price_per_traveler=Decimal(travel.max_price),
        currency=travel.currency,
        travelers=travel.travelers,
        max_layovers=travel.max_layovers,
        direct_only=travel.direct_only,
        max_flight_hours=travel.max_flight_hours,
        min_days_at_destination=travel.min_days_at_destination,
        max_trip_days=travel.max_trip_days,
        date_start=search.date_start if search.date_mode == "fixed" else None,
        date_end=search.date_end if search.date_mode == "fixed" else None,
        preferred_airlines=tuple(travel.preferred_airlines or ()),
    )
    by_id = {id(o): row for o, row in offers}
    result = filter_offers(
        [o for o, _ in offers], window, constraints, now=now, usd_rates=settings.fx_rates_usd
    )
    ranked = rank_offers(result.eligible, search.priority, constraints.preferred_airlines)
    match.recommended_arrival_date = window.recommended_arrival_date
    match.recommended_departure_date = window.recommended_departure_date
    match.flight_search_error = "; ".join(errors)[:1000] if errors else None
    if not ranked:
        reason = (
            result.summary()
            if offers
            else (
                "flight search failed: " + errors[0]
                if errors
                else "no offers returned by the flight provider"
            )
        )
        if result.cheapest_rejected_per_traveler is not None:
            cheapest = result.cheapest_rejected_per_traveler
            reason += f"; cheapest found {cheapest} {travel.currency} per traveller"
        _set_surf_only(match, reason, now)
        stats.surf_only += 1
        return

    match.offers.clear()
    db.flush()
    for rank, e in enumerate(ranked[:TOP_OFFERS], start=1):
        row = by_id[id(e.offer)]
        match.offers.append(
            OpportunityOffer(
                offer_id=row.id,
                rank=rank,
                score=round(e.rank_score, 2),
                price_converted=e.price_per_traveler,
            )
        )
    best = ranked[0]
    best_row = by_id[id(best.offer)]
    match.best_offer = best_row  # set the relationship, not just the FK, so it is never stale
    match.best_price = best.price_per_traveler
    match.currency = travel.currency
    match.origin_iata = best.offer.outbound.origin
    components, overall = opportunity_scores(
        search.priority,
        surf=surf_component(match.peak_score, match.avg_score),
        confidence=match.confidence,
        affordability=best.affordability,
        convenience=best.convenience,
    )
    match.scores = components
    match.overall_score = overall
    match.status = MatchStatus.FLIGHT_FOUND
    match.status_reason = None
    match.flight_checked_at = now
    stats.flight_found += 1


def matches_needing_flights(
    db: Session, now: datetime, limit: int, match_ids: list[Any] | None = None
) -> list[OpportunityMatch]:
    settings = get_settings()
    stale_before = now - timedelta(hours=settings.flight_refresh_hours)
    stmt = (
        select(OpportunityMatch)
        .join(SavedSearch, SavedSearch.id == OpportunityMatch.search_id)
        .where(
            SavedSearch.status == SearchStatus.ACTIVE,
            OpportunityMatch.window_start > now,
            or_(
                OpportunityMatch.status == MatchStatus.PENDING_FLIGHTS,
                (
                    OpportunityMatch.status.in_((MatchStatus.FLIGHT_FOUND, MatchStatus.SURF_ONLY))
                    & OpportunityMatch.flight_checked_at.is_not(None)
                    & (OpportunityMatch.flight_checked_at < stale_before)
                ),
                (
                    OpportunityMatch.flights_requested_at.is_not(None)
                    & (
                        OpportunityMatch.flight_checked_at.is_(None)
                        | (
                            OpportunityMatch.flights_requested_at
                            > OpportunityMatch.flight_checked_at
                        )
                    )
                    & OpportunityMatch.status.notin_((MatchStatus.EXPIRED, MatchStatus.DISMISSED))
                ),
            ),
        )
        .order_by(
            (OpportunityMatch.status != MatchStatus.PENDING_FLIGHTS),
            OpportunityMatch.overall_score.desc(),
        )
        .limit(limit)
    )
    if match_ids:
        stmt = stmt.where(OpportunityMatch.id.in_(match_ids))
    return list(db.scalars(stmt).unique().all())


def discover_flights(
    db: Session, now: datetime | None = None, limit: int = 50, match_ids: list[Any] | None = None
) -> FlightStats:
    now = now or utcnow()
    stats = FlightStats()
    matches = matches_needing_flights(db, now, limit, match_ids)
    if not matches:
        return stats
    provider = build_flight_provider(db)
    directory = AirportDirectory(db)
    for match in matches:
        stats.matches += 1
        try:
            process_match(db, provider, directory, match, now, stats)
            db.commit()
        except QuotaExceeded as exc:
            db.rollback()
            stats.quota_exhausted = True
            logger.warning("Stopping flight discovery: %s", exc)
            break
        except TransientProviderError as exc:
            db.rollback()
            stats.errors += 1
            match.flight_search_error = f"temporary provider error: {exc}"[:1000]
            db.commit()
            logger.warning("Transient flight provider error for match %s: %s", match.id, exc)
        except ProviderError as exc:
            db.rollback()
            stats.errors += 1
            _set_surf_only(match, f"flight provider error: {exc}", now)
            db.commit()
            logger.error("Flight provider error for match %s: %s", match.id, exc)
    logger.info("Flight discovery: %s", stats.as_dict())
    return stats


def revalidate_offer(
    db: Session, provider: FlightProvider, offer: FlightOffer, now: datetime
) -> OfferValidation:
    """Re-price an offer with its provider before alerting; never fabricates a price."""
    if offer.offer_expires_at is not None and offer.offer_expires_at <= now:
        offer.validation_status = OfferValidation.UNAVAILABLE
        offer.last_validated_at = now
        return offer.validation_status
    if provider.name != offer.provider or not provider.supports_revalidation:
        return offer.validation_status
    result = provider.revalidate(offer.provider_offer_id, offer.provider_payload)
    offer.last_validated_at = now
    if result.status == OfferValidation.VALID and result.total_price is not None:
        if result.total_price != offer.total_price or (
            result.currency and result.currency != offer.currency
        ):
            offer.total_price = result.total_price
            offer.currency = result.currency or offer.currency
            offer.price_per_traveler = (
                result.total_price / max(1, offer.flight_search.adults)
            ).quantize(Decimal("0.01"))
            offer.validation_status = OfferValidation.PRICE_CHANGED
        else:
            offer.validation_status = OfferValidation.VALID
        if result.expires_at:
            offer.offer_expires_at = result.expires_at
    else:
        offer.validation_status = result.status
    return offer.validation_status
