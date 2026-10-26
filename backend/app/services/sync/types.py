"""Types of the synced data pipeline.

A *plan* (:class:`SyncPlan`) says, per component (spot forecasts of one source, the GEFS
ensemble, the map layers), whether a newer model run is available than the one stored, and
whether a sync is due now. A *sync* runs the :data:`STAGES` in order and records a
:class:`StageResult` per stage; :class:`SyncResult` is the outcome. :class:`KnownRuns` is
what the daemon remembers between checks (in memory and in Redis, ``state.KNOWN_KEY``) so an
idle check needs no database access.

Every ``as_dict()`` / ``to_json()`` is JSON-safe: datetimes become ISO 8601 strings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
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


def known_key(source_code: str, run_key: str) -> str:
    """The key of one model run in :class:`KnownRuns` maps: ``'<source_code>|<run_key>'``."""
    return f"{source_code}|{run_key}"


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
    serves it. ``stored_run_key`` is the newest run stored, ``available_run_key`` /
    ``available_cycle`` the newest run the provider serves. ``first_seen_at`` is when the
    available run was first seen (coalescing), ``attempts`` the failed attempts for it and
    ``retry_after_s`` the wait before the next one. ``detail`` is plain and redacted.
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_code": self.source_code,
            "provider": self.provider,
            "state": self.state,
            "stored_run_key": self.stored_run_key,
            "available_run_key": self.available_run_key,
            "available_cycle": _iso(self.available_cycle),
            "first_seen_at": _iso(self.first_seen_at),
            "detail": self.detail,
            "retry_after_s": self.retry_after_s,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class SyncPlan:
    """What a check found: whether a sync is due now, and why.

    ``due`` is the trigger decision (SYNC_TRIGGER_SOURCES, coalescing, retries, ``forced``).
    ``anchor_cycle`` is the GFS cycle a spot run and the map layers are coalesced on, and
    ``waiting_until`` the end of that wait when the plan is held back.
    """

    checked_at: datetime
    due: bool
    components: tuple[ComponentStatus, ...]
    anchor_cycle: datetime | None
    reasons: tuple[str, ...]
    waiting_until: datetime | None = None
    forced: bool = False

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
    * ``loaded_at``: when ``stored`` and ``map_cycle`` were read from the database.
    """

    stored: dict[str, str]
    map_cycle: datetime | None
    first_seen: dict[str, datetime]
    attempts: dict[str, tuple[int, datetime]]
    loaded_at: datetime

    def to_json(self) -> str:
        return json.dumps(
            {
                "stored": dict(self.stored),
                "map_cycle": _iso(self.map_cycle),
                "first_seen": {k: v.isoformat() for k, v in self.first_seen.items()},
                "attempts": {k: [n, at.isoformat()] for k, (n, at) in self.attempts.items()},
                "loaded_at": self.loaded_at.isoformat(),
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
        )
