"""Tide freshness for the data-health report (called by app.services.health.data_health).

Each problem is a dict with the keys of ``app.services.health.Problem``: ``key`` (stable
while the problem persists, e.g. "tides_stale:noaa_coops"), ``severity`` ("degraded" or
"failing"), ``message`` (public, plain language) and optionally ``detail`` (admin email
only). Not implemented yet: no problems.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session


def tide_freshness(db: Session, now: datetime) -> list[dict[str, Any]]:
    """Problems with the stored tide predictions (e.g. none left for the coming days)."""
    return []
