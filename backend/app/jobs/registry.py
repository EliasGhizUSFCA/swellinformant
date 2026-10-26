"""Every background job by name: what it runs and how (lock TTL, judge, chaining).

Celery tasks (app.workers.tasks) and the sync daemon run jobs from this table through
:func:`app.jobs.runner.run_job`. The service imports stay inside the callables, so importing
the registry costs nothing and loads neither Celery nor the heavy data libraries; it also
lets tests patch a service function where it is defined.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.jobs.runner import Judge, judge_acquisition, judge_single
from app.jobs.schedule import DATA_HEALTH_TASK

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    """How to run one job: ``run(db)`` returns its details (a dict, or a value logged as
    ``{"result": value}``); ``lock_ttl`` is the job lock's TTL in seconds; ``judge`` turns
    the details into a status; ``chain`` lets a success queue NEXT_STAGE."""

    name: str
    run: Callable[[Session], Any]
    lock_ttl: int = 1800
    judge: Judge | None = None
    chain: bool = True


# ---------------------------------------------------------------- job bodies (lazy imports)
def _acquire_forecasts(db: Session) -> dict[str, Any]:
    from app.services.forecasts.ingestion import acquire_all

    return {"outcomes": [o.as_dict() for o in acquire_all(db)]}


def _blend(db: Session) -> dict[str, Any]:
    from app.services.forecasts.blend import build_blend

    return build_blend(db).as_dict()


def _predict(db: Session) -> Any:
    from app.services.surf_quality.predict import compute_pending_predictions

    return compute_pending_predictions(db)


def _detect(db: Session) -> dict[str, Any]:
    from app.services.swell_detection.service import detect_events

    return detect_events(db).as_dict()


def _match(db: Session) -> dict[str, Any]:
    from app.services.matching.service import match_all

    return match_all(db).as_dict()


def discover_flights_run(match_ids: Sequence[str] | None = None) -> Callable[[Session], Any]:
    """The flights.discover job body for these matches (None = every match due)."""

    def run(db: Session) -> dict[str, Any]:
        from app.services.flights.service import discover_flights

        ids = list(match_ids) if match_ids is not None else None
        return discover_flights(db, match_ids=ids).as_dict()

    return run


def _alerts(db: Session) -> dict[str, Any]:
    from app.services.notifications.alerts import generate_alerts

    return generate_alerts(db).as_dict()


def _deliver(db: Session) -> dict[str, Any]:
    from app.services.notifications.delivery import deliver_pending

    return deliver_pending(db).as_dict()


def _cleanup(db: Session) -> Any:
    from app.services.maintenance import cleanup

    return cleanup(db)


def _acquire_ensemble(db: Session) -> Any:
    from app.services.forecasts.ensemble import acquire_ensemble

    return acquire_ensemble(db)


def _map_fields(db: Session) -> dict[str, Any]:
    """Global map fields on their own (standalone mode only; otherwise a no-op).

    In "with_noaa" mode the NOAA acquisition stores the fields itself; with "off" nothing
    is fetched (see app.services.map_fields.map_fields_mode).
    """
    from app.core.config import get_settings
    from app.services.forecasts.ingestion import acquire_map_fields
    from app.services.map_fields import map_fields_mode

    mode = map_fields_mode(get_settings())
    if mode != "standalone":
        return {"skipped": f"map fields mode is {mode!r}, not 'standalone'"}
    return acquire_map_fields(db)


def _observations(db: Session) -> Any:
    from app.services.observations.ingest import ingest_all

    return ingest_all(db)


def _tides(db: Session) -> Any:
    from app.services.tides.ingest import acquire_tides

    return acquire_tides(db)


def _point_forecasts(db: Session) -> Any:
    from app.services.verification.point_forecasts import collect_recent

    return collect_recent(db)


def _verification(db: Session) -> Any:
    from app.services.verification.jobs import run_verification

    return run_verification(db)


def _check_staleness(db: Session) -> dict[str, Any]:
    from app.services.forecasts.queries import forecast_status

    status = forecast_status(db)
    stale = [s["code"] for s in status["sources"] if s["stale"]]
    if stale:
        logger.warning("Forecast data is stale for sources: %s", stale)
    return {"stale_sources": stale}


def _data_health(db: Session) -> Any:
    from app.services.notifications.ops_alerts import run_data_health_check

    return run_data_health_check(db)


def _sync_tick(db: Session) -> dict[str, Any]:
    """Celery beat's sync check: plan with the database, sync when due (the orchestrator
    opens its own sessions).

    The check's outcome ("not_due", "busy", or the sync's status) is logged as
    ``sync_status``: run_job's own ``status`` is the job's (success when the check ran).
    """
    from app.services.sync.orchestrator import tick

    out = dict(tick(trigger="celery"))
    if "status" in out:
        out["sync_status"] = out.pop("status")
    return out


def _sync_publish(db: Session) -> dict[str, Any]:
    from app.services.sync.publish import publish

    row = publish(db)
    return {
        "sync_run_id": row.id,
        "published_at": row.published_at,
        "map_run_id": row.map_run_id,
        "blend_run_id": row.blend_run_id,
    }


def _prune_pass(db: Session) -> dict[str, int]:
    from app.services.maintenance import prune_pass

    return prune_pass(db)


_SPECS = (
    JobSpec("forecasts.acquire", _acquire_forecasts, lock_ttl=3600, judge=judge_acquisition),
    JobSpec("forecasts.blend", _blend),
    JobSpec("quality.predict", _predict),
    JobSpec("detection.detect", _detect),
    JobSpec("matching.match", _match),
    JobSpec("flights.discover", discover_flights_run(), lock_ttl=3600),
    JobSpec("alerts.generate", _alerts),
    JobSpec("notifications.deliver", _deliver, lock_ttl=600),
    JobSpec("maintenance.cleanup", _cleanup, chain=False),
    JobSpec("forecasts.acquire_ensemble", _acquire_ensemble, lock_ttl=3600, judge=judge_single),
    JobSpec("forecasts.map_fields", _map_fields, lock_ttl=3600, judge=judge_single, chain=False),
    JobSpec("observations.ingest", _observations, lock_ttl=1800),
    JobSpec("tides.acquire", _tides, lock_ttl=1800),
    JobSpec("verification.collect_point_forecasts", _point_forecasts),
    JobSpec("verification.run", _verification, lock_ttl=3600),
    JobSpec("forecasts.check_staleness", _check_staleness, chain=False),
    JobSpec(DATA_HEALTH_TASK, _data_health, lock_ttl=600, chain=False),
    # The synced pipeline (app.services.sync).
    JobSpec("sync.tick", _sync_tick, lock_ttl=3600, chain=False),
    JobSpec("sync.publish", _sync_publish, chain=False),
    JobSpec("maintenance.prune_pass", _prune_pass, chain=False),
)

JOB_SPECS: dict[str, JobSpec] = {s.name: s for s in _SPECS}


def job_spec(name: str) -> JobSpec:
    """The spec of job ``name``; KeyError names the known jobs."""
    try:
        return JOB_SPECS[name]
    except KeyError:
        raise KeyError(f"unknown job {name!r}; known: {sorted(JOB_SPECS)}") from None
