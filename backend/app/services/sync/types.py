"""Types of the synced data pipeline.

A *plan* (:class:`SyncPlan`) says, per component, whether a newer model run is available
than the one stored, and whether a sync is due now. The components are the spot forecasts
of one source, split into their ``wave`` and ``atmosphere`` parts when the provider names a
run key per part (``RunInfo.component_keys``, design D6), the GEFS ensemble and the map
layers. A *sync* runs the :data:`STAGES` in order and records a :class:`StageResult` per
stage; :class:`SyncResult` is the outcome. :class:`KnownRuns` is what the runners remember
between checks (in memory and in Redis, ``state.KNOWN_KEY``) so an idle check needs no
database access; :class:`SpotRetry` is a planned retry of the spots whose data failed in
the published version (design D9).

Every ``as_dict()`` / ``to_json()`` is JSON-safe: datetimes become ISO 8601 strings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal

ComponentKind = Literal["spots", "ensemble", "map"]
# current: the stored run is the newest available; new: a newer run is available and due;
# waiting: newer run seen, held back (coalescing, or a retry not yet due); in_progress: a
# sync is acquiring it; failed: the last attempt failed (see attempts, retry_after_s);
# unknown: the provider's metadata could not be read; disabled: not configured.
ComponentState = Literal[
    "current", "new", "waiting", "in_progress", "failed", "unknown", "disabled"
]
SyncTrigger = Literal["daemon", "celery", "cli", "pipeline", "github", "http"]
SyncStatus = Literal["running", "success", "partial", "failed", "skipped", "abandoned"]
StageName = Literal[
    "spots",
    "ensemble",
    "map",
    "blend",
    "predict",
    "publish",
    "detect",
    "match",
    "flights",
    "alerts",
    "deliver",
    "prune",
]
StageStatus = Literal["success", "skipped", "failed"]

# The order a sync runs its stages in.
STAGES: tuple[StageName, ...] = (
    "spots",
    "ensemble",
    "map",
    "blend",
    "predict",
    "publish",
    "detect",
    "match",
    "flights",
    "alerts",
    "deliver",
    "prune",
)

# background_job_logs.job_name of each stage (the existing job names, so the data-health
# report and the job history read the same rows in every runner).
STAGE_JOB_NAMES: Mapping[StageName, str] = MappingProxyType(
    {
        "spots": "forecasts.acquire",
        "ensemble": "forecasts.acquire_ensemble",
        "map": "forecasts.map_fields",
        "blend": "forecasts.blend",
        "predict": "quality.predict",
        "publish": "sync.publish",
        "detect": "detection.detect",
        "match": "matching.match",
        "flights": "flights.discover",
        "alerts": "alerts.generate",
        "deliver": "notifications.deliver",
        "prune": "maintenance.prune_pass",
    }
)

# Job log names of the sync itself: one "sync.run" row per sync, and "sync.tick" for the
# Celery beat entry (JOB_RUNNER=celery only). The daemon does not log idle checks.
SYNC_RUN_JOB = "sync.run"
SYNC_TICK_JOB = "sync.tick"

# Jobs a sync runs (and Celery's sync check): never on a timer in the sync daemon
# (app.services.sync.daemon.periodic_entries), so data health does not expect them there
# either. The D14 schedule (app.jobs.schedule) no longer lists the acquisition, prediction
# and detection jobs; excluding them keeps the daemon right with an older schedule too.
SYNC_OWNED_TASKS = frozenset(
    {
        SYNC_TICK_JOB,
        "forecasts.acquire",
        "forecasts.acquire_ensemble",
        "forecasts.map_fields",
        "quality.predict",
        "detection.detect",
    }
)


class SyncBusy(RuntimeError):
    """Another sync holds a valid lease (a sync_runs row with status running)."""

    def __init__(self, holder: str | None = None, lease_expires_at: datetime | None = None) -> None:
        self.holder = holder
        self.lease_expires_at = lease_expires_at
        until = f" until {lease_expires_at.isoformat()}" if lease_expires_at else ""
        super().__init__(f"another sync holds the lease ({holder or 'unknown holder'}){until}")


def stored_key(source_code: str, component: str | None = None) -> str:
    """The key of a component in :attr:`KnownRuns.stored`: the source code for a whole
    source (and the map, the ensemble), ``'<source_code>:<component>'`` for the ``wave`` or
    ``atmosphere`` part of a spot source."""
    return source_code if component is None else f"{source_code}:{component}"


def known_key(source_code: str, run_key: str) -> str:
    """The key of one model run in :class:`KnownRuns` maps: ``'<source_code>|<run_key>'``
    (``source_code`` is a :func:`stored_key` for a component)."""
    return f"{source_code}|{run_key}"


# The waits between the retries of the spots that failed in a refresh (design D9): 5, 15
# and 45 minutes, then 45 minutes until SYNC_MAX_ATTEMPTS retries were made.
SPOT_RETRY_BACKOFF_MINUTES: tuple[int, ...] = (5, 15, 45)


def spot_retry_wait(retries_done: int) -> timedelta:
    """The wait before the next retry of failed spots after ``retries_done`` retries."""
    steps = SPOT_RETRY_BACKOFF_MINUTES
    return timedelta(minutes=steps[min(max(retries_done, 0), len(steps) - 1)])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def jsonable(value: Any) -> Any:
    """``value`` with datetimes as ISO strings, tuples as lists and dict keys as strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class ComponentStatus:
    """One component of a plan.

    ``source_code`` is the forecast source (e.g. ``open_meteo_gfs``, ``noaa_gfs_fields``,
    ``noaa_gefs_wave``) and ``provider`` the FORECAST_PROVIDERS token or provider name that
    serves it. ``component`` is ``wave`` or ``atmosphere`` for one part of a spot source
    whose provider names a run key per part, None for a whole source. ``stored_run_key`` is
    the newest run stored (and published), ``available_run_key`` / ``available_cycle`` the
    newest run the provider serves (the component's own key). ``first_seen_at`` is when the
    available run was first seen (coalescing within the component), ``attempts`` the failed
    attempts for it and ``retry_after_s`` the wait before the next one.
    ``retry_spot_ids``: spots whose data failed in the published version and are retried
    alone (state ``new`` when the retry is due, ``waiting`` during its backoff, ``failed``
    after SYNC_MAX_ATTEMPTS retries). ``detail`` is plain and redacted.
    """

    kind: ComponentKind
    source_code: str
    provider: str
    state: ComponentState
    stored_run_key: str | None
    available_run_key: str | None
    available_cycle: datetime | None
    first_seen_at: datetime | None = None
    detail: str | None = None
    retry_after_s: float | None = None
    attempts: int = 0
    component: str | None = None
    retry_spot_ids: tuple[int, ...] = ()

    @property
    def key(self) -> str:
        """:func:`stored_key` of this component."""
        return stored_key(self.source_code, self.component)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_code": self.source_code,
            "provider": self.provider,
            "component": self.component,
            "state": self.state,
            "stored_run_key": self.stored_run_key,
            "available_run_key": self.available_run_key,
            "available_cycle": _iso(self.available_cycle),
            "first_seen_at": _iso(self.first_seen_at),
            "detail": self.detail,
            "retry_after_s": self.retry_after_s,
            "attempts": self.attempts,
            "retry_spot_ids": list(self.retry_spot_ids),
        }


