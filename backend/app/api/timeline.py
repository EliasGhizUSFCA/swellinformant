"""The spot forecast timeline (public, cacheable). Contract: app/schemas/timeline.py.

Not implemented yet: the response says the timeline is not available and carries the tide
and observation blocks as app.services.tides.api reports them.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import NotFound
from app.models import SurfSpot
from app.schemas.timeline import SpotTimelineOut, StepsMetaOut, TimelineUnitsOut
from app.services.tides.api import spot_observations_block, spot_tide_block

router = APIRouter(prefix="/api/spots", tags=["spots"])

NOT_AVAILABLE = "Timeline not available yet"


@router.get("/{slug}/timeline", response_model=SpotTimelineOut)
def spot_timeline(
    slug: str,
    response: Response,
    days: int = Query(10, ge=1, le=16),
    db: Session = Depends(get_db),
) -> SpotTimelineOut:
    """Every chart row of the spot page on one time axis, for the next ``days`` days."""
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
    if spot is None:
        raise NotFound("Surf spot not found.")
    now = utcnow()
    lookback = timedelta(hours=get_settings().observation_lookback_hours)
    response.headers["Cache-Control"] = "public, max-age=300"
    return SpotTimelineOut(
        spot_slug=spot.slug,
        timezone=spot.timezone,
        generated_at=now,
        available=False,
        message=NOT_AVAILABLE,
        stale=False,
        units=TimelineUnitsOut(),
        steps_meta=StepsMetaOut(segments=[]),
        provenance=None,
        days=[],
        steps=[],
        tide=spot_tide_block(db, spot, now, now + timedelta(days=days), now),
        observations=spot_observations_block(db, spot, now - lookback, now, now),
    )
