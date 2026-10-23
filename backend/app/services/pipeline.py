"""Run jobs A→G in order, synchronously (A: acquisition, then multi-model blending).

Used by the developer "run pipeline" endpoint, the CLI and tests. The synced pipeline
(app.services.sync, docs/sync.md) runs the same service functions as the stages of one
sync. After the predictions the pipeline publishes what is stored (a ``sync_runs`` row
with trigger "pipeline"), so the map index moves to the new map run here too. At the end
it calls the public snapshot cache's hook (``public_cache.note_published``) as a sync does,
so the developer endpoints and the CLI never leave the public pages on older data for the
rest of the hour.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.flights.service import discover_flights
from app.services.forecasts.blend import build_blend
from app.services.forecasts.ingestion import acquire_all
from app.services.matching.service import match_all
from app.services.notifications.alerts import generate_alerts
from app.services.notifications.delivery import deliver_pending
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.swell_detection.service import detect_events
from app.services.sync.publish import publication_summary, publish


def run_pipeline(
    db: Session,
    *,
    now: datetime | None = None,
    acquire: bool = True,
    deliver: bool = True,
    force_acquire: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def stage(name: str, fn: Callable[[], Any]) -> None:
        # Each stage sees fresh database state, exactly like separate worker jobs do.
        db.expire_all()
        out[name] = fn()

    if acquire:
        stage(
            "acquisition",
            lambda: [o.as_dict() for o in acquire_all(db, now=now, force=force_acquire)],
        )
    stage("blending", lambda: build_blend(db, now).as_dict())
    stage("predictions", lambda: compute_pending_predictions(db, now))
    # The wall clock, not ``now``: a publication is a real event (detect.load_known
    # compares it with the runs' completed_at).
    published: list[int] = []

    def publish_stage() -> dict[str, Any]:
        row = publish(db, trigger="pipeline")
        published.append(row.id)
        return publication_summary(row)

    stage("publish", publish_stage)
    try:
        stage("detection", lambda: detect_events(db, now).as_dict())
        stage("matching", lambda: match_all(db, now).as_dict())
        stage("flights", lambda: discover_flights(db, now).as_dict())
        stage("alerts", lambda: generate_alerts(db, now).as_dict())
        if deliver:
            stage("delivery", lambda: deliver_pending(db, now).as_dict())
    finally:
        # The stages commit their own work: the public snapshots (app.services.public_cache)
        # show the publication and what followed it from the next request on, as after a
        # sync, instead of from the next hour (also when a later stage failed).
        from app.services import public_cache

        public_cache.note_published(published[0])
    db.expire_all()
    return out
