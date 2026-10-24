"""One coordinated sync (interface F7; docs/sync.md).

:func:`run_sync` takes the lease (a ``sync_runs`` row with status running; the unique
partial index ``uq_sync_runs_running`` allows one), runs the :data:`STAGES` in order through
app.jobs.runner.run_job (one ``background_job_logs`` row per stage under the existing job
names, ``chain=False``), records each stage with its duration and CPU time in
``sync_runs.stages``, renews the lease after every stage, publishes, and releases the lease.
While a stage runs, a keeper thread extends the lease every third of SYNC_LEASE_MINUTES, so
a stage longer than the lease (a full map stage on one vCPU) keeps it and only a process
that died lets it lapse. It opens its own sessions and raises :class:`SyncBusy` when another
sync holds a valid lease; an expired lease is marked ``abandoned`` and taken over (after a
killed deploy, at most SYNC_LEASE_MINUTES later: 15 under the daemon by default).

Stage rules:

* ``spots``: ``acquire_all(only=...)`` with the FORECAST_PROVIDERS tokens of the new spot
  components (every configured one when forced); ``ensemble`` and ``map`` only when new
  (or forced). Acquisitions are idempotent: a run key already stored is skipped.
* ``blend``: only with new member data (spot runs or the ensemble), or unpublished member
  data, or forced. ``predict`` always runs (it scores pending runs only). Either one
  whose job lock another holder has (e.g. a daemon killed mid-stage: the lock outlives it
  by its TTL) did not run: the stage is failed, not skipped.
* ``publish``: when an acquisition stored new data, data stored earlier is unpublished, or
  forced; but never after a failed ``predict`` (unless forced), so the map and the spot
  pages never switch to runs that were not scored. The runs then stay unpublished, and
  the components this sync acquired or found stored count as failed attempts: the next
  check retries after SYNC_RETRY_MINUTES, at most SYNC_MAX_ATTEMPTS times per run (a
  predict that fails every time does not re-sync forever). ``detect``, ``match``,
  ``flights``, ``alerts`` and ``deliver`` run only after a publish; ``prune`` too, and
  only with SYNC_PRUNE_EACH_PASS.
* ``stop`` (SIGTERM in the daemon) is checked between stages: the current stage finishes,
  the rest is recorded as skipped and the lease is released.

Status: success; partial (some component or stage failed, or the sync was stopped);
failed (nothing published, and something failed); skipped (nothing new after all);
abandoned (the lease was lost). :func:`tick` plans with the database and syncs when one is
due (Celery's ``sync.tick`` and ``python -m app.cli sync run --if-due``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, utcnow
from app.core.logging import redact
from app.jobs.registry import job_spec
from app.jobs.runner import cpu_seconds, run_job
from app.models import BackgroundJobLog, ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus, JobStatus
from app.models.sync import SyncRun
from app.services.sync import state
from app.services.sync.detect import (
    BLEND_CODE,
    apply_result,
    empty_known,
    plan_sync,
    refresh_known,
)
from app.services.sync.types import (
    STAGE_JOB_NAMES,
    STAGES,
    SYNC_RUN_JOB,
    ComponentKind,
    ComponentStatus,
    KnownRuns,
    StageName,
    StageResult,
    StageStatus,
    SyncBusy,
    SyncPlan,
    SyncResult,
    SyncStatus,
    SyncTrigger,
    jsonable,
)

__all__ = ["SyncBusy", "remember", "run_sync", "tick"]

logger = logging.getLogger(__name__)

LEASE_INDEX = "uq_sync_runs_running"
ACQUISITION_STAGES: tuple[StageName, ...] = ("spots", "ensemble", "map")
AFTER_PUBLISH: tuple[StageName, ...] = ("detect", "match", "flights", "alerts", "deliver")
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
# Stage details stored in sync_runs.stages are compacted (lists, strings).
DETAIL_LIST_MAX = 20
DETAIL_TEXT_MAX = 300
# Acquisition messages that mean "this run is stored already".
ALREADY_STORED = frozenset(
    {"run already ingested", "already stored", "fields of this cycle already stored"}
)
# ... and "another runner is storing this run": a failed attempt (retried after
# SYNC_RETRY_MINUTES; ingestion re-acquires a run left running for STUCK_RUN_AFTER, 1 h, which
# is longer than the lease). Any other skipped acquisition is a run the provider does not
# serve unmixed (RunNotServedError): "deferred".
IN_PROGRESS = frozenset({"run in progress", "in progress"})
_STAGE_KIND: dict[StageName, ComponentKind] = {
    "spots": "spots",
    "ensemble": "ensemble",
    "map": "map",
}
# Stages the publication depends on: one that did not run (its job lock was held) failed.
REQUIRED_STAGES: frozenset[StageName] = frozenset({"blend", "predict"})
PUBLISH_HELD = "predict did not complete"
HELD_MESSAGE = "stored, not published: predict did not complete"


def _holder() -> str:
    host = socket.gethostname()[:80] or "host"
    return f"{host}:{os.getpid()}:{secrets.token_hex(4)}"[:120]


def _text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:2000]


def _compact(value: Any, depth: int = 0) -> Any:
    """A bounded copy of a job result for sync_runs.stages (the full one is in the job log)."""
    if isinstance(value, str):
        return value[:DETAIL_TEXT_MAX]
    if depth >= 4:
        return None if isinstance(value, dict | list | tuple) else value
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list | tuple):
        return [_compact(v, depth + 1) for v in value[:DETAIL_LIST_MAX]]
    return value


# --------------------------------------------------------------------------- the lease
@dataclass
class _Lease:
    row_id: int
    holder: str
    minutes: int

    @classmethod
    def acquire(cls, *, trigger: SyncTrigger, plan: SyncPlan, settings: Settings) -> _Lease:
        """INSERT the running row; on the unique violation take over an expired lease
        (marked abandoned) or raise SyncBusy."""
        holder = _holder()
        minutes = settings.sync_lease_minutes
        for _ in range(3):
            with SessionLocal() as db:
                now = utcnow()
                row = SyncRun(
                    trigger=trigger,
                    status="running",
                    started_at=now,
                    lease_expires_at=now + timedelta(minutes=minutes),
                    holder=holder,
                    anchor_cycle=plan.anchor_cycle,
                    plan=jsonable(plan.as_dict()),
                    stages=[],
                )
                db.add(row)
                try:
                    db.commit()
                    return cls(row.id, holder, minutes)
                except IntegrityError as exc:
                    db.rollback()
                    if LEASE_INDEX not in str(exc.orig):
                        raise
                current = db.scalar(
                    select(SyncRun).where(SyncRun.status == "running").with_for_update()
                )
                if current is None:
                    continue  # released meanwhile
                expires = current.lease_expires_at
                if expires is not None and expires > utcnow():
                    raise SyncBusy(current.holder, expires)
                current.status = "abandoned"
                current.finished_at = utcnow()
                current.error_message = redact(
                    f"lease expired ({expires.isoformat() if expires else 'no expiry'}, holder "
                    f"{current.holder or 'unknown'}); taken over by {holder}"
                )[:2000]
                db.commit()
                logger.warning(
                    "Sync %s: lease of %s expired; marked abandoned", current.id, current.holder
                )
        raise SyncBusy()

    def _update(self, values: dict[str, Any]) -> bool:
        with SessionLocal() as db:
            res = db.execute(
                update(SyncRun)
                .where(
                    SyncRun.id == self.row_id,
                    SyncRun.status == "running",
                    SyncRun.holder == self.holder,
                )
                .values(**values)
            )
            db.commit()
            return int(getattr(res, "rowcount", 0) or 0) == 1

    def extend(self) -> bool:
        """Push the expiry SYNC_LEASE_MINUTES ahead (the keeper); False when it was lost."""
        return self._update({"lease_expires_at": utcnow() + timedelta(minutes=self.minutes)})

    def renew(self, stages: list[StageResult]) -> bool:
        """Store the stages so far and extend the lease; False when the lease was lost."""
        return self._update(
            {
                "stages": [s.as_dict() for s in stages],
                "lease_expires_at": utcnow() + timedelta(minutes=self.minutes),
            }
        )

    def release(self, status: SyncStatus, stages: list[StageResult], error: str | None) -> bool:
        return self._update(
            {
                "status": status,
                "finished_at": utcnow(),
                "lease_expires_at": None,
                "stages": [s.as_dict() for s in stages],
                "error_message": error,
            }
        )


class _LeaseKeeper:
    """Extends ``lease`` every ``interval_s`` (a third of the lease) on a thread of its own
    until stopped, so a stage that outlasts the lease keeps it. Stops by itself when the
    lease was lost (the stage loop sees that at its next renewal). Database errors are
    logged and retried at the next interval."""

    def __init__(self, lease: _Lease, interval_s: float | None = None) -> None:
        self.lease = lease
        self.interval_s = interval_s if interval_s is not None else lease.minutes * 60 / 3
        self.extended = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sync-lease-keeper", daemon=True)

    def __enter__(self) -> _LeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                kept = self.lease.extend()
            except Exception as exc:  # e.g. the database went away for a moment
                logger.warning("Sync lease not extended: %s", type(exc).__name__)
                continue
            if not kept:
                logger.warning("Sync %s lost its lease", self.lease.row_id)
                return
            self.extended += 1


def _valid_lease(db: Session) -> SyncRun | None:
    row = db.scalar(select(SyncRun).where(SyncRun.status == "running").limit(1))
    if row is None or row.lease_expires_at is None or row.lease_expires_at <= utcnow():
        return None
    return row


def _unpublished(db: Session) -> set[str]:
    """What is stored but newer than the newest publication: "member" (spot or ensemble
    runs) and/or "map" (map-field runs). Everything stored, before the first publication."""
    published_at = db.scalar(select(func.max(SyncRun.published_at)))
    stmt = (
        select(ForecastSource.kind)
        .join(ForecastRun, ForecastRun.source_id == ForecastSource.id)
        .where(ForecastRun.status.in_(USABLE), ForecastSource.code != BLEND_CODE)
        .distinct()
    )
    if published_at is not None:
        stmt = stmt.where(ForecastRun.completed_at > published_at)
    return {"map" if kind == "fields" else "member" for kind in db.scalars(stmt)}


# --------------------------------------------------------------------------- one sync
@dataclass
class _Sync:
    plan: SyncPlan
    settings: Settings
    trigger: SyncTrigger
    now: datetime | None
    only: frozenset[ComponentKind] | None
    row_id: int = 0
    stored: set[ComponentKind] = field(default_factory=set)
    failed: list[str] = field(default_factory=list)
    unpublished: set[str] = field(default_factory=set)
    published_at: datetime | None = None
    publish_failed: bool = False
    predict_ok: bool = True
    # The acquisition stages' component outcomes (the dicts in their stage details).
    components: list[dict[str, Any]] = field(default_factory=list)

    def wanted(self, kind: ComponentKind) -> list[ComponentStatus]:
        if self.only is not None and kind not in self.only:
            return []
        return [
            c
            for c in self.plan.components
            if c.kind == kind and (c.state == "new" or (self.plan.forced and c.state != "disabled"))
        ]


def _outcome(status: str | None, message: str | None) -> str:
    """stored, current, deferred or failed (see ALREADY_STORED and IN_PROGRESS).

    ``deferred``: the provider stopped serving the run unmixed between the check and the
    acquisition (Open-Meteo publishing its next run, or serving a run this source does not
    read). That is no failure: no attempt is counted, and the next check sees the
    component waiting ("not served yet") or offers the next run.
    """
    if status in ("success", "partial"):
        return "stored"
    if status == "skipped":
        text = message or ""
        if text in ALREADY_STORED:
            return "current"
        if text not in IN_PROGRESS:
            return "deferred"
    return "failed"


def _components(
    ctx: _Sync, comps: list[ComponentStatus], details: dict[str, Any] | None, stage: StageResult
) -> list[dict[str, Any]]:
    """Per component: the outcome of its acquisition (stored, current or failed)."""
    out: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    if details is not None and isinstance(details.get("outcomes"), list):
        results = {str(o.get("source")): o for o in details["outcomes"]}
    for c in comps:
        own = results.get(c.source_code) or results.get(c.provider)
        o = own if c.kind == "spots" else details
        if o is None:
            why = stage.detail.get("error") or stage.detail.get("reason") or "not attempted"
            entry = {"outcome": "failed", "message": str(why)[:DETAIL_TEXT_MAX], "run_key": None}
        elif "skipped" in o and "status" not in o:  # the job body's own "not configured"
            entry = {"outcome": "current", "message": str(o["skipped"]), "run_key": None}
        else:
            entry = {
                "outcome": _outcome(o.get("status"), o.get("message")),
                "message": str(o.get("message") or "")[:DETAIL_TEXT_MAX],
                "run_key": o.get("run_key"),
                "retry_after_s": o.get("retry_after_s"),
            }
        entry = {
            "source_code": c.source_code,
            "provider": c.provider,
            **entry,
            "run_key": entry.get("run_key") or c.available_run_key,
        }
        if entry["outcome"] == "stored":
            ctx.stored.add(c.kind)
        elif entry["outcome"] == "failed":
            ctx.failed.append(f"{c.source_code}: {entry['message'] or 'failed'}")
        out.append(entry)
    return out


def _skipped(name: StageName, reason: str) -> StageResult:
    return StageResult(name, "skipped", utcnow(), 0, 0.0, {"reason": reason})


def _not_acquired(ctx: _Sync, kind: ComponentKind) -> str:
    """Why an acquisition stage acquires nothing: --only, a component that is not
    configured (its plan detail, e.g. "GEFS_ENABLED=false"), or no new run."""
    if ctx.only is not None and kind not in ctx.only:
        return "--only excludes it"
    comps = [c for c in ctx.plan.components if c.kind == kind]
    if comps and all(c.state == "disabled" for c in comps):
        details = "; ".join(sorted({c.detail for c in comps if c.detail}))
        return f"disabled ({details})" if details else "disabled"
    return f"no new {kind} run"


def _execute(name: StageName, fn: Callable[[Session], Any]) -> tuple[StageResult, Any]:
    """Run one stage as its job (lock, job log, judge); never raises."""
    spec = job_spec(STAGE_JOB_NAMES[name])
    seen: dict[str, Any] = {}

    def body(db: Session) -> Any:
        seen["ran"] = True
        seen["out"] = fn(db)
        return seen["out"]

    started, t0, cpu0 = utcnow(), time.monotonic(), cpu_seconds()
    try:
        out = run_job(spec.name, body, lock_ttl=spec.lock_ttl, chain=False, judge=spec.judge)
    except Exception as exc:  # run_job logged it FAILED
        duration = int((time.monotonic() - t0) * 1000)
        cpu = round(cpu_seconds() - cpu0, 3)
        return StageResult(name, "failed", started, duration, cpu, {"error": _text(exc)}), None
    duration = int((time.monotonic() - t0) * 1000)
    cpu = float(out.get("cpu_s", round(cpu_seconds() - cpu0, 3)))
    if not seen.get("ran"):
        reason = str(out.get("reason") or "another runner holds this job's lock")
        if name in REQUIRED_STAGES:
            failed = {"error": f"did not run: {reason}"}
            return StageResult(name, "failed", started, duration, cpu, failed), None
        return StageResult(name, "skipped", started, duration, cpu, {"reason": reason}), None
    raw = seen.get("out")
    details = raw if isinstance(raw, dict) else {"result": raw}
    status = JobStatus.SUCCESS
    if spec.judge is not None:
        status, _ = spec.judge(details)
    stage_status: StageStatus = "failed" if status == JobStatus.FAILED else "success"
    return StageResult(name, stage_status, started, duration, cpu, _compact(details)), details


def _stage(name: StageName, ctx: _Sync) -> StageResult:
    """Decide and run one stage."""
    now = ctx.now
    forced = ctx.plan.forced
    if name in ACQUISITION_STAGES:
        kind = _STAGE_KIND[name]
        comps = ctx.wanted(kind)
        if not comps:
            return _skipped(name, _not_acquired(ctx, kind))
        result, details = _execute(name, _acquisition(kind, comps, now))
        result.detail["components"] = _components(ctx, comps, details, result)
        ctx.components.extend(result.detail["components"])
        return result
    if name == "blend":
        if not (forced or ctx.stored & {"spots", "ensemble"} or "member" in ctx.unpublished):
            return _skipped(name, "no new member data")
        return _execute(name, _job("blend", now))[0]
    if name == "predict":
        result = _execute(name, _job("predict", now))[0]
        ctx.predict_ok = result.status == "success"
        return result
    if name == "publish":
        if not (forced or ctx.stored or ctx.unpublished):
            return _skipped(name, "nothing new to publish")
        if not ctx.predict_ok and not forced:
            _hold_back(ctx)
            return _skipped(name, PUBLISH_HELD)
        result, _ = _execute(name, _publish_body(ctx))
        ctx.publish_failed = result.status != "success" or ctx.published_at is None
        if not ctx.publish_failed:
            _note_published(ctx.row_id, prewarm=False)
        return result
    if ctx.published_at is None:
        return _skipped(name, "nothing was published")
    if name == "prune" and not ctx.settings.sync_prune_each_pass:
        return _skipped(name, "SYNC_PRUNE_EACH_PASS=false (the daily cleanup prunes)")
    return _execute(name, _job(name, now))[0]


def _hold_back(ctx: _Sync) -> None:
    """The publication waits for a predict that completes: every component this sync
    stored or found stored becomes a failed attempt (apply_result: retried after
    SYNC_RETRY_MINUTES, at most SYNC_MAX_ATTEMPTS times; an outcome "current" would clear
    the attempts at every retry). Its runs stay newer than the newest publication, so the
    next check finds them new again."""
    held = 0
    for comp in ctx.components:
        if comp.get("outcome") in ("stored", "current"):
            comp["outcome"], comp["message"] = "failed", HELD_MESSAGE
            held += 1
    ctx.failed.append(
        f"publication held back: {PUBLISH_HELD}"
        + (f" ({held} component(s) retried after SYNC_RETRY_MINUTES)" if held else "")
    )


def _acquisition(
    kind: ComponentKind, comps: list[ComponentStatus], now: datetime | None
) -> Callable[[Session], Any]:
    if kind == "spots":
        tokens = {c.provider for c in comps}

        def spots(db: Session) -> dict[str, Any]:
            from app.services.forecasts.ingestion import acquire_all

            return {"outcomes": [o.as_dict() for o in acquire_all(db, now=now, only=tokens)]}

        return spots
    if kind == "ensemble":

        def ensemble(db: Session) -> Any:
            from app.services.forecasts.ensemble import acquire_ensemble

            return acquire_ensemble(db, now)

        return ensemble

    def map_layers(db: Session) -> dict[str, Any]:
        from app.services.forecasts.ingestion import acquire_map_fields
        from app.services.map_fields import map_fields_mode

        mode = map_fields_mode(get_settings())
        if mode != "standalone":
            return {"skipped": f"map fields mode is {mode!r}, not 'standalone'"}
        return acquire_map_fields(db, now=now)

    return map_layers


def _job(name: str, now: datetime | None) -> Callable[[Session], Any]:
    """The job body of a non-acquisition stage, with the sync's ``now``."""

    def run(db: Session) -> Any:
        if name == "blend":
            from app.services.forecasts.blend import build_blend

            return build_blend(db, now).as_dict()
        if name == "predict":
            from app.services.surf_quality.predict import compute_pending_predictions

            return compute_pending_predictions(db, now)
        if name == "detect":
            from app.services.swell_detection.service import detect_events

            return detect_events(db, now).as_dict()
        if name == "match":
            from app.services.matching.service import match_all

            return match_all(db, now).as_dict()
        if name == "flights":
            from app.services.flights.service import discover_flights

            return discover_flights(db, now).as_dict()
        if name == "alerts":
            from app.services.notifications.alerts import generate_alerts

            return generate_alerts(db, now).as_dict()
        if name == "deliver":
            from app.services.notifications.delivery import deliver_pending

            return deliver_pending(db, now).as_dict()
        if name == "prune":
            from app.services.maintenance import prune_pass

            return prune_pass(db, now)
        raise KeyError(name)

    return run


