"""The spot forecast timeline (public, cacheable). Contract: app/schemas/timeline.py.

Answered from the public snapshot cache (app.services.public_cache) with its ETag; the
timeline is built for the start of the current UTC hour, together with the spot's
/forecast (app.api.spots.spot_page: the two read the same hours).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.spots import public_cache, public_json, spot_page, version_query
from app.core.database import get_db
from app.core.ratelimit import client_ip
from app.schemas.timeline import SpotTimelineOut

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
    v: str | None = version_query(),
    db: Session = Depends(get_db),
) -> Response:
    """Every chart row of the spot page on one time axis, for the next ``days`` days."""
    snapshot = spot_page(db, slug, days).snapshot(db, "spot-timeline", visitor=client_ip(request))
    etag = snapshot.etag or ""
    if _matches(request.headers.get("if-none-match"), etag):
        response = Response(status_code=304)
        public_cache(response, request, snapshot)
        response.headers["ETag"] = etag
        return response
    response = public_json(snapshot, request)
    response.headers["ETag"] = etag
    return response
