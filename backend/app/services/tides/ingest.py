"""The daily ``tides.acquire`` job: store the tide predictions of every spot's station.

Not implemented yet: the job does nothing and says so in its result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session


def acquire_tides(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Fetch or compute predictions for TIDE_HORIZON_DAYS; a summary for the job log."""
    return {"status": "skipped", "reason": "tide acquisition is not implemented yet"}
