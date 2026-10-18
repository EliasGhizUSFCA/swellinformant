"""The newest published sync (foundation contract F3; frozen).

The publish stage of a sync sets ``sync_runs.published_at`` and ``map_run_id``; the map index
serves that map run (SYNC_PUBLISH_GATE) and the status page shows the publication.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sync import SyncRun


def latest_publication(db: Session) -> SyncRun | None:
    """The newest sync with a publication (by published_at, then id), or None."""
    return db.scalar(
        select(SyncRun)
        .where(SyncRun.published_at.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
    )


def published_map_run_id(db: Session) -> int | None:
    """The map run of the newest publication; None when nothing was published or that
    publication had no servable map run."""
    return db.scalar(
        select(SyncRun.map_run_id)
        .where(SyncRun.published_at.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
