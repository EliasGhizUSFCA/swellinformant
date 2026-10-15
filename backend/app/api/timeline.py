"""The spot forecast timeline (public, cacheable). Contract: app/schemas/timeline.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.errors import NotFound
from app.models import SurfSpot
from app.schemas.timeline import SpotTimelineOut
from app.services.timeline import spot_timeline as build_timeline
from app.services.timeline import timeline_etag

router = APIRouter(prefix="/api/spots", tags=["spots"])

CACHE_CONTROL = "public, max-age=300"


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
    response: Response,
    days: int = Query(10, ge=1, le=16),
    db: Session = Depends(get_db),
) -> SpotTimelineOut | Response:
    """Every chart row of the spot page on one time axis, for the next ``days`` days."""
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
    if spot is None:
        raise NotFound("Surf spot not found.")
    timeline = build_timeline(db, spot, days)
    etag = timeline_etag(timeline, days)
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return timeline
