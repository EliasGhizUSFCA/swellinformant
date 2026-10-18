"""Model-run detection: is a sync due? (interface F7; implemented by the sync-core package).

* :func:`load_known` reads the newest stored run key per configured source and the newest
  servable map cycle from the database.
* :func:`plan_sync` asks each provider for its newest available run (metadata only:
  Open-Meteo meta.json, NOAA NODD .idx HEADs) and applies the trigger, coalescing and
  retry rules. With ``db=None`` it works from ``known`` alone (the daemon's idle check
  needs no database). Provider errors never raise: the component is ``unknown``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.sync.types import KnownRuns, SyncPlan

NOT_IMPLEMENTED = "sync detection is not implemented yet (sync-core package)"


def load_known(db: Session) -> KnownRuns:
    raise NotImplementedError(NOT_IMPLEMENTED)


def plan_sync(
    db: Session | None,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    known: KnownRuns | None = None,
    force: bool = False,
) -> SyncPlan:
    raise NotImplementedError(NOT_IMPLEMENTED)
