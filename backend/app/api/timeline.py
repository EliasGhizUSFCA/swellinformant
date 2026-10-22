"""The spot forecast timeline (public, cacheable). Contract: app/schemas/timeline.py.

Answered from the public snapshot cache (app.services.public_cache) with its ETag; the
timeline is built for the start of the current UTC hour.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.spots import public_cache, public_json
from app.core.database import get_db
from app.core.errors import NotFound
from app.models import SurfSpot
from app.schemas.timeline import SpotTimelineOut
from app.services import public_cache as snapshots
from app.services.timeline import spot_timeline as build_timeline
from app.services.timeline import timeline_etag

TIMELINE = TypeAdapter(SpotTimelineOut)

router = APIRouter(prefix="/api/spots", tags=["spots"])


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Weak comparison (RFC 9110 13.1.2): ``W/"x"`` matches ``"x"`` and ``*`` matches all."""
    if not if_none_match:
        return False
    tags = [t.strip() for t in if_none_match.split(",")]
    bare = etag.removeprefix("W/")
    return "*" in tags or any(t.removeprefix("W/") == bare for t in tags)


@router.get(
    "/{slug}/timeline",
    response_model=SpotTimelineOut,
    responses={304: {"description": "Not modified (If-None-Match matched the ETag)"}},
)
def spot_timeline(
    slug: str,
    request: Request,
    days: int = Query(10, ge=1, le=16),
    db: Session = Depends(get_db),
) -> Response:
    """Every chart row of the spot page on one time axis, for the next ``days`` days."""

    def build(now: datetime) -> snapshots.Snapshot:
        spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
        if spot is None:
            raise NotFound("Surf spot not found.")
        timeline = build_timeline(db, spot, days, now)
        return snapshots.render(TIMELINE, timeline, etag=timeline_etag(timeline, days))

    snapshot = snapshots.snapshot(db, "spot-timeline", f"{slug}:{days}", build)
    etag = snapshot.etag or ""
    if _matches(request.headers.get("if-none-match"), etag):
        response = Response(status_code=304)
        public_cache(response)
        response.headers["ETag"] = etag
        return response
    response = public_json(snapshot)
    response.headers["ETag"] = etag
    return response