@dataclass(frozen=True)
class SyncPlan:
    """What a check found: whether a sync is due now, and why.

    ``due`` is the trigger decision (SYNC_TRIGGER_SOURCES, coalescing, retries, ``forced``).
    ``anchor_cycle`` is the GFS cycle of the map layers (or the GFS spot run of a map cycle)
    a due plan acquires, and ``waiting_until`` the end of the nearest wait when the plan is
    held back. ``triggers``: the component kinds whose new run made (or would make) it due.
    """

    checked_at: datetime
    due: bool
    components: tuple[ComponentStatus, ...]
    anchor_cycle: datetime | None
    reasons: tuple[str, ...]
    waiting_until: datetime | None = None
    forced: bool = False
    triggers: frozenset[str] = frozenset()

    def due_kinds(self) -> frozenset[ComponentKind]:
        """The kinds a sync run on this plan acquires: every component in state ``new``,
        and with ``forced`` every component that is not ``disabled``.

        Independent of ``due``: a component that is not a trigger (by default the ensemble)
        is still acquired by a sync that something else triggered.
        """
        return frozenset(
            c.kind
            for c in self.components
            if c.state == "new" or (self.forced and c.state != "disabled")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": _iso(self.checked_at),
            "due": self.due,
            "forced": self.forced,
            "anchor_cycle": _iso(self.anchor_cycle),
            "waiting_until": _iso(self.waiting_until),
            "reasons": list(self.reasons),
            "due_kinds": sorted(self.due_kinds()),
            "components": [c.as_dict() for c in self.components],
        }


