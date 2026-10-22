"""Tide predictions and nearby observations for one spot (public, cacheable).

Contract: app/schemas/tides.py. The blocks are built by app.services.tides.api.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.spots import public_cache
from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import AppError, NotFound
from app.models import SurfSpot
from app.schemas.tides import ObservationsBlockOut, TideBlockOut
from app.services.tides.api import spot_observations_block, spot_tide_block

router = APIRouter(prefix="/api/spots", tags=["tides"])

# Longest window one request may ask for (the job stores TIDE_HORIZON_DAYS <= 31 ahead).
MAX_WINDOW = timedelta(days=31)
# Accepted window instants: stored predictions only cover TIDE_RETENTION_PAST_DAYS back to
# TIDE_HORIZON_DAYS ahead, so nothing is lost, and the date arithmetic (curve and event
# margins, the default horizon) stays far from the calendar's limits (year 1 and 9999).
EARLIEST = datetime(1900, 1, 1, tzinfo=UTC)
LATEST = datetime(2200, 1, 1, tzinfo=UTC)


def active_spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug, SurfSpot.is_active))
    if spot is None:
        raise NotFound("Surf spot not found.")
    return spot


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _bounded(when: datetime, name: str) -> datetime:
    """``when`` in UTC, or HTTP 400 outside EARLIEST..LATEST (including instants that cannot
    be expressed in UTC at all, e.g. 0001-01-01T00:00+05:00)."""
    try:
        value = _as_utc(when)
    except OverflowError:
        raise AppError(f"{name} is out of range.") from None
    if not EARLIEST <= value <= LATEST:
        raise AppError(f"{name} must lie between 1900 and 2200.")
    return value


@router.get("/{slug}/tides", response_model=TideBlockOut)
def spot_tides(
    slug: str,
    response: Response,
    start: datetime | None = Query(None, description="Window start (ISO 8601); default now"),
    end: datetime | None = Query(
        None, description="Window end (ISO 8601); default start + TIDE_HORIZON_DAYS"
    ),
    db: Session = Depends(get_db),
) -> TideBlockOut:
    """Predicted tide heights and high/low waters at the spot's tide station.

    Astronomical tide predictions (never observed water levels), relative to the stated
    datum; ``status`` says whether they are real, synthetic (demo) or unavailable.
    """
    spot = active_spot(db, slug)
    now = utcnow()
    window_start = _bounded(start, "start") if start is not None else now
    window_end = (
        _bounded(end, "end")
        if end is not None
        else window_start + timedelta(days=get_settings().tide_horizon_days)
    )
    if window_end <= window_start:
        raise AppError("end must be after start.")
    if window_end - window_start > MAX_WINDOW:
        raise AppError("The window may span at most 31 days.")
    block = spot_tide_block(db, spot, window_start, window_end, now)
    public_cache(response)
    return block


@router.get("/{slug}/observations", response_model=ObservationsBlockOut)
def spot_observations(
    slug: str, response: Response, db: Session = Depends(get_db)
) -> ObservationsBlockOut:
    """Recent measurements at the spot's reference stations (never forecasts)."""
    spot = active_spot(db, slug)
    now = utcnow()
    start = now - timedelta(hours=get_settings().observation_lookback_hours)
    block = spot_observations_block(db, spot, start, now, now)
    public_cache(response)
    return block
