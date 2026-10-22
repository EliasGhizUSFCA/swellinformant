"""Surf spots, forecasts, swell events and reference data (public).

The public GETs that read the most (the spot list, the map conditions, a spot's detail,
forecast and events, and the next swells) are answered from the public snapshot cache
(app.services.public_cache): computed for the start of the current UTC hour at most once
per published data, hour and query, and sent with the shared-cache headers below. The
bodies are the ones the response models give (``public_cache.render``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from pydantic import TypeAdapter
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.deps import get_optional_user
from app.core.database import get_db
from app.core.errors import NotFound
from app.core.http_cache import cache_headers
from app.models import Airport, SurfSpot, SwellEvent, User
from app.schemas.opportunities import MatchSummary
from app.schemas.spots import (
    AirportOut,
    EventOut,
    MapSnapshotOut,
    RegionOut,
    SpotDetail,
    SpotForecastOut,
    SpotSummary,
)
from app.schemas.timeline import SpotTimelineOut
from app.services import public_cache as snapshots
from app.services.opportunity_views import list_matches
from app.services.spot_views import (
    event_out,
    shared_hour_reads,
    spot_detail,
    spot_forecast,
    upcoming_events,
)
from app.services.spot_views import spot_events as events_of_spot
from app.services.timeline import spot_timeline as build_timeline
from app.services.timeline import timeline_etag

router = APIRouter(prefix="/api", tags=["spots"])

# The user-independent public GETs (no user dependency, no cookie): browsers keep them a
# minute; the CDN 5 minutes, and for up to an hour more it serves its stale copy while it
# refreshes in the background. Their data changes only with a publication or the hour, and
# every one of them uses these windows (with the map-field index, app.api.map_fields), so
# after a publication the edge mixes old and new answers for about 5 minutes only. Used
# here, by the timeline, tides and observations routes. Never on a route with a user
# dependency (app.core.http_cache).
PUBLIC_BROWSER_SECONDS = 60
PUBLIC_CDN_SECONDS = 300
PUBLIC_STALE_SECONDS = 3600

SPOT_DETAIL = TypeAdapter(SpotDetail)
SPOT_FORECAST = TypeAdapter(SpotForecastOut)
TIMELINE = TypeAdapter(SpotTimelineOut)
EVENTS = TypeAdapter(list[EventOut])


def public_cache(response: Response) -> None:
    """The cache headers of a user-independent public GET (see PUBLIC_*_SECONDS)."""
    cache_headers(
        response,
        browser_seconds=PUBLIC_BROWSER_SECONDS,
        cdn_seconds=PUBLIC_CDN_SECONDS,
        stale_while_revalidate=PUBLIC_STALE_SECONDS,
    )


def public_json(snapshot: snapshots.Snapshot) -> Response:
    """A public snapshot as the JSON response FastAPI would send, with the public headers."""
    response = Response(content=snapshot.body, media_type="application/json")
    public_cache(response)
    return response


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
    if spot is None:
        raise NotFound("Surf spot not found.")
    return spot


@router.get("/map/conditions", response_model=MapSnapshotOut)
def map_conditions(
    at: datetime | None = Query(None, description="Forecast time (ISO 8601); default now"),
    db: Session = Depends(get_db),
) -> Response:
    """All spots at one forecast hour, from cached predictions (public data, cacheable)."""
    return public_json(snapshots.map_conditions(db, at))


@router.get("/spots", response_model=list[SpotSummary])
def list_spots(db: Session = Depends(get_db)) -> Response:
    """Every active spot with its current conditions (public data, cacheable)."""
    return public_json(snapshots.spot_list(db))


@router.get("/spots/{slug}", response_model=SpotDetail)
def get_spot(slug: str, db: Session = Depends(get_db)) -> Response:
    """One spot's page data (public data, cacheable)."""
    return public_json(
        snapshots.snapshot(
            db,
            "spot",
            slug,
            lambda now: snapshots.render(SPOT_DETAIL, spot_detail(db, _spot(db, slug), now)),
        )
    )


def spot_page(db: Session, slug: str, days: int) -> snapshots.Together:
    """The spot page's forecast and timeline (app.api.timeline) for ``days`` days: they
    read the same hours, so a miss of either computes both (public_cache.Together)."""
    found: list[SurfSpot] = []

    def spot() -> SurfSpot:
        if not found:
            found.append(_spot(db, slug))
        return found[0]

    def forecast(now: datetime) -> snapshots.Snapshot:
        return snapshots.render(SPOT_FORECAST, spot_forecast(db, spot(), days=days, now=now))

    def timeline(now: datetime) -> snapshots.Snapshot:
        result = build_timeline(db, spot(), days, now)
        return snapshots.render(TIMELINE, result, etag=timeline_etag(result, days))

    return snapshots.Together(
        "spot-page",
        f"{slug}:{days}",
        {"spot-forecast": forecast, "spot-timeline": timeline},
        reads=lambda: shared_hour_reads(db),
    )


@router.get("/spots/{slug}/forecast", response_model=SpotForecastOut)
def get_forecast(
    slug: str, days: int = Query(10, ge=1, le=16), db: Session = Depends(get_db)
) -> Response:
    """The spot's forecast hours and model series (public data, cacheable)."""
    return public_json(spot_page(db, slug, days).snapshot(db, "spot-forecast"))


@router.get("/spots/{slug}/events", response_model=list[EventOut])
def spot_events(slug: str, db: Session = Depends(get_db)) -> Response:
    """The spot's swell events that have not ended (public data, cacheable)."""
    return public_json(
        snapshots.snapshot(
            db,
            "spot-events",
            slug,
            lambda now: snapshots.render(EVENTS, events_of_spot(db, _spot(db, slug), now)),
        )
    )


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
) -> Response:
    """The best swells that have not ended (public data, cacheable)."""
    return public_json(
        snapshots.snapshot(
            db,
            "events",
            f"{limit}:{min_score}",
            lambda now: snapshots.render(
                EVENTS, upcoming_events(db, now, limit=limit, min_score=min_score)
            ),
        )
    )


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
