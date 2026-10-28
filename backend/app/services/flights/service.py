"""Job E — search flights only when a search is justified (spec 26/27, design D15).

A provider is asked for flights only for:

* a **new** surf-qualified match (status PENDING_FLIGHTS, set by matching),
* a **material change** of a match: its recommended travel dates or destination airport
  moved, or its forecast crossed back over the search's quality threshold (matching sets
  the match back to PENDING_FLIGHTS; app.services.matching.service.evaluate_match),
* an **explicit user request** ("Re-check flights", ``flights_requested_at``),
* a price re-check right before an alert is actually sent (notifications/alerts.py; an
  unusable fare sends the match back here once).

There is no timer and no periodic re-search: a match whose forecast did not change is never
searched again. Every search is capped per match (one first-choice date for every origin,
up to 5, then second-choice dates up to FLIGHT_MAX_QUERIES_PER_MATCH), per user and day
(FLIGHT_USER_DAILY_QUOTA, app.services.flights.budget) and per provider and day
(FLIGHT_DAILY_QUOTA). Results are cached by (provider, route, dates, passengers, cabin,
currency, connections) until the earlier of FLIGHT_CACHE_TTL_HOURS and the first offer's
own expiry, so many users chasing the same swell share one provider call and an expired
quote is never served from the cache. Users are served round-robin, explicit requests and
short-notice swells first.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, lazyload, selectinload

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
from app.services.flights.airports import gateway_fallbacks
from app.services.flights.base import FlightProvider, FlightQuery, NormalizedOffer, Segment, Slice
from app.services.flights.budget import UserFlightBudgetExceeded, consume_user_search
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
from app.services.matching.scoring import (
    match_surf_component,
    opportunity_scores,
    ranking_score,
)

logger = logging.getLogger(__name__)

# Loader options of the matches flights are discovered for: their event and best offer are
# never read here (the best offer is only replaced), so they are not joined into every row;
# their current offers are read for all matches in one statement, without the flight offer
# each one joins (a match's offers are only cleared and replaced).
MATCH_FOR_FLIGHTS = (
    lazyload(OpportunityMatch.swell_event),
    lazyload(OpportunityMatch.best_offer),
    selectinload(OpportunityMatch.offers).lazyload(OpportunityOffer.offer),
)

# Origins searched per match: the schema allows up to 5 home airports and every one of
# them gets its first-choice date (FLIGHT_MAX_QUERIES_PER_MATCH only limits the extra,
# second-choice dates).
MAX_ORIGINS = 5
# Connections per direction a provider is asked for (the search allows 0-3 layovers).
MAX_CONNECTIONS = 3
TOP_OFFERS = 5
FAILED_SEARCH_TTL = timedelta(hours=3)
# matches_needing_flights reads up to this many times ``limit`` candidates before ordering.
CANDIDATE_FACTOR = 10


@dataclass
class FlightStats:
    matches: int = 0
    provider_queries: int = 0
    cache_hits: int = 0
    flight_found: int = 0
    surf_only: int = 0
    errors: int = 0
    quota_exhausted: bool = False
    # Matches left PENDING because their user's daily flight-search budget is used up.
    budget_deferred: int = 0
    # Matches searched again via an international gateway (REGIONAL_AIRPORTS).
    gateway_fallbacks: int = 0

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
            operating_carrier_name=d.get("operating_carrier_name"),
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


def cache_expiry(offers: list[NormalizedOffer], now: datetime, ttl: timedelta) -> datetime:
    """When a search's results stop being served from the cache: after ``ttl``
    (FLIGHT_CACHE_TTL_HOURS), or earlier, when the first of its offers expires (Duffel
    offers last about 30 minutes), so a cache hit never holds only expired quotes."""
    expiry = now + ttl
    for offer in offers:
        if offer.expires_at is not None and offer.expires_at < expiry:
            expiry = max(now, offer.expires_at)
    return expiry


def get_or_search(
    db: Session,
    provider: FlightProvider,
    query: FlightQuery,
    now: datetime,
    stats: FlightStats,
    *,
    user_id: object | None = None,
) -> FlightSearch:
    """The cached search for ``query`` (shared by every user), or a new provider search.

    A provider search counts against ``user_id``'s daily budget first
    (UserFlightBudgetExceeded is raised before the provider is called); a cache hit is free.
    """
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
    if user_id is not None:
        consume_user_search(user_id, settings.flight_user_daily_quota, now)
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
    search.expires_at = cache_expiry(offers, now, timedelta(hours=settings.flight_cache_ttl_hours))
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


def per_match_budget(origins: int, configured: int) -> int:
    """Provider queries one match may plan: a first-choice date for every origin (up to
    MAX_ORIGINS), and FLIGHT_MAX_QUERIES_PER_MATCH in all when that is more."""
    return max(configured, min(origins, MAX_ORIGINS))


def max_connections(travel: object) -> int:
    """The connections per direction a provider is asked for: 0 for direct-only searches,
    else the search's maximum layovers (0-3), passed through unchanged."""
    if getattr(travel, "direct_only", False):
        return 0
    return max(0, min(int(getattr(travel, "max_layovers", 0)), MAX_CONNECTIONS))


