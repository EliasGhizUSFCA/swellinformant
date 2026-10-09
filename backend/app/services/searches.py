"""Saved search CRUD with ownership enforced on every operation."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.errors import AppError, NotFound
from app.models import (
    Airport,
    Notification,
    OpportunityMatch,
    SavedSearch,
    SearchDestination,
    SearchOrigin,
    SurfSpot,
    TravelPreference,
    User,
)
from app.models.enums import DestinationMode, NotificationStatus, SearchStatus
from app.schemas.searches import DestinationsIn, MatchCounts, SearchIn, SearchOut, TravelOut
from app.services.units import ft_to_units, height_to_ft

logger = logging.getLogger(__name__)

MAX_SEARCHES_PER_USER = 25


def get_owned(db: Session, user: User, search_id: uuid.UUID) -> SavedSearch:
    search = db.get(SavedSearch, search_id)
    # Same 404 for "missing" and "someone else's" so IDs cannot be probed.
    if search is None or search.user_id != user.id:
        raise NotFound("Search not found.")
    return search


def _validate_refs(db: Session, data: SearchIn) -> dict[str, int]:
    known = set(db.scalars(select(Airport.iata).where(Airport.iata.in_(data.origins))).all())
    unknown = [o for o in data.origins if o not in known]
    if unknown:
        raise AppError(
            f"Unknown departure airport(s): {', '.join(unknown)}.", code="unknown_airport"
        )
    slugs: dict[str, int] = {}
    if data.destination_mode == DestinationMode.SPOTS:
        rows = db.execute(
            select(SurfSpot.slug, SurfSpot.id).where(
                SurfSpot.slug.in_(data.destinations.spot_slugs)
            )
        ).all()
        slugs = {slug: sid for slug, sid in rows}
        missing = [s for s in data.destinations.spot_slugs if s not in slugs]
        if missing:
            raise AppError(f"Unknown surf spot(s): {', '.join(missing)}.", code="unknown_spot")
    if data.destination_mode == DestinationMode.REGIONS:
        regions = set(db.scalars(select(SurfSpot.region_group).distinct()).all())
        countries = set(db.scalars(select(SurfSpot.country_code).distinct()).all())
        bad = [r for r in data.destinations.region_groups if r not in regions]
        bad += [c for c in data.destinations.countries if c.upper() not in countries]
        if bad:
            raise AppError(
                f"Unknown region(s) or countries: {', '.join(bad)}.", code="unknown_region"
            )
    return slugs


def _apply(db: Session, search: SavedSearch, data: SearchIn) -> None:
    slugs = _validate_refs(db, data)
    search.name = data.name
    search.date_mode = data.date_mode
    search.date_start = data.date_start if data.date_mode == "fixed" else None
    search.date_end = data.date_end if data.date_mode == "fixed" else None
    search.horizon_days = data.horizon_days
    search.units = data.units
    search.wave_min_ft = round(height_to_ft(data.wave_min, data.units), 2)
    search.wave_max_ft = round(height_to_ft(data.wave_max, data.units), 2)
    search.wave_preferred_ft = (
        round(height_to_ft(data.wave_preferred, data.units), 2)
        if data.wave_preferred is not None
        else None
    )
    search.min_quality = data.min_quality
    search.min_period_s = data.min_period_s
    search.max_wind_kmh = data.max_wind_kmh
    search.wind_requirement = data.wind_requirement
    search.swell_direction_min = data.swell_direction_min
    search.swell_direction_max = data.swell_direction_max
    search.min_consistency = data.min_consistency
    search.break_types = [b.value for b in data.break_types]
    search.min_window_hours = data.min_window_hours
    search.destination_mode = data.destination_mode
    search.max_transfer_minutes = data.max_transfer_minutes
    search.max_origin_ground_km = data.max_origin_ground_km
    search.notification_channel = data.notification_channel
    search.notify_surf_only = data.notify_surf_only
    search.notify_on_updates = data.notify_on_updates
    search.priority = data.priority

    search.origins.clear()
    db.flush()
    for code in data.origins:
        search.origins.append(SearchOrigin(airport_iata=code))
    search.destinations.clear()
    db.flush()
    if data.destination_mode == DestinationMode.SPOTS:
        for slug in data.destinations.spot_slugs:
            search.destinations.append(SearchDestination(spot_id=slugs[slug]))
    elif data.destination_mode == DestinationMode.REGIONS:
        for region in data.destinations.region_groups:
            search.destinations.append(SearchDestination(region_group=region))
        for country in data.destinations.countries:
            search.destinations.append(SearchDestination(country_code=country.upper()))
    t = data.travel
    travel = search.travel or TravelPreference()
    travel.max_price = t.max_price
    travel.currency = t.currency
    travel.direct_only = t.direct_only
    travel.max_layovers = 0 if t.direct_only else t.max_layovers
    travel.max_flight_hours = t.max_flight_hours
    travel.min_days_at_destination = t.min_days_at_destination
    travel.max_trip_days = t.max_trip_days
    travel.travelers = t.travelers
    travel.preferred_airlines = t.preferred_airlines
    travel.cabin_class = t.cabin_class
    travel.arrival_buffer_days = t.arrival_buffer_days
    travel.departure_buffer_days = t.departure_buffer_days
    search.travel = travel


def create_search(db: Session, user: User, data: SearchIn) -> SavedSearch:
    count = db.scalar(select(func.count(SavedSearch.id)).where(SavedSearch.user_id == user.id)) or 0
    if count >= MAX_SEARCHES_PER_USER:
        raise AppError(
            f"You can keep at most {MAX_SEARCHES_PER_USER} searches.", code="limit_reached"
        )
    search = SavedSearch(
        user_id=user.id, status=SearchStatus.ACTIVE, wave_min_ft=0, wave_max_ft=1, name=data.name
    )
    db.add(search)
    _apply(db, search, data)
    db.commit()
    return search


def update_search(db: Session, search: SavedSearch, data: SearchIn) -> SavedSearch:
    _apply(db, search, data)
    db.commit()
    return search


def set_paused(db: Session, search: SavedSearch, paused: bool) -> SavedSearch:
    now = utcnow()
    search.status = SearchStatus.PAUSED if paused else SearchStatus.ACTIVE
    search.paused_at = now if paused else None
    if paused:
        # Alerts queued but not yet sent for this search are withdrawn.
        match_ids = select(OpportunityMatch.id).where(OpportunityMatch.search_id == search.id)
        db.execute(
            update(Notification)
            .where(
                Notification.match_id.in_(match_ids),
                Notification.status == NotificationStatus.QUEUED,
            )
            .values(
                status=NotificationStatus.SKIPPED,
                last_error="search paused before delivery",
                updated_at=now,
            )
        )
    db.commit()
    return search


def duplicate_search(db: Session, user: User, search: SavedSearch) -> SavedSearch:
    """Copies start PAUSED so they can be edited before they begin alerting."""
    data = to_input(db, search)
    data.name = f"{search.name} (copy)"[:120]
    copy = create_search(db, user, data)
    return set_paused(db, copy, True)


def delete_search(db: Session, search: SavedSearch) -> None:
    db.delete(search)
    db.commit()


def _spot_slugs(db: Session, search: SavedSearch) -> list[str]:
    ids = [d.spot_id for d in search.destinations if d.spot_id]
    if not ids:
        return []
    return list(
        db.scalars(select(SurfSpot.slug).where(SurfSpot.id.in_(ids)).order_by(SurfSpot.slug)).all()
    )


def to_input(db: Session, search: SavedSearch) -> SearchIn:
    out = search_out_fields(search, _spot_slugs(db, search))
    return SearchIn.model_validate({k: out[k] for k in SearchIn.model_fields if k in out})


def _display(value_ft: float | None, units: str) -> float | None:
    return None if value_ft is None else round(ft_to_units(value_ft, units), 1)


def search_out_fields(search: SavedSearch, spot_slugs: list[str]) -> dict[str, object]:
    t = search.travel
    destinations = DestinationsIn(
        spot_slugs=spot_slugs,
        countries=[d.country_code for d in search.destinations if d.country_code],
        region_groups=[d.region_group for d in search.destinations if d.region_group],
    )
    return {
        "id": search.id,
        "name": search.name,
        "status": search.status,
        "date_mode": search.date_mode,
        "date_start": search.date_start,
        "date_end": search.date_end,
        "horizon_days": search.horizon_days,
        "units": search.units,
        "wave_min": _display(search.wave_min_ft, search.units),
        "wave_max": _display(search.wave_max_ft, search.units),
        "wave_preferred": _display(search.wave_preferred_ft, search.units),
        "wave_min_ft": search.wave_min_ft,
        "wave_max_ft": search.wave_max_ft,
        "min_quality": search.min_quality,
        "min_period_s": search.min_period_s,
        "max_wind_kmh": search.max_wind_kmh,
        "wind_requirement": search.wind_requirement,
        "swell_direction_min": search.swell_direction_min,
        "swell_direction_max": search.swell_direction_max,
        "min_consistency": search.min_consistency,
        "break_types": list(search.break_types or []),
        "min_window_hours": search.min_window_hours,
        "destination_mode": search.destination_mode,
        "destinations": destinations,
        "max_transfer_minutes": search.max_transfer_minutes,
        "origins": [o.airport_iata for o in search.origins],
        "max_origin_ground_km": search.max_origin_ground_km,
        "notification_channel": search.notification_channel,
        "notify_surf_only": search.notify_surf_only,
        "notify_on_updates": search.notify_on_updates,
        "priority": search.priority,
        "travel": TravelOut(
            max_price=Decimal(t.max_price),
            currency=t.currency,
            direct_only=t.direct_only,
            max_layovers=t.max_layovers,
            max_flight_hours=t.max_flight_hours,
            min_days_at_destination=t.min_days_at_destination,
            max_trip_days=t.max_trip_days,
            travelers=t.travelers,
            preferred_airlines=list(t.preferred_airlines or []),
            cabin_class=t.cabin_class,
            arrival_buffer_days=t.arrival_buffer_days,
            departure_buffer_days=t.departure_buffer_days,
        ),
        "created_at": search.created_at,
        "updated_at": search.updated_at,
        "paused_at": search.paused_at,
        "last_evaluated_at": search.last_evaluated_at,
    }


def search_out(db: Session, search: SavedSearch, counts: Counter[str] | None = None) -> SearchOut:
    if counts is None:
        counts = Counter(
            dict(
                db.execute(
                    select(OpportunityMatch.status, func.count())
                    .where(OpportunityMatch.search_id == search.id)
                    .group_by(OpportunityMatch.status)
                ).all()
            )
        )
    fields = search_out_fields(search, _spot_slugs(db, search))
    by_status = {str(getattr(k, "value", k)): v for k, v in counts.items()}
    fields["match_counts"] = MatchCounts(
        total=sum(by_status.values()),
        flight_found=by_status.get("flight_found", 0),
        surf_only=by_status.get("surf_only", 0),
        pending_flights=by_status.get("pending_flights", 0),
        expired=by_status.get("expired", 0),
    )
    return SearchOut.model_validate(fields)