def _publish_body(ctx: _Sync) -> Callable[[Session], Any]:
    def run(db: Session) -> dict[str, Any]:
        from app.services.sync.publish import publication_summary, publish

        row = db.get(SyncRun, ctx.row_id)
        if row is None:
            raise RuntimeError(f"sync_runs row {ctx.row_id} no longer exists")
        published = publish(db, sync_run=row, trigger=ctx.trigger)
        ctx.published_at = published.published_at
        return publication_summary(published)

    return run


def _note_published(sync_run_id: int, *, prewarm: bool) -> None:
    """The public snapshot cache's hook (app.services.public_cache.note_published): every
    public snapshot is computed afresh from the next request on, and no request reads the
    database to learn that. With ``prewarm`` (SYNC_PREWARM_PUBLIC_CACHE) the spot list and
    the conditions are computed now. Never raises: a sync's result does not depend on it."""
    try:
        from app.services import public_cache

        if public_cache.note_published(sync_run_id) and prewarm:
            with SessionLocal() as db:
                public_cache.prewarm(db)
    except Exception:  # the snapshots then follow the database marker, or the next hour
        logger.warning("Public snapshot hook failed after sync %s", sync_run_id, exc_info=True)


def _status(
    ctx: _Sync, stages: list[StageResult], stopped: bool, lost: bool
) -> tuple[SyncStatus, str | None]:
    errors = list(ctx.failed)
    errors += [
        f"stage {s.name}: {s.detail.get('error') or 'failed'}"
        for s in stages
        if s.status == "failed" and s.name not in ACQUISITION_STAGES
    ]
    if stopped:
        ran = [s.name for s in stages if s.detail.get("reason") != "stopped"]
        errors.append(
            f"stopped after stage {ran[-1] if ran else 'none'}; the next sync completes it"
        )
    status: SyncStatus
    if lost:
        status = "abandoned"
        errors.append("the lease was lost (expired and taken over)")
    elif ctx.publish_failed:
        status = "failed"
    elif ctx.published_at is not None:
        status = "partial" if errors else "success"
    elif stopped:
        status = "partial" if any(s.status != "skipped" for s in stages) else "skipped"
    else:
        status = "failed" if errors else "skipped"
    error = redact("; ".join(errors))[:2000] if errors else None
    return status, error


