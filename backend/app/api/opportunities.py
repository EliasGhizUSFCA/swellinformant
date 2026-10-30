"""Travel opportunities (owner-only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import AppError
from app.core.ratelimit import hit
from app.models import User
from app.models.enums import MatchStatus
from app.schemas.opportunities import MatchDetail, MatchSummary
from app.services.flights.budget import searches_left
from app.services.opportunity_views import (
    get_owned_match,
    list_matches,
    match_detail,
    match_summary,
)
from app.workers.dispatch import kick

router = APIRouter(prefix="/api/opportunities", tags=["opportunities"])


@router.get("", response_model=list[MatchSummary])
def list_opportunities(
    status: list[MatchStatus] | None = Query(None),
    include_past: bool = False,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MatchSummary]:
    statuses = status or [
        MatchStatus.FLIGHT_FOUND,
        MatchStatus.SURF_ONLY,
        MatchStatus.PENDING_FLIGHTS,
    ]
    return list_matches(db, user, statuses=statuses, include_past=include_past, limit=limit)


@router.get("/{match_id}", response_model=MatchDetail)
def get_opportunity(
    match_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> MatchDetail:
    return match_detail(db, get_owned_match(db, user, match_id))


@router.post("/{match_id}/refresh-flights", response_model=MatchDetail)
def refresh_flights(
    match_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> MatchDetail:
    """An explicit flight check (spec 26: one of the few reasons to search). Searches that
    are still cached are reused; one whose first offer expired is not (its cache entry
    ends then), so the provider is asked again. It counts against the user's daily
    flight-search budget (FLIGHT_USER_DAILY_QUOTA), refused here once that is used up."""
    hit("refresh-flights", str(user.id), 6, 3600)
    match = get_owned_match(db, user, match_id)
    if match.status in (MatchStatus.EXPIRED, MatchStatus.DISMISSED):
        raise AppError("This opportunity is no longer active.", code="inactive")
    now = utcnow()
    limit = get_settings().flight_user_daily_quota
    if searches_left(user.id, limit, now) <= 0:
        raise AppError(
            f"You have used today's {limit} flight searches for your account. You can "
            "check again after 00:00 UTC.",
            code="flight_budget_reached",
            status_code=429,
        )
    match.flights_requested_at = now
    db.commit()
    # Never raises: if the request cannot be recorded, the next flight run (each forecast
    # publication) handles it.
    kick("flights.discover", match_ids=[str(match.id)])
    return match_detail(db, match)


@router.post("/{match_id}/dismiss", response_model=MatchSummary)
def dismiss(
    match_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> MatchSummary:
    match = get_owned_match(db, user, match_id)
    match.status = MatchStatus.DISMISSED
    match.status_reason = "dismissed by you"
    db.commit()
    return match_summary(match)
