"""Read helpers: which model run is authoritative for each spot, and data freshness."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.services.forecasts.registry import configured_source_codes

USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)


def is_stale(run: ForecastRun, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return now - run.issued_at > timedelta(hours=get_settings().forecast_stale_hours)


def candidate_runs(db: Session, require_predictions: bool = True) -> list[ForecastRun]:
    codes = configured_source_codes()
    stmt = (
        select(ForecastRun)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.code.in_(codes), ForecastRun.status.in_(USABLE))
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(200)
    )
    if require_predictions:
        stmt = stmt.where(ForecastRun.predictions_computed_at.is_not(None))
    return list(db.scalars(stmt).all())


def latest_runs_by_spot(
    db: Session, now: datetime | None = None, require_predictions: bool = True
) -> dict[int, ForecastRun]:
    """Authoritative run per spot.

    Preference order: fresh runs over stale ones, then configured source priority, then
    the newest model run. A partial run only covers the spots listed in its details.
    """
    now = now or utcnow()
    codes = configured_source_codes()
    runs = candidate_runs(db, require_predictions)
    runs.sort(
        key=lambda r: (
            is_stale(r, now),
            codes.index(r.source.code) if r.source.code in codes else 99,
            -r.issued_at.timestamp(),
            -r.id,
        )
    )
    chosen: dict[int, ForecastRun] = {}
    for run in runs:
        for spot_id in run.details.get("spot_ids", []):
            chosen.setdefault(int(spot_id), run)
    return chosen


def forecast_status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    codes = configured_source_codes()
    sources: list[dict[str, Any]] = []
    for code in codes:
        src = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
        if src is None:
            sources.append(
                {
                    "code": code,
                    "name": code,
                    "latest_run": None,
                    "stale": True,
                    "is_demo": code == "demo",
                }
            )
            continue
        latest = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.source_id == src.id)
            .order_by(ForecastRun.started_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        latest_ok = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.source_id == src.id, ForecastRun.status.in_(USABLE))
            .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        sources.append(
            {
                "code": src.code,
                "name": src.name,
                "is_demo": src.is_demo,
                "attribution": src.attribution,
                "latest_attempt": _run_summary(latest),
                "latest_successful": _run_summary(latest_ok),
                "stale": latest_ok is None or is_stale(latest_ok, now),
            }
        )
    return {"sources": sources, "stale_after_hours": get_settings().forecast_stale_hours}


def _run_summary(run: ForecastRun | None) -> dict[str, object] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "run_key": run.run_key,
        "status": run.status.value,
        "issued_at": run.issued_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "spot_count": run.spot_count,
        "record_count": run.record_count,
        "error": run.error_message,
    }