def plan_queries(
    search: SavedSearch,
    window: TravelWindow,
    directory: AirportDirectory,
    origins: list[str],
    *,
    first_choice_only: bool = False,
) -> list[FlightQuery]:
    """The queries for one match: the first-choice date of every origin, then second
    choices, within :func:`per_match_budget` (``first_choice_only``: no second choices)."""
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
                    max_connections=max_connections(travel),
                    preferred_airlines=tuple(travel.preferred_airlines or ()),
                )
                for d in dates
            ]
        )
    plan: list[FlightQuery] = []
    for round_idx in range(1 if first_choice_only else 2):
        # First choice for every origin, then second choices.
        for queries in per_origin:
            if round_idx < len(queries):
                plan.append(queries[round_idx])
    return plan[: per_match_budget(len(per_origin), settings.flight_max_queries_per_match)]


def _set_surf_only(match: OpportunityMatch, reason: str, now: datetime) -> None:
    match.status = MatchStatus.SURF_ONLY
    match.status_reason = reason
    match.best_offer = None
    match.best_price = None
    match.origin_iata = None
    match.offers.clear()
    components, overall = opportunity_scores(
        match.search.priority,
        surf=_surf(match),
        confidence=match.confidence,
        affordability=None,
        convenience=None,
    )
    match.scores = components
    match.overall_score = overall
    match.flight_checked_at = now


def _surf(match: OpportunityMatch) -> float:
    return match_surf_component(
        match.peak_score,
        match.avg_score,
        match.breaking_height_min_ft,
        match.breaking_height_max_ft,
        match.search,
    )


