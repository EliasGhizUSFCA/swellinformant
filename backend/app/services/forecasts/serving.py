"""What the public API serves of the published forecast (design D7, D10, D11; C3, C4).

* **The version marker** (:func:`version_marker`): the C4 payload of the published version
  (``versions.version_payload``), which GET /api/forecasts/version answers and the public
  snapshot cache keys every payload on (app.services.public_cache). It is read from this
  process's memo (:data:`VERSION_MEMO_SECONDS`), else from Redis (the refresh writes it
  after each publication: ``versions.write_version_marker`` or :func:`refresh_version_marker`),
  else from the database in ONE statement (:func:`load_version_payload`), which is then
  stored for the next readers with SET NX and a :data:`SEEDED_MARKER_TTL_SECONDS` expiry
  (:func:`seed_version_marker`). NX: a reader that read the database just before a
  publication can never overwrite the newer marker the publisher wrote; the expiry bounds how
  long a marker seeded in such a race could outlive its version (the publisher's own marker
  has none). So while Redis holds the marker no request reads the database, and Neon's
  compute can stay asleep.
* **Freshness** (:func:`freshness_by_spot`): per spot, the version's freshness (design D10,
  ``versions.spot_freshness``) with the version id; before the first publication, the
  served run's last update and its own staleness rule (``queries.is_stale``).
* **Status** (:func:`status_payload`): GET /api/forecasts/status, the version, the model run
  of each component and the stale active spots.

The map run a payload names is the newest publication's map run (``sync_runs.map_run_id``
of the newest row with ``published_at`` and a map run) with its cycle (the run's
``issued_at``). Redis errors never reach a visitor: the helpers answer as without Redis.
Lean imports only (the API imports this module).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import redis
from sqlalchemy import literal, select
from sqlalchemy.orm import Session, load_only

from app.models import ForecastRun, ForecastVersion, SurfSpot
from app.models.sync import SyncRun
from app.schemas.forecasts import (
    ForecastComponentOut,
    ForecastDomainOut,
    ForecastStatusOut,
    SpotFreshnessOut,
    StaleSpotOut,
)
from app.services.forecasts import versions
from app.services.forecasts.queries import is_stale, served_runs_by_spot, served_version

logger = logging.getLogger(__name__)

# How long this process answers the version endpoint from memory before asking Redis again
# (a CDN region asks the origin once per FORECAST_VERSION_CDN_SECONDS; a warm instance that
# serves several regions or the snapshot routes asks Redis at most this often).
VERSION_MEMO_SECONDS = 5.0
# A marker a reader seeded from the database expires after this (the publisher's has none).
SEEDED_MARKER_TTL_SECONDS = 3600
# First-served marks this process already recorded (one Redis SET NX per version).
_NOTED_MAX = 32

# The C4 fields of a version (the rest of the row, e.g. spot_runs, is not read for it).
_PAYLOAD_COLUMNS = (
    ForecastVersion.status,
    ForecastVersion.published_at,
    ForecastVersion.provider,
    ForecastVersion.source_code,
    ForecastVersion.wind_model,
    ForecastVersion.wind_model_run,
    ForecastVersion.wind_run_estimated,
    ForecastVersion.wave_model,
    ForecastVersion.wave_model_run,
    ForecastVersion.wave_run_estimated,
    ForecastVersion.spots_total,
    ForecastVersion.spots_updated,
    ForecastVersion.spots_stale,
)

_memo_lock = threading.Lock()
_memo: tuple[float, dict[str, Any]] | None = None
_noted: OrderedDict[int, None] = OrderedDict()


def clear_memo() -> None:
    """Forget this process's marker and first-served memos (tests; a new instance)."""
    global _memo
    with _memo_lock:
        _memo = None
        _noted.clear()


