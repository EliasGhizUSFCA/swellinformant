"""The publish stage (interface F7; implemented by the sync-core package).

:func:`publish` commits ``published_at``, ``map_run_id`` (the best servable map run) and
``blend_run_id`` (the newest blend run with predictions) on ``sync_run``, or on a new
``sync_runs`` row with status success when ``sync_run`` is None (the CLI and dev
pipelines), and returns that row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.sync import SyncRun
from app.services.sync.types import SyncTrigger

NOT_IMPLEMENTED = "sync publishing is not implemented yet (sync-core package)"


def publish(
    db: Session,
    *,
    sync_run: SyncRun | None = None,
    trigger: SyncTrigger = "pipeline",
    now: datetime | None = None,
) -> SyncRun:
    raise NotImplementedError(NOT_IMPLEMENTED)
