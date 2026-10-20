"""Travel opportunities (owner-only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db, utcnow
from app.core.errors import AppError
from app.core.ratelimit import hit
from app.models import User
from app.models.enums import MatchStatus
from app.schemas.opportunities import MatchDetail, MatchSummary
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
    hit("refresh-flights", str(user.id), 6, 3600)
    match = get_owned_match(db, user, match_id)
    if match.status in (MatchStatus.EXPIRED, MatchStatus.DISMISSED):
        raise AppError("This opportunity is no longer active.", code="inactive")
    match.flights_requested_at = utcnow()
    db.commit()
    # Never raises: if the request cannot be recorded, the scheduled run handles it.
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
