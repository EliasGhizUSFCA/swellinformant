"""Synced data pipeline: one coordinated, idempotent sync per new model run.

Contracts (foundation, frozen):

* :mod:`app.services.sync.types`: type aliases, the stage order and job names, and the
  plan / result / known-runs dataclasses;
* :mod:`app.services.sync.state`: Redis keys and helpers (daemon heartbeat, known run keys,
  kick flags), best effort;
* :mod:`app.services.sync.published`: the newest published sync and its map run.

Implemented by the sync-core package: ``detect`` (load_known, plan_sync), ``orchestrator``
(run_sync, tick), ``publish`` (publish), ``daemon`` (run_daemon) and ``cli`` (the
``python -m app.cli sync`` commands). Nothing here imports Celery, kombu or the heavy
data libraries at module level.
"""
