"""Developer tools for demo mode (only mounted when ENABLE_DEV_ENDPOINTS=true).

Production refuses to start with dev endpoints enabled (see Settings validation).
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
from app.core.errors import AppError, NotFound
from app.models import SurfSpot, User
from app.services.forecasts.ingestion import simulate_swell
from app.services.notifications.providers import read_outbox
from app.services.pipeline import run_pipeline

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
        result["pipeline"] = run_pipeline(db, acquire=False, deliver=body.deliver)
    return result


@router.post("/run-pipeline")
def pipeline(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return run_pipeline(db)


@router.get("/outbox")
def outbox(
    limit: int = Query(20, ge=1, le=200), _: User = Depends(get_current_user)
) -> list[dict[str, object]]:
    """Messages captured by the console email/SMS adapters (demo only)."""
    return read_outbox(limit=limit)
