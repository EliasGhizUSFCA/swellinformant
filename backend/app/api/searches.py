"""Saved surf travel searches (owner-only)."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.core.errors import AppError
from app.models import OpportunityMatch, SavedSearch, User
from app.schemas.common import Message
from app.schemas.opportunities import MatchSummary
from app.schemas.searches import SearchIn, SearchOut
from app.services import searches as svc
from app.services.opportunity_views import list_matches

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/searches", tags=["searches"])


def _kick_matching() -> None:
    """Evaluate searches promptly instead of waiting for the next scheduled run."""
    try:
        from app.workers.celery_app import celery_app

        celery_app.signature("matching.match").delay()
    except Exception:  # broker down: the beat schedule will pick it up
        logger.warning("Could not enqueue matching; it will run on schedule")


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
    return [svc.search_out(db, s, counts[s.id]) for s in rows]


@router.post("", response_model=SearchOut, status_code=status.HTTP_201_CREATED)
def create(
    body: SearchIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchOut:
    search = svc.create_search(db, user, body)
    _kick_matching()
    return svc.search_out(db, search)


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
