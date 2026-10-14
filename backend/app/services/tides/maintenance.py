"""Retention of tide predictions (called by app.services.maintenance.cleanup).

Not implemented yet: nothing is stored, so nothing is deleted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session


def prune_tide_predictions(db: Session, now: datetime) -> int:
    """Delete predictions older than TIDE_RETENTION_PAST_DAYS; the number of rows deleted.

    The caller commits.
    """
    return 0
