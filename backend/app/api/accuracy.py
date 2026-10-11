"""Developer accuracy dashboard (admin accounts only).

Verification of the offshore forecasts against buoy observations, the model registry and
the experiments behind it. Read-only.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.core.database import get_db
from app.core.errors import Forbidden
from app.models import User
from app.schemas.accuracy import AccuracyOverview, MetricRow, PairsOut
from app.services import accuracy_views

router = APIRouter(prefix="/api/admin/accuracy", tags=["accuracy"])

Variable = Literal["hs", "hs_h13", "tp", "dir"]


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("The accuracy dashboard is for admin accounts.")
    return user


@router.get("/overview", response_model=AccuracyOverview)
def accuracy_overview(
    _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> AccuracyOverview:
    return accuracy_views.overview(db)


@router.get("/metrics", response_model=list[MetricRow])
def accuracy_metrics(
    variable: Variable = "hs",
    scope: Literal["station", "region", "global", "spot"] | None = None,
    source: str | None = Query(None, max_length=40),
    window_days: int | None = Query(None, ge=1, le=3650),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[MetricRow]:
    return accuracy_views.metrics(
        db, variable=variable, scope=scope, source_code=source, window_days=window_days
    )


@router.get("/pairs", response_model=PairsOut)
def accuracy_pairs(
    variable: Variable = "hs",
    source: str | None = Query(None, max_length=40),
    model_version: str = Query("raw", max_length=60),
    station: str | None = Query(None, max_length=80),
    lead_min: float | None = Query(None, ge=0, le=400),
    lead_max: float | None = Query(None, ge=0, le=400),
    days: int = Query(90, ge=1, le=3650),
    limit: int = Query(1500, ge=1, le=5000),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PairsOut:
    return accuracy_views.pairs(
        db,
        variable=variable,
        source_code=source,
        model_version=model_version,
        station=station,
        lead_min=lead_min,
        lead_max=lead_max,
        days=days,
        limit=limit,
    )