_LOG_STATUS: dict[str, JobStatus] = {
    "success": JobStatus.SUCCESS,
    "partial": JobStatus.SUCCESS,
    "skipped": JobStatus.SKIPPED,
    "failed": JobStatus.FAILED,
    "abandoned": JobStatus.FAILED,
}


def _open_log(row_id: int) -> int:
    with SessionLocal() as db:
        log = BackgroundJobLog(
            job_name=SYNC_RUN_JOB,
            status=JobStatus.RUNNING,
            started_at=utcnow(),
            details={"sync_run_id": row_id},
        )
        db.add(log)
        db.commit()
        return int(log.id)


def _close_log(
    log_id: int,
    result: SyncResult,
    *,
    duration_ms: int,
    cpu_total: float,
    error: str | None,
) -> None:
    stage_cpu = sum(s.cpu_s for s in result.stages)
    with SessionLocal() as db:
        log = db.get(BackgroundJobLog, log_id)
        if log is None:
            return
        log.status = _LOG_STATUS[result.status]
        log.finished_at = utcnow()
        log.duration_ms = duration_ms
        log.error_message = error
        # cpu_s is the sync's own share (planning the stages, the lease, the logs): every
        # stage logs its own cpu_s under its job name, so summing cpu_s over job logs
        # counts each CPU second once. cpu_s_total includes the stages.
        log.details = jsonable(
            {
                "sync_run_id": result.sync_run_id,
                "sync_status": result.status,
                "published_at": result.published_at,
                "stages": [[s.name, s.status, s.cpu_s] for s in result.stages],
                "cpu_s": round(max(0.0, cpu_total - stage_cpu), 3),
                "cpu_s_total": round(cpu_total, 3),
            }
        )
        db.commit()


