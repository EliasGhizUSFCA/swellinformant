"""The publish stage (docs/sync.md).

:func:`publish` commits ``published_at``, ``map_run_id`` (the best servable map run,
``map_fields.servable_runs(db, limit=1)``, or None) and ``blend_run_id`` (the newest blend
run with predictions) on ``sync_run``, or on a new ``sync_runs`` row with status success
when ``sync_run`` is None (the CLI and dev pipelines), and returns that row.

The map index serves the newest publication's map run (SYNC_PUBLISH_GATE); spot pages
switch when the new predictions commit, in the stage just before. A run stored after the
newest publication counts as not stored yet for the next check (detect.load_known), so
data a sync stored but did not publish is published by the next sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.services.sync.types import SyncTrigger

BLEND_CODE = "blend"  # app.services.forecasts.blend.BLEND_CODE (not imported: pandas)
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)


def newest_blend_run_id(db: Session) -> int | None:
    """The newest usable blend run whose predictions are computed, or None."""
    return db.scalar(
        select(ForecastRun.id)
        .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
        .where(
            ForecastSource.code == BLEND_CODE,
            ForecastRun.status.in_(USABLE),
            ForecastRun.predictions_computed_at.is_not(None),
        )
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def publish(
    db: Session,
    *,
    sync_run: SyncRun | None = None,
    trigger: SyncTrigger = "pipeline",
    now: datetime | None = None,
) -> SyncRun:
    """Publish what is stored now: one commit sets the publication on ``sync_run`` (or on
    a new ``sync_runs`` row with status success, ``trigger`` and ``plan.published_by``).

    ``now`` is the publication time; the default, the wall clock, is what the synced
    pipeline always uses (load_known compares it with the runs' ``completed_at``).
    """
    from app.services.map_fields import servable_runs

    at = now or utcnow()
    if sync_run is None:
        sync_run = SyncRun(
            trigger=trigger,
            status="success",
            started_at=at,
            finished_at=at,
            plan={"published_by": trigger},
            stages=[],
        )
        db.add(sync_run)
    best = servable_runs(db, limit=1)
    sync_run.published_at = at
    sync_run.map_run_id = best[0].run_id if best else None
    sync_run.blend_run_id = newest_blend_run_id(db)
    db.commit()
    return sync_run


def publication_summary(row: SyncRun) -> dict[str, Any]:
    """What a publication set (job log details, CLI output)."""
    return {
        "sync_run_id": row.id,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "map_run_id": row.map_run_id,
        "blend_run_id": row.blend_run_id,
    }