@dataclass
class StageResult:
    """One stage of a sync; ``as_dict()`` is the entry stored in ``sync_runs.stages``."""

    name: StageName
    status: StageStatus
    started_at: datetime
    duration_ms: int
    cpu_s: float
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "duration_ms": self.duration_ms,
            "cpu_s": self.cpu_s,
            "detail": jsonable(self.detail),
        }


@dataclass
class SyncResult:
    """The outcome of one sync (``sync_run_id`` is None when no row was written)."""

    sync_run_id: int | None
    status: SyncStatus
    plan: SyncPlan
    stages: list[StageResult]
    published_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sync_run_id": self.sync_run_id,
            "status": self.status,
            "published_at": _iso(self.published_at),
            "plan": self.plan.as_dict(),
            "stages": [s.as_dict() for s in self.stages],
        }


@dataclass
class KnownRuns:
    """Run keys known without asking the database.

    * ``stored``: source code -> newest run key stored for it;
    * ``map_cycle``: the newest servable map cycle;
    * ``first_seen``: :func:`known_key` -> when that available run was first seen;
    * ``attempts``: :func:`known_key` -> (failed attempts, time of the last one);
    * ``loaded_at``: when ``stored``, ``map_cycle`` and ``retries`` were read from the
      database;
    * ``retries``: source code -> the planned retry of its failed spots (from the database:
      the published version's stale spots and the retry runs made for it).

    ``stored`` holds one key per whole source and one per component (:func:`stored_key`).
    """

    stored: dict[str, str]
    map_cycle: datetime | None
    first_seen: dict[str, datetime]
    attempts: dict[str, tuple[int, datetime]]
    loaded_at: datetime
    retries: dict[str, SpotRetry] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "stored": dict(self.stored),
                "map_cycle": _iso(self.map_cycle),
                "first_seen": {k: v.isoformat() for k, v in self.first_seen.items()},
                "attempts": {k: [n, at.isoformat()] for k, (n, at) in self.attempts.items()},
                "loaded_at": self.loaded_at.isoformat(),
                "retries": {k: r.as_dict() for k, r in self.retries.items()},
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str | bytes) -> KnownRuns:
        """Parse :meth:`to_json` output; raises ValueError (or KeyError) when malformed."""
        data = json.loads(text)
        loaded_at = _dt(data["loaded_at"])
        if loaded_at is None:
            raise ValueError("KnownRuns JSON without loaded_at")
        return cls(
            stored={str(k): str(v) for k, v in data.get("stored", {}).items()},
            map_cycle=_dt(data.get("map_cycle")),
            first_seen={
                str(k): datetime.fromisoformat(v) for k, v in data.get("first_seen", {}).items()
            },
            attempts={
                str(k): (int(n), datetime.fromisoformat(at))
                for k, (n, at) in data.get("attempts", {}).items()
            },
            loaded_at=loaded_at,
            retries={
                str(k): SpotRetry.from_dict(v) for k, v in (data.get("retries") or {}).items()
            },
        )


@dataclass(frozen=True)
class SpotRetry:
    """A planned retry of the spots whose data failed in the published version (D9).

    ``run_key`` is the run key of the refresh that failed them (the published version's
    run, without a ``#retry`` suffix); ``spot_ids`` the spots still stale with an error;
    ``retries`` the retry runs made for that run key so far; ``last_attempt_at`` when the
    refresh or the last retry ran. The next retry is due :func:`spot_retry_wait` later, at
    most SYNC_MAX_ATTEMPTS retries per run key.
    """

    source_code: str
    run_key: str
    version_id: int | None
    spot_ids: tuple[int, ...]
    retries: int
    last_attempt_at: datetime
    components: tuple[str, ...] = ()

    def due_at(self) -> datetime:
        return self.last_attempt_at + spot_retry_wait(self.retries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_code": self.source_code,
            "run_key": self.run_key,
            "version_id": self.version_id,
            "spot_ids": list(self.spot_ids),
            "retries": self.retries,
            "last_attempt_at": self.last_attempt_at.isoformat(),
            "components": list(self.components),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpotRetry:
        last = _dt(data.get("last_attempt_at"))
        if last is None:
            raise ValueError("SpotRetry without last_attempt_at")
        version = data.get("version_id")
        return cls(
            source_code=str(data["source_code"]),
            run_key=str(data["run_key"]),
            version_id=None if version is None else int(version),
            spot_ids=tuple(int(s) for s in data.get("spot_ids") or ()),
            retries=int(data.get("retries") or 0),
            last_attempt_at=last,
            components=tuple(str(c) for c in data.get("components") or ()),
        )
