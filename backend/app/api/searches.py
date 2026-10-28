"""Saved surf travel searches (owner-only)."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.config import get_settings
from app.core.database import get_db
from app.core.errors import AppError
from app.models import OpportunityMatch, SavedSearch, User
from app.schemas.common import Message
from app.schemas.opportunities import MatchSummary
from app.schemas.searches import MAX_ALERT_LEAD_DAYS, SearchDefaultsOut, SearchIn, SearchOut
from app.services import searches as svc
from app.services.flights.service import MAX_CONNECTIONS, MAX_ORIGINS
from app.services.opportunity_views import list_matches
from app.workers.dispatch import kick

router = APIRouter(prefix="/api/searches", tags=["searches"])


def _kick_matching() -> None:
    """Evaluate searches promptly instead of waiting for the next scheduled run (never
    raises; under JOB_RUNNER=daemon a Redis kick flag the daemon picks up)."""
    kick("matching.match")


@router.get("", response_model=list[SearchOut])
def list_searches(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[SearchOut]:
    rows = db.scalars(
        select(SavedSearch)
        .where(SavedSearch.user_id == user.id)
        .order_by(SavedSearch.created_at.desc())
    ).all()
    counts: dict[uuid.UUID, Counter[str]] = defaultdict(Counter)
    for search_id, st, n in db.execute(
        select(OpportunityMatch.search_id, OpportunityMatch.status, func.count())
        .where(OpportunityMatch.user_id == user.id)
        .group_by(OpportunityMatch.search_id, OpportunityMatch.status)
    ).all():
        counts[search_id][st.value] = n
    slugs = svc.spot_slugs_of(db, rows)
    return [svc.search_out(db, s, counts[s.id], slugs[s.id]) for s in rows]


@router.post("", response_model=SearchOut, status_code=status.HTTP_201_CREATED)
def create(
    body: SearchIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    search = svc.create_search(db, user, body)
    _kick_matching()
    return svc.search_out(db, search)


@router.get("/defaults", response_model=SearchDefaultsOut)
def defaults(user: User = Depends(get_current_user)) -> SearchDefaultsOut:
    """The deployment's defaults and limits for a new search (the wizard's starting point)."""
    s = get_settings()
    return SearchDefaultsOut(
        alert_lead_min_days=s.alert_lead_default_min_days,
        alert_lead_max_days=s.alert_lead_default_max_days,
        forecast_days=s.forecast_days,
        detection_lead_min_days=s.detection_lead_min_days,
        detection_lead_max_days=s.detection_lead_max_days,
        short_notice_min_days=s.short_notice_min_days,
        short_notice_max_days=s.short_notice_max_days,
        max_origins=MAX_ORIGINS,
        max_layovers=MAX_CONNECTIONS,
        max_alert_lead_days=MAX_ALERT_LEAD_DAYS,
        flight_searches_per_day=s.flight_user_daily_quota,
    )


@router.get("/{search_id}", response_model=SearchOut)
def get(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    return svc.search_out(db, svc.get_owned(db, user, search_id))


@router.put("/{search_id}", response_model=SearchOut)
def update(
    search_id: uuid.UUID,
    body: SearchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SearchOut:
    search = svc.update_search(db, svc.get_owned(db, user, search_id), body)
    _kick_matching()
    return svc.search_out(db, search)


@router.post("/{search_id}/pause", response_model=SearchOut)
def pause(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    return svc.search_out(db, svc.set_paused(db, svc.get_owned(db, user, search_id), True))


@router.post("/{search_id}/resume", response_model=SearchOut)
def resume(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    search = svc.set_paused(db, svc.get_owned(db, user, search_id), False)
    _kick_matching()
    return svc.search_out(db, search)


@router.post(
    "/{search_id}/duplicate", response_model=SearchOut, status_code=status.HTTP_201_CREATED
)
def duplicate(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    original = svc.get_owned(db, user, search_id)
    try:
        copy = svc.duplicate_search(db, user, original)
    except ValidationError as exc:
        raise AppError(
            "This search can't be copied as-is (for example its fixed dates are in the past). "
            "Edit it first, then duplicate.",
            code="cannot_duplicate",
        ) from exc
    return svc.search_out(db, copy)


@router.delete("/{search_id}", response_model=Message)
def delete(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Message:
    svc.delete_search(db, svc.get_owned(db, user, search_id))
    return Message(message="Search deleted.")


@router.get("/{search_id}/matches", response_model=list[MatchSummary])
def matches(
    search_id: uuid.UUID, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[MatchSummary]:
    search = svc.get_owned(db, user, search_id)
    return list_matches(db, user, search_id=search.id, include_past=True, limit=200)