def run_sync(
    *,
    trigger: SyncTrigger,
    force: bool = False,
    only: Collection[ComponentKind] | None = None,
    now: datetime | None = None,
    stop: threading.Event | None = None,
    plan: SyncPlan | None = None,
) -> SyncResult:
    """Run one sync now (see the module docs); returns its result.

    Without ``plan`` the plan is made with the database (and the known runs in Redis);
    a plan that is not due (and not ``force``) returns status skipped without a row.
    ``only`` limits the acquisitions to these component kinds. Raises SyncBusy when
    another sync holds a valid lease.
    """
    settings = get_settings()
    if plan is None:
        with SessionLocal() as db:
            running = _valid_lease(db)
            if running is not None:
                raise SyncBusy(running.holder, running.lease_expires_at)
            plan = plan_sync(db, settings=settings, now=now, known=state.read_known(), force=force)
    elif force and not plan.forced:
        plan = replace(plan, due=True, forced=True, reasons=(*plan.reasons, "forced"))
    if not plan.due:
        return SyncResult(None, "skipped", plan, [], None)

    lease = _Lease.acquire(trigger=trigger, plan=plan, settings=settings)
    ctx = _Sync(
        plan=plan,
        settings=settings,
        trigger=trigger,
        now=now,
        only=frozenset(only) if only is not None else None,
        row_id=lease.row_id,
    )
    t0, cpu0 = time.monotonic(), cpu_seconds()
    try:
        log_id = _open_log(lease.row_id)
    except BaseException as exc:  # e.g. the database went away: do not hold the lease
        with contextlib.suppress(Exception):
            lease.release("failed", [], _text(exc))
        raise
    logger.info("Sync %s (%s) started: %s", lease.row_id, trigger, "; ".join(plan.reasons))
    stages: list[StageResult] = []
    stopped = lost = False
    try:
        with _LeaseKeeper(lease):
            for name in STAGES:
                if stop is not None and stop.is_set():
                    stopped = True
                    break
                if name == "blend":
                    with SessionLocal() as db:
                        ctx.unpublished = _unpublished(db)
                stages.append(_stage(name, ctx))
                if not lease.renew(stages):
                    lost = True
                    break
    except BaseException as exc:  # e.g. KeyboardInterrupt: never leave the lease running
        lease.release("failed", stages, _text(exc))
        _close_log(
            log_id,
            SyncResult(lease.row_id, "failed", plan, stages, ctx.published_at),
            duration_ms=int((time.monotonic() - t0) * 1000),
            cpu_total=cpu_seconds() - cpu0,
            error=_text(exc),
        )
        raise
    done = {s.name for s in stages}
    reason = "lease lost" if lost else "stopped"
    stages += [_skipped(n, reason) for n in STAGES if n not in done]
    status, error = _status(ctx, stages, stopped, lost)
    if not lost and not lease.release(status, stages, error):
        status = "abandoned"
    result = SyncResult(lease.row_id, status, plan, stages, ctx.published_at)
    _close_log(
        log_id,
        result,
        duration_ms=int((time.monotonic() - t0) * 1000),
        cpu_total=cpu_seconds() - cpu0,
        error=error,
    )
    if ctx.published_at is not None:
        # Again at the end: detection and the later stages changed the swell events (and the
        # job list) the public snapshots show after the publication.
        _note_published(lease.row_id, prewarm=settings.sync_prewarm_public_cache)
    logger.info(
        "Sync %s finished %s in %.1f s (%.1f CPU-s)%s",
        lease.row_id,
        status,
        time.monotonic() - t0,
        cpu_seconds() - cpu0,
        f": {error}" if error else "",
    )
    return result