@dataclass
class _Attempt:
    """The offers found for one destination airport of a match."""

    window: TravelWindow
    destination_iata: str
    transfer_minutes: int
    planned: int
    searched: int = 0
    offers: list[tuple[NormalizedOffer, FlightOffer]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_exhausted: bool = False


def _attempt(
    db: Session,
    provider: FlightProvider,
    directory: AirportDirectory,
    match: OpportunityMatch,
    destination: tuple[str, int, str],
    now: datetime,
    stats: FlightStats,
    offer_rows: dict[Any, list[FlightOffer]] | None,
    *,
    first_choice_only: bool = False,
) -> _Attempt | str:
    """Search (or read cached) flights to one destination airport (iata, transfer minutes,
    time zone). Returns the reason when no trip to it can work."""
    search = match.search
    travel = search.travel
    iata, transfer, tz = destination
    try:
        window = compute_travel_window(
            surf_start=match.window_start,
            surf_end=match.window_end,
            spot_tz=match.spot.timezone,
            destination_iata=iata,
            destination_tz=tz,
            transfer_minutes=transfer,
            arrival_buffer_days=travel.arrival_buffer_days,
            departure_buffer_days=travel.departure_buffer_days,
            min_days_at_destination=travel.min_days_at_destination,
            max_trip_days=travel.max_trip_days,
            now=now,
        )
    except InfeasibleWindow as exc:
        return str(exc)
    origins = origin_airports(search, directory, iata)
    if not origins:
        return "no departure airport configured for this search"
    plan = plan_queries(search, window, directory, origins, first_choice_only=first_choice_only)
    if not plan:
        return "no departure dates can reach the arrival window"
    attempt = _Attempt(window, iata, transfer, planned=len(plan))
    for query in plan:
        try:
            fs = get_or_search(db, provider, query, now, stats, user_id=match.user_id)
        except UserFlightBudgetExceeded:
            attempt.budget_exhausted = True
            break
        attempt.searched += 1
        if fs.status == FlightSearchStatus.FAILED:
            attempt.errors.append(fs.error_message or "search failed")
            continue
        rows = None if offer_rows is None else offer_rows.get(fs.id)
        if rows is None:
            rows = list(
                db.scalars(select(FlightOffer).where(FlightOffer.flight_search_id == fs.id)).all()
            )
            if offer_rows is not None:
                offer_rows[fs.id] = rows
        for row in rows:
            attempt.offers.append((row_to_offer(row), row))
    return attempt


def _defer_for_budget(match: OpportunityMatch, stats: FlightStats) -> None:
    """Leave the match waiting (PENDING_FLIGHTS): its user's searches for today are used
    up. It is searched again by the next flight run after 00:00 UTC (each publication)."""
    limit = get_settings().flight_user_daily_quota
    match.status = MatchStatus.PENDING_FLIGHTS
    match.status_reason = (
        f"daily flight-search limit reached ({limit} searches per account); "
        "searching again after 00:00 UTC"
    )
    stats.budget_deferred += 1


def process_match(
    db: Session,
    provider: FlightProvider,
    directory: AirportDirectory,
    match: OpportunityMatch,
    now: datetime,
    stats: FlightStats,
    offer_rows: dict[Any, list[FlightOffer]] | None = None,
) -> bool:
    """Search (or reuse cached) flights for ``match`` and rank them. ``offer_rows``: the
    offers already read for a flight search in this run, by its id (a cached search is
    shared by the matches of every saved search with the same trip, and its offers do not
    change while flights are discovered).

    Returns True when the user's daily flight-search budget ran out (the caller skips the
    user's other matches in this run)."""
    settings = get_settings()
    search = match.search
    travel = search.travel
    if not match.destination_iata:
        _set_surf_only(match, match.status_reason or "no practical destination airport", now)
        return False
    dest_tz = directory.tz(match.destination_iata) or match.spot.timezone
    first = _attempt(
        db,
        provider,
        directory,
        match,
        (match.destination_iata, match.transfer_minutes or 0, dest_tz),
        now,
        stats,
        offer_rows,
    )
    if isinstance(first, str):
        _set_surf_only(match, first, now)
        return False
    chosen = first
    fallback_note: str | None = None
    if not first.offers and not first.budget_exhausted:
        # The regional airport returned nothing at all: try the spot's gateway once.
        for gateway in gateway_fallbacks(
            match.spot.airports, match.destination_iata, search.max_transfer_minutes
        ):
            alt = _attempt(
                db,
                provider,
                directory,
                match,
                (
                    gateway.airport_iata,
                    gateway.transfer_minutes,
                    directory.tz(gateway.airport_iata) or gateway.airport.timezone,
                ),
                now,
                stats,
                offer_rows,
                first_choice_only=True,
            )
            if isinstance(alt, str):
                continue
            stats.gateway_fallbacks += 1
            chosen = alt
            fallback_note = (
                f"no offers to {first.destination_iata}; searched via {alt.destination_iata}"
            )
            break
    offers, errors, window = chosen.offers, chosen.errors, chosen.window

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
    notes = list(errors)
    if chosen.budget_exhausted:
        notes.append(
            f"daily flight-search limit reached after {chosen.searched} of "
            f"{chosen.planned} planned searches"
        )
    if fallback_note:
        notes.insert(0, fallback_note)
    if chosen.budget_exhausted and not ranked:
        # Nothing usable yet and routes left unsearched: finish after the budget resets.
        match.flight_search_error = "; ".join(notes)[:1000]
        _defer_for_budget(match, stats)
        return True
    match.recommended_arrival_date = window.recommended_arrival_date
    match.recommended_departure_date = window.recommended_departure_date
    match.flight_search_error = "; ".join(notes)[:1000] if notes else None
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
        return chosen.budget_exhausted

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
    if chosen is not first:
        # Flights land at the gateway: the trip (and its transfer) is planned from there.
        match.destination_iata = chosen.destination_iata
        match.transfer_minutes = chosen.transfer_minutes
    components, overall = opportunity_scores(
        search.priority,
        surf=_surf(match),
        confidence=match.confidence,
        affordability=best.affordability,
        convenience=best.convenience,
    )
    match.scores = components
    match.overall_score = overall
    match.status = MatchStatus.FLIGHT_FOUND
    match.status_reason = fallback_note
    match.flight_checked_at = now
    stats.flight_found += 1
    return chosen.budget_exhausted


def flights_requested(match: OpportunityMatch) -> bool:
    """The user asked for a flight check that has not run yet."""
    return match.flights_requested_at is not None and (
        match.flight_checked_at is None or match.flights_requested_at > match.flight_checked_at
    )


def queue_order(matches: list[OpportunityMatch], now: datetime) -> list[OpportunityMatch]:
    """The order flights are searched in: each user's matches by priority (an explicit
    request first, then short-notice swells, then the best overall score), and the users
    served round-robin, so no account's many searches can use up the provider's daily
    quota before anyone else's first match is searched."""
    settings = get_settings()

    def priority(m: OpportunityMatch) -> tuple[int, int, datetime]:
        boosted = ranking_score(m.overall_score, m.window_start, now, settings)
        return (0 if flights_requested(m) else 1, -boosted, m.window_start)

    queues: dict[Any, deque[OpportunityMatch]] = {}
    for m in sorted(matches, key=priority):
        queues.setdefault(m.user_id, deque()).append(m)
    ordered: list[OpportunityMatch] = []
    while queues:
        for user_id in list(queues):
            queue = queues[user_id]
            ordered.append(queue.popleft())
            if not queue:
                del queues[user_id]
    return ordered


def matches_needing_flights(
    db: Session, now: datetime, limit: int, match_ids: list[Any] | None = None
) -> list[OpportunityMatch]:
    """The matches a flight search is justified for: new or materially changed ones
    (PENDING_FLIGHTS) and explicit user requests. Never a match whose forecast and trip did
    not change (no periodic re-search), in :func:`queue_order`."""
    requested = OpportunityMatch.flights_requested_at.is_not(None) & (
        OpportunityMatch.flight_checked_at.is_(None)
        | (OpportunityMatch.flights_requested_at > OpportunityMatch.flight_checked_at)
    )
    stmt = (
        select(OpportunityMatch)
        .join(SavedSearch, SavedSearch.id == OpportunityMatch.search_id)
        .where(
            SavedSearch.status == SearchStatus.ACTIVE,
            OpportunityMatch.window_start > now,
            or_(
                OpportunityMatch.status == MatchStatus.PENDING_FLIGHTS,
                requested
                & OpportunityMatch.status.notin_((MatchStatus.EXPIRED, MatchStatus.DISMISSED)),
            ),
        )
        # A bounded candidate set (explicit requests first); queue_order then serves the
        # users round-robin.
        .order_by(~requested, OpportunityMatch.overall_score.desc())
        .limit(max(limit, 1) * CANDIDATE_FACTOR)
    )
    if match_ids:
        stmt = stmt.where(OpportunityMatch.id.in_(match_ids))
    rows = list(db.scalars(stmt.options(*MATCH_FOR_FLIGHTS)).unique().all())
    return queue_order(rows, now)[:limit]


def discover_flights(
    db: Session, now: datetime | None = None, limit: int = 50, match_ids: list[Any] | None = None
) -> FlightStats:
    now = now or utcnow()
    stats = FlightStats()
    matches = matches_needing_flights(db, now, limit, match_ids)
    if not matches:
        return stats
    directory = AirportDirectory(db)
    provider = build_flight_provider(db, directory=directory)
    offer_rows: dict[Any, list[FlightOffer]] = {}
    out_of_budget: set[Any] = set()
    for match in matches:
        if match.user_id in out_of_budget:
            continue  # this user's searches for today are used up; the match stays pending
        stats.matches += 1
        try:
            if process_match(db, provider, directory, match, now, stats, offer_rows):
                out_of_budget.add(match.user_id)
            db.commit()
        except QuotaExceeded as exc:
            db.rollback()
            stats.quota_exhausted = True
            logger.warning("Stopping flight discovery: %s", exc)
            break
        except TransientProviderError as exc:
            db.rollback()
            offer_rows.clear()  # a rollback expires (or discards) the offers read
            stats.errors += 1
            match.flight_search_error = f"temporary provider error: {exc}"[:1000]
            db.commit()
            logger.warning("Transient flight provider error for match %s: %s", match.id, exc)
        except ProviderError as exc:
            db.rollback()
            offer_rows.clear()
            stats.errors += 1
            _set_surf_only(match, f"flight provider error: {exc}", now)
            db.commit()
            logger.error("Flight provider error for match %s: %s", match.id, exc)
    logger.info("Flight discovery: %s", stats.as_dict())
    return stats


def research_match(
    db: Session,
    match: OpportunityMatch,
    now: datetime,
    provider: FlightProvider | None = None,
    directory: AirportDirectory | None = None,
) -> FlightStats:
    """One targeted re-search of ``match`` right before an alert is sent, because its best
    fare expired, sold out or rose above the budget (app.services.notifications.alerts).
    Searches that are still cached are reused; the user's daily budget applies. A provider
    error leaves the match PENDING_FLIGHTS for the next flight run (each publication)
    instead of alerting with an unusable fare. Commits."""
    stats = FlightStats(matches=1)
    directory = directory or AirportDirectory(db)
    provider = provider or build_flight_provider(db, directory=directory)
    try:
        process_match(db, provider, directory, match, now, stats)
        db.commit()
    except (QuotaExceeded, TransientProviderError) as exc:
        db.rollback()
        if isinstance(exc, QuotaExceeded):
            stats.quota_exhausted = True
        else:
            stats.errors += 1
        match.status = MatchStatus.PENDING_FLIGHTS
        match.status_reason = "fare needs a re-check; searching again with the next update"
        match.flight_search_error = f"flight re-check before the alert failed: {exc}"[:1000]
        db.commit()
        logger.warning("Re-check before an alert failed for match %s: %s", match.id, exc)
    except ProviderError as exc:
        db.rollback()
        stats.errors += 1
        _set_surf_only(match, f"flight provider error: {exc}", now)
        db.commit()
        logger.error("Flight provider error for match %s: %s", match.id, exc)
    return stats


def revalidate_offer(
    db: Session, provider: FlightProvider, offer: FlightOffer, now: datetime
) -> OfferValidation:
    """Re-price an offer with its provider before alerting; never fabricates a price."""
    if offer.offer_expires_at is not None and offer.offer_expires_at <= now:
        offer.validation_status = OfferValidation.UNAVAILABLE
        offer.last_validated_at = now
        return offer.validation_status
    if provider.name != offer.provider:
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
