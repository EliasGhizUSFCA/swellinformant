"""Surf spots, forecasts, swell events and reference data (public)."""

from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.deps import get_optional_user
from app.core.database import get_db, utcnow
from app.core.errors import NotFound
from app.models import Airport, SurfSpot, SwellEvent, User
from app.models.enums import SwellEventStatus
from app.schemas.opportunities import MatchSummary
from app.schemas.spots import (
    AirportOut,
    EventOut,
    RegionOut,
    SpotDetail,
    SpotForecastOut,
    SpotSummary,
)
from app.services.opportunity_views import list_matches
from app.services.spot_views import (
    event_out,
    spot_detail,
    spot_forecast,
    spot_summaries,
    upcoming_events,
)

router = APIRouter(prefix="/api", tags=["spots"])


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
    if spot is None:
        raise NotFound("Surf spot not found.")
    return spot


@router.get("/spots", response_model=list[SpotSummary])
def list_spots(db: Session = Depends(get_db)) -> list[SpotSummary]:
    return spot_summaries(db)


@router.get("/spots/{slug}", response_model=SpotDetail)
def get_spot(slug: str, db: Session = Depends(get_db)) -> SpotDetail:
    return spot_detail(db, _spot(db, slug))


@router.get("/spots/{slug}/forecast", response_model=SpotForecastOut)
def get_forecast(
    slug: str, days: int = Query(10, ge=1, le=16), db: Session = Depends(get_db)
) -> SpotForecastOut:
    return spot_forecast(db, _spot(db, slug), days=days)


@router.get("/spots/{slug}/events", response_model=list[EventOut])
def spot_events(slug: str, db: Session = Depends(get_db)) -> list[EventOut]:
    spot = _spot(db, slug)
    now = utcnow()
    rows = db.scalars(
        select(SwellEvent)
        .where(
            SwellEvent.spot_id == spot.id,
            SwellEvent.end_time >= now,
            SwellEvent.status.in_((SwellEventStatus.ACTIVE, SwellEventStatus.DOWNGRADED)),
        )
        .order_by(SwellEvent.start_time)
    ).all()
    return [event_out(e, now) for e in rows]


@router.get("/spots/{slug}/opportunities", response_model=list[MatchSummary])
def spot_opportunities(
    slug: str, user: User | None = Depends(get_optional_user), db: Session = Depends(get_db)
) -> list[MatchSummary]:
    """The signed-in user's travel opportunities for this spot (empty when signed out)."""
    if user is None:
        return []
    return list_matches(db, user, spot_id=_spot(db, slug).id)


@router.get("/events", response_model=list[EventOut])
def list_events(
    limit: int = Query(20, ge=1, le=100),
    min_score: int = Query(0, ge=0, le=100),
    db: Session = Depends(get_db),
) -> list[EventOut]:
    return upcoming_events(db, limit=limit, min_score=min_score)


@router.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: uuid.UUID, db: Session = Depends(get_db)) -> EventOut:
    event = db.get(SwellEvent, event_id)
    if event is None:
        raise NotFound("Swell event not found.")
    return event_out(event, include_history=True)


@router.get("/airports", response_model=list[AirportOut])
def search_airports(
    q: str = Query("", max_length=60),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[AirportOut]:
    stmt = select(Airport)
    term = q.strip()
    if term:
        like = f"%{term.replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(
                Airport.iata.ilike(f"{term[:3]}%"),
                Airport.city.ilike(like),
                Airport.name.ilike(like),
                Airport.country.ilike(like),
            )
        )
    rows = db.scalars(stmt.order_by(Airport.iata).limit(200)).all()
    exact = [a for a in rows if a.iata == term.upper()]
    rest = [a for a in rows if a.iata != term.upper()]
    return [AirportOut.model_validate(a) for a in (exact + rest)[:limit]]


@router.get("/regions", response_model=list[RegionOut])
def regions(db: Session = Depends(get_db)) -> list[RegionOut]:
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    counts: dict[str, int] = defaultdict(int)
    for s in db.scalars(select(SurfSpot).where(SurfSpot.is_active)).all():
        grouped[s.region_group][s.country_code] = s.country
        counts[s.region_group] += 1
    return [
        RegionOut(
            region_group=group,
            spot_count=counts[group],
            countries=[
                {"code": c, "name": n}
                for c, n in sorted(grouped[group].items(), key=lambda x: x[1])
            ],
        )
        for group in sorted(grouped)
    ]