def remember(known: KnownRuns, result: SyncResult, now: datetime | None = None) -> KnownRuns:
    """After a sync: reload the stored keys, record the outcomes (attempts at ``now``, the
    wall clock by default), save to Redis."""
    with SessionLocal() as db:
        refresh_known(db, known)
    apply_result(known, result, now)
    state.write_known(known)
    return known


def tick(*, trigger: SyncTrigger) -> dict[str, Any]:
    """Plan with the database (and the known runs in Redis) and sync when one is due."""
    known = state.read_known() or empty_known()
    with SessionLocal() as db:
        plan = plan_sync(db, known=known)
    state.write_known(known)
    out: dict[str, Any] = {
        "due": plan.due,
        "reasons": list(plan.reasons),
        "waiting_until": plan.waiting_until.isoformat() if plan.waiting_until else None,
        "status": "not_due",
        "sync_run_id": None,
        "published_at": None,
    }
    if not plan.due:
        return out
    try:
        result = run_sync(trigger=trigger, plan=plan)
    except SyncBusy as exc:
        return {**out, "status": "busy", "detail": str(exc)}
    remember(known, result)
    return {
        **out,
        "status": result.status,
        "sync_run_id": result.sync_run_id,
        "published_at": result.published_at.isoformat() if result.published_at else None,
        "stages": [[s.name, s.status, s.cpu_s] for s in result.stages],
    }
