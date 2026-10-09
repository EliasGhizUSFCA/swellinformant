"""Notification history and one-click unsubscribe."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.core.errors import AppError
from app.core.ratelimit import rate_limit
from app.models import NotificationPreference, User
from app.schemas.common import Message
from app.schemas.opportunities import NotificationOut
from app.services.opportunity_views import list_notifications

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationPage(BaseModel):
    total: int
    items: list[NotificationOut]


@router.get("", response_model=NotificationPage)
def history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationPage:
    total, items = list_notifications(db, user, limit, offset)
    return NotificationPage(total=total, items=items)


@router.post(
    "/unsubscribe",
    response_model=Message,
    dependencies=[Depends(rate_limit("unsubscribe", 30, 900))],
)
async def unsubscribe(
    request: Request, token: str | None = Query(None, max_length=100), db: Session = Depends(get_db)
) -> Message:
    """Disable email alerts using the secret token from an alert email.

    Supports RFC 8058 one-click (form POST with ``List-Unsubscribe=One-Click`` and the
    token in the URL) and the web page (JSON body ``{"token": "..."}``). No session
    needed: possession of the per-user token is the authorisation.
    """
    if token is None and request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            token = str(body.get("token") or "")[:100]
        except ValueError:
            token = None
    if not token:
        raise AppError("Missing unsubscribe token.", code="invalid_token")
    prefs = db.scalar(
        select(NotificationPreference).where(NotificationPreference.unsubscribe_token == token)
    )
    if prefs is None:
        raise AppError("This unsubscribe link is invalid.", code="invalid_token")
    prefs.email_enabled = False
    db.commit()
    return Message(
        message="You've been unsubscribed from email alerts. You can re-enable them in settings."
    )
