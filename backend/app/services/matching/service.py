"""Job D — compare swell events with every active saved search.

* New matches are only created for events starting inside the detection lead-time range
  (DETECTION_LEAD_MIN_DAYS…MAX, default 5–10 days): far enough ahead to book, close
  enough for a useful forecast. Existing matches keep updating until the swell passes.
* Matches are unique per (search, event), so re-running never duplicates them.
* When an event stops qualifying (forecast downgraded, search edited, swell passed) its
  matches become EXPIRED; when detection merged two events, matches follow the survivor
  so alert history (and de-duplication) is preserved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    OpportunityMatch,
    SavedSearch,
    SpotAirport,
    SurfSpot,
    SwellEvent,
    User,
)
from app.models.enums import (
    DateMode,
    MatchStatus,
    SearchStatus,
    SwellEventStatus,
)
from app.services.flights.windows import InfeasibleWindow, TravelWindow, compute_travel_window
from app.services.matching.evaluate import (
    SearchCriteria,
    UserWindow,
    evaluate_steps,
    search_allows_spot,
)
from app.services.matching.scoring import opportunity_scores, surf_component
from app.services.surf_quality.confidence import confidence_label
from app.services.swell_detection.clustering import Step
from app.services.swell_detection.service import load_steps

logger = logging.getLogger(__name__)


@dataclass
class MatchingStats:
    searches: int = 0
    events: int = 0
    created: int = 0
    updated: int = 0
    expired: int = 0
    migrated: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def choose_airport(spot: SurfSpot, max_transfer_minutes: int | None) -> SpotAirport | None:
    options = [
        a
        for a in spot.airports
        if max_transfer_minutes is None or a.transfer_minutes <= max_transfer_minutes
    ]
    if not options:
        return None
    return sorted(options, key=lambda a: (not a.is_primary, a.transfer_minutes))[0]


def _expire(match: OpportunityMatch, reason: str, stats: MatchingStats) -> None:
    if match.status in (MatchStatus.EXPIRED, MatchStatus.DISMISSED):
        return
    match.status = MatchStatus.EXPIRED
    match.status_reason = reason
    stats.expired += 1


def migrate_merged_matches(db: Session, stats: MatchingStats) -> None:
    rows = db.scalars(
        select(OpportunityMatch)
        .join(SwellEvent, SwellEvent.id == OpportunityMatch.swell_event_id)
        .where(
            SwellEvent.status == SwellEventStatus.CANCELLED, SwellEvent.merged_into_id.is_not(None)
        )
    ).all()
    for m in rows:
        target = m.swell_event.merged_into_id
        exists = db.scalar(
            select(OpportunityMatch.id).where(
                OpportunityMatch.search_id == m.search_id, OpportunityMatch.swell_event_id == target
            )
        )
        if exists:
            _expire(m, "swell event merged into another event", stats)
        else:
            m.swell_event_id = target  # type: ignore[assignment]
            stats.migrated += 1
    db.flush()


def _apply_window(match: OpportunityMatch, event: SwellEvent, window: UserWindow) -> None:
    match.window_start = window.start
    match.window_end = window.end
    match.qualifying_hours = window.qualifying_hours
    match.peak_score = window.peak_score
    match.avg_score = window.avg_score
    match.peak_label = window.peak_label
    match.breaking_height_min_ft = window.height_min_ft
    match.breaking_height_max_ft = window.height_max_ft
    match.confidence = window.confidence
    match.confidence_label = confidence_label(window.confidence)
    match.is_demo = event.is_demo


def _surf_only_scores(match: OpportunityMatch, search: SavedSearch) -> None:
    components, overall = opportunity_scores(
        search.priority,
        surf=surf_component(match.peak_score, match.avg_score),
        confidence=match.confidence,
        affordability=None,
        convenience=None,
    )
    match.scores = components
    match.overall_score = overall


def _clear_flights(match: OpportunityMatch) -> None:
    match.best_offer = None
    match.best_price = None
    match.origin_iata = None
    match.offers.clear()


def evaluate_match(
    db: Session,
    search: SavedSearch,
    event: SwellEvent,
    steps: list[Step],
    existing: OpportunityMatch | None,
    now: datetime,
    stats: MatchingStats,
) -> OpportunityMatch | None:
    settings = get_settings()
    spot = event.spot
    if not search_allows_spot(search, spot):
        if existing:
            _expire(existing, "destination no longer included in this search", stats)
        return existing
    window = evaluate_steps(
        steps, SearchCriteria.from_search(search), settings.swell_event_break_daylight_hours
    )
    if window is None or window.end <= now:
        if existing:
            _expire(
                existing, "the latest forecast no longer meets this search's surf criteria", stats
            )
        return existing
    if search.date_mode == DateMode.FLEXIBLE and window.start > now + timedelta(
        days=search.horizon_days
    ):
        if existing:
            _expire(existing, "swell is beyond this search's monitoring horizon", stats)
        return existing
    if search.date_mode == DateMode.FIXED:
        # The surf itself must fall inside the user's available dates, whatever the airport.
        zone = ZoneInfo(spot.timezone)
        first_day = window.start.astimezone(zone).date()
        last_day = (window.end - timedelta(minutes=1)).astimezone(zone).date()
        if (
            search.date_start is None
            or search.date_end is None
            or not (search.date_start <= first_day and last_day <= search.date_end)
        ):
            if existing:
                _expire(existing, "swell no longer fits your fixed travel dates", stats)
            return existing

    airport = choose_airport(spot, search.max_transfer_minutes)
    travel = search.travel
    tw: TravelWindow | None = None
    structural_reason: str | None = None
    if airport is None:
        structural_reason = (
            "no practical airport within your transfer limit"
            if spot.airports
            else "no practical airport is configured for this spot"
        )
    else:
        try:
            tw = compute_travel_window(
                surf_start=window.start,
                surf_end=window.end,
                spot_tz=spot.timezone,
                destination_iata=airport.airport_iata,
                destination_tz=airport.airport.timezone,
                transfer_minutes=airport.transfer_minutes,
                arrival_buffer_days=travel.arrival_buffer_days,
                departure_buffer_days=travel.departure_buffer_days,
                min_days_at_destination=travel.min_days_at_destination,
                max_trip_days=travel.max_trip_days,
                now=now,
            )
        except InfeasibleWindow as exc:
            structural_reason = str(exc)
    if tw is not None and search.date_mode == DateMode.FIXED:
        assert search.date_start and search.date_end
        if (
            tw.recommended_arrival_date < search.date_start
            or tw.recommended_departure_date > search.date_end
        ):
            if existing:
                _expire(existing, "swell no longer fits your fixed travel dates", stats)
            return existing

    match = existing
    if match is None:
        match = OpportunityMatch(
            search_id=search.id,
            user_id=search.user_id,
            swell_event_id=event.id,
            spot_id=spot.id,
            status=MatchStatus.PENDING_FLIGHTS,
            scores={},
            notification_count=0,
        )
        db.add(match)
        stats.created += 1
    else:
        stats.updated += 1
    previous = (
        match.recommended_arrival_date,
        match.recommended_departure_date,
        match.destination_iata,
    )
    _apply_window(match, event, window)

    if structural_reason is not None:
        if match.status != MatchStatus.DISMISSED:
            match.status = MatchStatus.SURF_ONLY
            match.status_reason = structural_reason
        _clear_flights(match)
        match.flight_checked_at = None
        match.destination_iata = airport.airport_iata if airport else None
        match.transfer_minutes = airport.transfer_minutes if airport else None
        match.recommended_arrival_date = None
        match.recommended_departure_date = None
        _surf_only_scores(match, search)
        return match

    assert tw is not None and airport is not None
    match.destination_iata = airport.airport_iata
    match.transfer_minutes = airport.transfer_minutes
    match.recommended_arrival_date = tw.recommended_arrival_date
    match.recommended_departure_date = tw.recommended_departure_date
    changed = previous != (
        tw.recommended_arrival_date,
        tw.recommended_departure_date,
        airport.airport_iata,
    )
    if match.status == MatchStatus.DISMISSED:
        return match
    if (
        existing is None
        or changed
        or match.status in (MatchStatus.EXPIRED, MatchStatus.PENDING_FLIGHTS)
        or (match.status == MatchStatus.SURF_ONLY and match.flight_checked_at is None)
    ):
        if changed and existing is not None:
            _clear_flights(match)
        match.status = MatchStatus.PENDING_FLIGHTS
        match.status_reason = "searching for flights"
        _surf_only_scores(match, search)
    elif match.status == MatchStatus.SURF_ONLY:
        _surf_only_scores(match, search)
    return match


def match_all(db: Session, now: datetime | None = None) -> MatchingStats:
    settings = get_settings()
    now = now or utcnow()
    stats = MatchingStats()
    migrate_merged_matches(db, stats)

    events = db.scalars(
        select(SwellEvent).where(
            SwellEvent.status == SwellEventStatus.ACTIVE,
            SwellEvent.end_time >= now,
            SwellEvent.start_time <= now + timedelta(days=settings.detection_lead_max_days),
        )
    ).all()
    stats.events = len(events)
    searches = db.scalars(
        select(SavedSearch)
        .join(User, User.id == SavedSearch.user_id)
        .where(SavedSearch.status == SearchStatus.ACTIVE, User.is_active)
    ).all()
    stats.searches = len(searches)
    search_ids = [s.id for s in searches]
    existing: dict[tuple[object, object], OpportunityMatch] = {}
    if search_ids:
        for m in db.scalars(
            select(OpportunityMatch).where(OpportunityMatch.search_id.in_(search_ids))
        ).all():
            existing[(m.search_id, m.swell_event_id)] = m

    steps_cache: dict[object, list[Step]] = {}

    def steps_for(event: SwellEvent) -> list[Step]:
        if event.id not in steps_cache:
            steps: list[Step] = []
            if event.latest_run_id is not None:
                steps = [
                    s
                    for s in load_steps(
                        db,
                        event.spot_id,
                        event.latest_run_id,
                        event.start_time - timedelta(hours=1),
                    )
                    if s.time <= event.end_time
                ]
            steps_cache[event.id] = steps
        return steps_cache[event.id]

    event_ids = {e.id for e in events}
    lead_min = timedelta(days=settings.detection_lead_min_days)
    lead_max = timedelta(days=settings.detection_lead_max_days)
    for search in searches:
        seen: set[object] = set()
        for event in events:
            current = existing.get((search.id, event.id))
            if current is None and not (now + lead_min <= event.start_time <= now + lead_max):
                continue
            evaluate_match(db, search, event, steps_for(event), current, now, stats)
            seen.add(event.id)
        search.last_evaluated_at = now
        for (sid, eid), m in existing.items():
            if sid == search.id and eid not in seen and eid not in event_ids:
                _expire(m, "swell event is no longer forecast", stats)
        db.commit()
    # Matches of paused searches are left untouched (no new alerts are generated for them).
    logger.info("Matching: %s", stats.as_dict())
    return stats