# --------------------------------------------------------------------------- the marker
def _map_run_columns() -> tuple[Any, Any]:
    """Scalar subqueries: the newest publication's map run id and that run's cycle."""
    newest = (
        select(SyncRun.map_run_id)
        .where(SyncRun.published_at.is_not(None), SyncRun.map_run_id.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    cycle = select(ForecastRun.issued_at).where(ForecastRun.id == newest).scalar_subquery()
    return newest, cycle


def map_run_ref(db: Session) -> versions.MapRunRef | None:
    """The published map run (one statement), as the version payload names it."""
    newest, cycle = _map_run_columns()
    run_id, issued = db.execute(select(newest, cycle)).one()
    return versions.MapRunRef(int(run_id), issued) if run_id is not None else None


def load_version_payload(db: Session) -> dict[str, Any]:
    """The C4 payload from the database in one statement: the published version (None
    before the first publication) and the published map run."""
    newest, cycle = _map_run_columns()
    anchor = select(literal(1).label("anchor")).subquery()
    version, map_run_id, map_cycle = db.execute(
        select(ForecastVersion, newest.label("map_run_id"), cycle.label("map_cycle"))
        .select_from(anchor)
        .outerjoin(ForecastVersion, ForecastVersion.status == versions.PUBLISHED)
        .options(load_only(*_PAYLOAD_COLUMNS))
    ).one()
    ref = versions.MapRunRef(int(map_run_id), map_cycle) if map_run_id is not None else None
    return versions.version_payload(version, ref)


def normalised(raw: Any) -> dict[str, Any] | None:
    """A stored marker as a complete C4 payload, or None when it is not one (no
    ``version_id``, or an id or map run id that is not an integer)."""
    if not isinstance(raw, Mapping) or "version_id" not in raw:
        return None
    for name in ("version_id", "map_run_id"):
        value = raw.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return None
    base = versions.version_payload(None)
    return {**base, **{k: raw[k] for k in base if k in raw}}


def parse_marker(raw: bytes | str | None) -> dict[str, Any] | None:
    """:func:`normalised` of a marker as Redis stores it (JSON text)."""
    if not raw:
        return None
    try:
        return normalised(json.loads(raw))
    except (TypeError, ValueError):
        return None


def encode_marker(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), separators=(",", ":"))


def seed_version_marker(payload: Mapping[str, Any], client: redis.Redis | None = None) -> bool:
    """Store a marker read from the database unless one is stored (SET NX, expiring after
    :data:`SEEDED_MARKER_TTL_SECONDS`). True when this call stored it. ``client``: the
    Redis client to use (default: the forecast versions' one, skipped while Redis rests
    after an error). Redis errors are logged by type only and answered False."""
    target = client if client is not None else versions._client()
    if target is None:
        return False
    try:
        return bool(
            target.set(
                versions.marker_key(),
                encode_marker(payload),
                nx=True,
                ex=SEEDED_MARKER_TTL_SECONDS,
            )
        )
    except redis.RedisError as exc:
        logger.warning("Forecast version marker not seeded: %s", type(exc).__name__)
        return False


def version_marker(db: Session) -> dict[str, Any]:
    """The published version's C4 payload: this process's memo, else Redis, else the
    database (one statement, then seeded into Redis). See the module docstring."""
    global _memo
    now = time.monotonic()
    with _memo_lock:
        memo = _memo
    if memo is not None and memo[0] > now:
        return memo[1]
    payload = normalised(versions.read_version_marker())
    if payload is None:
        payload = load_version_payload(db)
        seed_version_marker(payload)
    with _memo_lock:
        _memo = (now + VERSION_MEMO_SECONDS, payload)
    return payload


def refresh_version_marker(db: Session) -> dict[str, Any]:
    """For the publisher, right after a publication (spot version or map run) committed:
    read the payload from the database and store it as the marker (replacing any), so the
    version endpoint and the public snapshots switch at once. Returns the payload (stored
    or not: Redis errors are swallowed)."""
    payload = load_version_payload(db)
    versions.write_version_marker(payload)
    clear_memo()
    return payload


def note_served(version_id: int | None) -> None:
    """Record when ``version_id`` was first served (``versions.note_first_served``, a Redis
    SET NX), once per version in this process."""
    if version_id is None:
        return
    with _memo_lock:
        if version_id in _noted:
            return
        _noted[version_id] = None
        while len(_noted) > _NOTED_MAX:
            _noted.popitem(last=False)
    versions.note_first_served(version_id)


# --------------------------------------------------------------------------- freshness
def _run_updated_at(run: ForecastRun) -> datetime:
    return run.completed_at or run.started_at or run.issued_at


def freshness_by_spot(
    db: Session, runs: Mapping[int, ForecastRun], now: datetime
) -> dict[int, SpotFreshnessOut]:
    """{spot_id: freshness} of the served forecast at ``now``: from the served version
    (``versions.spot_freshness``: every spot the version maps, also those without data),
    else, before the first publication, from each served run in ``runs`` (its last update
    and its own staleness rule)."""
    version = served_version(db)
    if version is not None:
        return {
            spot_id: SpotFreshnessOut(
                version_id=version.id,
                last_updated_at=fresh.last_updated_at,
                stale=fresh.stale,
                reason=fresh.reason,  # type: ignore[arg-type]
            )
            for spot_id, fresh in versions.spot_freshness(version, now).items()
        }
    out: dict[int, SpotFreshnessOut] = {}
    for spot_id, run in runs.items():
        stale = is_stale(run, now)
        out[spot_id] = SpotFreshnessOut(
            version_id=None,
            last_updated_at=_run_updated_at(run),
            stale=stale,
            reason="age" if stale else None,
        )
    return out


# --------------------------------------------------------------------------- status
def _component_out(component: versions.ComponentRun) -> ForecastComponentOut:
    return ForecastComponentOut(
        component=component.component,  # type: ignore[arg-type]
        model=component.model,
        run_key=component.run_key,
        model_run=component.init_time,
        estimated=component.estimated,
        available_at=component.available_at,
        retrieved_at=component.retrieved_at,
        changed=component.changed,
        domains=[
            ForecastDomainOut(model=d.model, model_run=d.init_time, available_at=d.available_at)
            for d in component.domains
        ],
    )


def status_payload(db: Session, now: datetime) -> ForecastStatusOut:
    """GET /api/forecasts/status at ``now``: the served version (C4 payload with the
    published map run), its components (none before the first publication) and the active
    spots whose served forecast is stale, by name."""
    version = served_version(db)
    runs = served_runs_by_spot(db, now)
    freshness = freshness_by_spot(db, runs, now)
    spots = db.execute(
        select(SurfSpot.id, SurfSpot.slug, SurfSpot.name)
        .where(SurfSpot.is_active)
        .order_by(SurfSpot.name)
    ).all()
    stale = [
        StaleSpotOut(
            spot_id=spot.id,
            slug=spot.slug,
            name=spot.name,
            last_updated_at=freshness[spot.id].last_updated_at,
            reason=freshness[spot.id].reason,
        )
        for spot in spots
        if spot.id in freshness and freshness[spot.id].stale
    ]
    components = (
        [_component_out(c) for _, c in sorted(versions.component_runs_of(version).items())]
        if version is not None
        else []
    )
    return ForecastStatusOut.model_validate(
        {
            "version": versions.version_payload(version, map_run_ref(db)),
            "components": components,
            "spots_served": sum(1 for spot in spots if spot.id in runs),
            "stale_spots": stale,
            "generated_at": now,
        }
    )
