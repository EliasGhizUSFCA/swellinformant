"""The sync daemon, ``python -m app.cli sync daemon`` (interface F7; implemented by the
sync-core package).

:func:`run_daemon` optionally initialises the database (SYNC_DAEMON_INIT_DB), then loops
until ``stop`` is set (SIGTERM): every SYNC_DAEMON_POLL_SECONDS it takes the API's kicks,
every SYNC_CHECK_MINUTES it plans from the known runs and syncs when due, and it runs the
periodic jobs of app.jobs.schedule (all but sync.tick). Returns the process exit code.
Never imports Celery or kombu.
"""

from __future__ import annotations

import threading

NOT_IMPLEMENTED = "the sync daemon is not implemented yet (sync-core package)"


def run_daemon(
    *,
    stop: threading.Event | None = None,
    once: bool = False,
    init_db: bool | None = None,
) -> int:
    raise NotImplementedError(NOT_IMPLEMENTED)
