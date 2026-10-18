"""Developer tools for demo mode (only mounted when ENABLE_DEV_ENDPOINTS=true).

Production refuses to start with dev endpoints enabled (see Settings validation).

The pipeline is imported inside the endpoints: it loads the blend's data libraries
(pandas, scikit-learn), which a lean API install does not have.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.config import get_settings
from app.core.database import get_db, utcnow
from app.core.errors import AppError, Forbidden, NotFound
from app.core.ratelimit import hit
from app.models import EmailMessage, SurfSpot, User
from app.models.enums import EmailPurpose
from app.services.forecasts.ingestion import simulate_swell
from app.services.notifications.providers import read_outbox
from app.services.notifications.templates import render_test_email
from app.services.notifications.transactional import queue_account_email

router = APIRouter(prefix="/api/dev", tags=["developer tools"])


class SimulateIn(BaseModel):
    spot_slug: str
    days_ahead: float = Field(7.0, ge=1, le=14)
    duration_hours: int = Field(60, ge=12, le=168)
    target_breaking_ft: float | None = Field(None, gt=0, le=80)
    period_s: float | None = Field(None, ge=6, le=24)
    run_pipeline: bool = True
    # False stops after alert generation so callers can observe QUEUED notifications.
    deliver: bool = True


@router.post("/simulate-swell")
def simulate(
    body: SimulateIn, _: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    if "demo" not in get_settings().forecast_provider_list:
        raise AppError("Swell simulation requires FORECAST_PROVIDERS=demo.", code="not_demo")
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == body.spot_slug))
    if spot is None:
        raise NotFound("Surf spot not found.")
    start = (utcnow() + timedelta(days=body.days_ahead)).replace(minute=0, second=0, microsecond=0)
    outcome = simulate_swell(
        db,
        spot,
        start=start,
        duration_hours=body.duration_hours,
        period_s=body.period_s,
        target_breaking_ft=body.target_breaking_ft,
    )
    result: dict[str, Any] = {"simulation": outcome.as_dict(), "swell_start": start.isoformat()}
    if body.run_pipeline:
        from app.services.pipeline import run_pipeline

        result["pipeline"] = run_pipeline(db, acquire=False, deliver=body.deliver)
    return result


@router.post("/run-pipeline")
def pipeline(
    acquire: bool = Query(True, description="Fetch forecasts first (false: use what is stored)"),
    deliver: bool = Query(True, description="False stops after alert generation (QUEUED)"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.services.pipeline import run_pipeline

    return run_pipeline(db, acquire=acquire, deliver=deliver)


@router.get("/outbox")
def outbox(
    limit: int = Query(20, ge=1, le=200), _: User = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Messages captured by the console email/SMS adapters (demo only)."""
    return read_outbox(limit=limit)


def _email_status(row: EmailMessage) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "purpose": row.purpose.value,
        "status": row.status.value,
        "provider": row.provider,
        "provider_message_id": row.provider_message_id,
        "attempts": row.attempts,
        "error": row.last_error,
        "created_at": row.created_at.isoformat(),
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
        "events": row.events,
        "meaning": {
            "queued": "waiting for the worker",
            "sending": "worker is talking to the provider",
            "accepted": "provider accepted it; delivery not yet confirmed",
            "captured": "captured by the development inbox; not delivered to a real mailbox",
            "delivered": "provider confirmed delivery (webhook)",
            "bounced": "provider reported a bounce",
            "complained": "recipient marked it as spam",
            "rejected": "provider refused or dropped it",
            "failed": "could not be handed to the provider",
        }[row.status.value],
    }


@router.post("/test-email")
def send_test_email(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Send a test message to the signed-in developer's own verified address.

    Restricted to admins (``python -m app.cli make-admin EMAIL``), only to their own
    verified address (never an arbitrary recipient, so it cannot be used as a relay),
    and rate-limited."""
    if not user.is_admin:
        raise Forbidden("Only admin accounts can send test emails.")
    if user.email_verified_at is None:
        raise AppError("Verify your email address first.", code="email_not_verified")
    hit("dev-test-email", str(user.id), 5, 3600)
    s = get_settings()
    subject, text, html = render_test_email(user.full_name, s.email_provider, s.app_base_url)
    row = queue_account_email(db, user, EmailPurpose.TEST, subject, text, html)
    db.refresh(row)
    return _email_status(row)


@router.get("/test-email/{message_id}")
def test_email_status(
    message_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    row = db.scalar(
        select(EmailMessage).where(EmailMessage.id == message_id, EmailMessage.user_id == user.id)
    )
    if row is None:
        raise NotFound("No such test email.")
    return _email_status(row)
