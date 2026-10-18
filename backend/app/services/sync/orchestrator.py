"""One coordinated sync (interface F7; implemented by the sync-core package).

:func:`run_sync` takes the lease (a ``sync_runs`` row with status running), runs the
``STAGES`` in order through app.jobs.runner.run_job, records each stage with its CPU time,
publishes, and releases the lease. It opens its own sessions and raises :class:`SyncBusy`
when another sync holds a valid lease. :func:`tick` plans with the database and runs a sync
when one is due (Celery's ``sync.tick`` and ``python -m app.cli sync run --if-due``).
"""

from __future__ import annotations

import threading
from collections.abc import Collection
from datetime import datetime
from typing import Any

from app.services.sync.types import ComponentKind, SyncBusy, SyncPlan, SyncResult, SyncTrigger

__all__ = ["SyncBusy", "run_sync", "tick"]

NOT_IMPLEMENTED = "the sync orchestrator is not implemented yet (sync-core package)"


def run_sync(
    *,
    trigger: SyncTrigger,
    force: bool = False,
    only: Collection[ComponentKind] | None = None,
    now: datetime | None = None,
    stop: threading.Event | None = None,
    plan: SyncPlan | None = None,
) -> SyncResult:
    raise NotImplementedError(NOT_IMPLEMENTED)


def tick(*, trigger: SyncTrigger) -> dict[str, Any]:
    raise NotImplementedError(NOT_IMPLEMENTED)
