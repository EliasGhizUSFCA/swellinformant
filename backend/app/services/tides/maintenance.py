"""Retention of tide predictions (called by app.services.maintenance.cleanup).

Predictions are recomputable (CO-OPS serves them for any period, the harmonic model is
evaluated locally), so only the future ones and the last TIDE_RETENTION_PAST_DAYS days
are kept: about 1,000 rows per station.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import TidePrediction


def prune_tide_predictions(db: Session, now: datetime) -> int:
    """Delete predictions older than TIDE_RETENTION_PAST_DAYS; the number of rows deleted.

    The caller commits.
    """
    cutoff = now - timedelta(days=get_settings().tide_retention_past_days)
    result = db.execute(delete(TidePrediction).where(TidePrediction.valid_time < cutoff))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
