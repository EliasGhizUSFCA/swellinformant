"""Run jobs A→G in order, synchronously.

Used by the developer "run pipeline" endpoint, the CLI and tests. The Celery workers run
the same service functions as separate chained tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.flights.service import discover_flights
from app.services.forecasts.ingestion import acquire_all
from app.services.matching.service import match_all
from app.services.notifications.alerts import generate_alerts
from app.services.notifications.delivery import deliver_pending
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.swell_detection.service import detect_events


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
    stage("predictions", lambda: compute_pending_predictions(db))
    stage("detection", lambda: detect_events(db, now).as_dict())
    stage("matching", lambda: match_all(db, now).as_dict())
    stage("flights", lambda: discover_flights(db, now).as_dict())
    stage("alerts", lambda: generate_alerts(db, now).as_dict())
    if deliver:
        stage("delivery", lambda: deliver_pending(db, now).as_dict())
    db.expire_all()
    return out
