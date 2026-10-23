"""Public snapshot cache: the heavy anonymous GETs, computed at most once per key.

What /api/spots, /api/map/conditions, a spot page (detail, forecast, timeline, events) and
the next swells show changes only when a sync publishes (and detects swells right after)
and as the clock passes into a new hour. Each such response is therefore computed for the
start of the current UTC hour (:func:`public_now`) and kept in Redis under

    public:v1:<epoch>:<marker>:<YYYYmmddTHH>:<route>[:<params>]

* ``marker`` names the published data. :func:`note_published` (the sync's hook: after its
  publish stage and again at its end) stores a new random marker, so the next request of
  every route computes afresh, and no request reads the database to learn the marker
  (Neon's compute can stay asleep). Without a stored marker (no hook ran since Redis was
  emptied, or it expired after :data:`MARKER_TTL_SECONDS`) each request derives one from
  the database with one small statement (:func:`derived_marker`: the newest publication
  and when it finished, and the counts and newest changes of the runs, predictions, swell
  events, active spots, research assessments and map fields).
* ``epoch`` is a random token kept in Redis: a Redis that lost its data starts a new
  epoch, so this process's memo can never hand out a payload of an earlier one.
* ``hour`` bounds every payload to its hour, whatever the marker says.

A key is computed at most once: the first request takes the key's Redis lock (SET NX EX)
and stores the payload, zlib-compressed (Upstash counts bandwidth), for
:data:`PAYLOAD_TTL_SECONDS`; concurrent requests for the same key wait for it (a few MGETs
with backoff) instead of reading the database too. Routes that read the same rows (a
spot page's forecast and timeline, which the page requests side by side) are computed
together (:class:`Together`): a miss of either computes both under one lock, in one read
of the rows. Each process also keeps the newest payloads in memory (:data:`MEMO_MAX_BYTES`),
so a warm serverless instance answers a repeated request with a single Redis command (the
epoch and marker).

Redis unavailable: the request computes its payload directly, exactly as a cache miss
does, and Redis is not asked again for :data:`FAIL_OPEN_SECONDS` (as the rate limiter does),
so an unreachable Redis delays at most one request per process in that time.

The cached bytes are what FastAPI itself sends for the route's response model (its
``dump_json`` path: ``TypeAdapter.dump_json`` of the validated value, by alias), so a hit,
a miss and a request without Redis return identical bodies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import redis
from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.redis import get_redis_bytes
from app.models import ForecastRun, SpotResearchAssessment, SurfSpot, SwellEvent
from app.models.forecast_engine import MapField
from app.models.sync import SyncRun
from app.schemas.spots import MapSnapshotOut, SpotSummary
from app.services.spot_views import map_snapshot, spot_summaries

logger = logging.getLogger(__name__)

PREFIX = "public:v1"
EPOCH_KEY = f"{PREFIX}:epoch"
MARKER_KEY = f"{PREFIX}:marker"
# A payload outlives its hour a little (the hour is part of its key), then Redis drops it.
PAYLOAD_TTL_SECONDS = 2 * 3600
# A marker the sync stored is trusted this long; the sync stores a new one at every
# publication (about 6 a day), so it expires only when the syncs stop.
MARKER_TTL_SECONDS = 24 * 3600
# The single-flight lock outlives the slowest computation (about 2 s measured for a
# spot's forecast on Neon-like latency); a crashed holder frees it after this.
LOCK_TTL_SECONDS = 60
# Waiting for another request's computation: GETs after these pauses (about 6.4 s in all),
# then the request computes the payload itself without storing it.
WAIT_PAUSES_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2)
FAIL_OPEN_SECONDS = 30.0
MEMO_MAX_BYTES = 32 * 1024 * 1024
# The ?at= times of /api/map/conditions that are cached, relative to the current hour: the
# map's steps (the stored forecast hours, up to 16 days ahead, and the hours just past).
MAP_AT_CACHED_WINDOW = (timedelta(days=-2), timedelta(days=17))
COMPRESSION_LEVEL = 6


def public_now(now: datetime | None = None) -> datetime:
    """The instant the public snapshots are computed for: the start of the UTC hour."""
    return (now or utcnow()).replace(minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Snapshot:
    """One cached response: its JSON body and, for the timeline, its ETag."""

    body: bytes
    etag: str | None = None


def render(adapter: TypeAdapter[Any], value: Any, etag: str | None = None) -> Snapshot:
    """The body FastAPI sends for ``value`` under a route whose response model ``adapter``
    describes (fastapi.routing: validate, then ``dump_json`` by alias)."""
    return Snapshot(adapter.dump_json(adapter.validate_python(value), by_alias=True), etag)


def encode(snapshot: Snapshot) -> bytes:
    meta = json.dumps({"etag": snapshot.etag}, separators=(",", ":")).encode()
    return zlib.compress(meta + b"\n" + snapshot.body, COMPRESSION_LEVEL)


def decode(raw: bytes) -> Snapshot:
    meta, _, body = zlib.decompress(raw).partition(b"\n")
    return Snapshot(body, json.loads(meta).get("etag"))


# --------------------------------------------------------------------------- the marker
def note_published(sync_run_id: int | None = None) -> bool:
    """The sync's hook: call it after the publish stage commits and once more when the sync
    ends (detection, which follows the publication, changes the swell events shown). Every
    public snapshot is computed afresh from the next request on. False when Redis could not
    store the marker (the snapshots then follow the database marker, or the next hour)."""
    marker = f"p{sync_run_id if sync_run_id is not None else 0}.{secrets.token_hex(6)}"
    try:
        get_redis_bytes().set(MARKER_KEY, marker.encode(), ex=MARKER_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Public snapshot marker not stored: %s", type(exc).__name__)
        return False
    return True


def derived_marker(db: Session) -> str:
    """The published data's marker read from the database (one statement over small
    tables), for when no sync stored one."""
    newest = (
        select(SyncRun.id, SyncRun.finished_at)
        .where(SyncRun.published_at.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
        .subquery()
    )
    row = db.execute(
        select(
            select(newest.c.id).scalar_subquery(),
            select(newest.c.finished_at).scalar_subquery(),
            select(func.count(ForecastRun.id)).scalar_subquery(),
            select(func.max(ForecastRun.id)).scalar_subquery(),
            select(func.max(ForecastRun.predictions_computed_at)).scalar_subquery(),
            select(func.max(ForecastRun.completed_at)).scalar_subquery(),
            select(func.count(SwellEvent.id)).scalar_subquery(),
            select(func.max(SwellEvent.last_updated_at)).scalar_subquery(),
            select(func.count(SurfSpot.id)).where(SurfSpot.is_active).scalar_subquery(),
            select(func.max(SpotResearchAssessment.id)).scalar_subquery(),
            # The map-field index: a run's fields pruned or replaced outside a sync.
            select(func.count(MapField.id)).scalar_subquery(),
            select(func.max(MapField.id)).scalar_subquery(),
        )
    ).one()
    text = "|".join(v.isoformat() if isinstance(v, datetime) else str(v) for v in row)
    return "d" + hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- the memo
@dataclass
class _Memo:
    """This process's newest payloads (least recently used dropped first)."""

    max_bytes: int = MEMO_MAX_BYTES
    entries: OrderedDict[str, Snapshot] = field(default_factory=OrderedDict)
    size: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str) -> Snapshot | None:
        with self.lock:
            hit = self.entries.get(key)
            if hit is not None:
                self.entries.move_to_end(key)
            return hit

    def put(self, key: str, snapshot: Snapshot) -> None:
        if len(snapshot.body) > self.max_bytes:
            return
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.size -= len(old.body)
            self.entries[key] = snapshot
            self.size += len(snapshot.body)
            while self.size > self.max_bytes:
                _, dropped = self.entries.popitem(last=False)
                self.size -= len(dropped.body)

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.size = 0


_memo = _Memo()
# (the Redis client that failed, monotonic time until which it is not asked)
_fail_open: tuple[redis.Redis, float] | None = None


def clear_memo() -> None:
    """Forget this process's payloads (tests; a new serverless instance starts empty)."""
    _memo.clear()


def clear() -> None:
    """Forget every payload: this process's and Redis's (a new epoch, so a payload stored
    under the old one is never read again). Ops and tests only."""
    global _fail_open
    _memo.clear()
    _fail_open = None
    try:
        get_redis_bytes().delete(EPOCH_KEY)
    except redis.RedisError as exc:
        logger.warning("Public snapshot epoch not reset: %s", type(exc).__name__)


def _usable_client() -> redis.Redis | None:
    client = get_redis_bytes()
    if _fail_open is not None and _fail_open[0] is client and time.monotonic() < _fail_open[1]:
        return None
    return client


def _failed(client: redis.Redis, exc: Exception) -> None:
    global _fail_open
    _fail_open = (client, time.monotonic() + FAIL_OPEN_SECONDS)
    logger.warning(
        "Public snapshot cache skipped for %.0f s: Redis error %s",
        FAIL_OPEN_SECONDS,
        type(exc).__name__,
    )


def _epoch_and_marker(client: redis.Redis) -> tuple[str, str | None]:
    raw_epoch, raw_marker = client.mget([EPOCH_KEY, MARKER_KEY])
    if raw_epoch is None:
        token = secrets.token_hex(6).encode()
        raw_epoch = token if client.set(EPOCH_KEY, token, nx=True) else client.get(EPOCH_KEY)
        raw_epoch = raw_epoch or token
    marker = raw_marker.decode() if isinstance(raw_marker, bytes) else None
    epoch = raw_epoch.decode() if isinstance(raw_epoch, bytes) else str(raw_epoch)
    return epoch, marker


def _stored(client: redis.Redis, key: str) -> Snapshot | None:
    raw = client.get(key)
    return decode(raw) if isinstance(raw, bytes) else None


def cache_key(epoch: str, marker: str, hour: datetime, route: str, params: str) -> str:
    key = f"{PREFIX}:{epoch}:{marker}:{hour:%Y%m%dT%H}:{route}"
    if params:
        # Query values are visitor input: hashed, so a key stays short and plain.
        key += ":" + hashlib.sha256(params.encode()).hexdigest()[:24]
    return key


# Deletes the lock only while it is still ours (a lock that expired may be another's now).
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _computed_once(
    client: redis.Redis,
    key: str,
    lock: str,
    build_all: Callable[[], dict[str, Snapshot]],
    build_own: Callable[[], Snapshot],
) -> Snapshot:
    """``build_all`` (``key``'s snapshot and any computed with it, by key) under ``lock``
    (SET NX EX), each stored for the other requests; or, when another request holds the
    lock, ``key``'s stored result. A waiter that finds the lock gone without a result (the
    holder failed, e.g. a 404) or waits out WAIT_PAUSES_SECONDS builds its own
    (``build_own``), unstored."""
    token = secrets.token_hex(8)
    if client.set(lock, token, nx=True, ex=LOCK_TTL_SECONDS):
        try:
            built = build_all()
            for name, snap in built.items():
                client.set(name, encode(snap), ex=PAYLOAD_TTL_SECONDS)
            return built[key]
        finally:
            try:
                client.eval(_RELEASE_SCRIPT, 1, lock, token)
            except redis.RedisError as exc:  # it expires after LOCK_TTL_SECONDS anyway
                logger.warning("Public snapshot lock not released: %s", type(exc).__name__)
    for pause in WAIT_PAUSES_SECONDS:
        time.sleep(pause)
        raw, holder = client.mget([key, lock])
        if isinstance(raw, bytes):
            return decode(raw)
        if holder is None:
            break
    return build_own()


@dataclass(frozen=True)
class Together:
    """Routes computed together on a miss of any of them, because they read the same rows
    (a spot page's forecast and timeline read the same hours, about 0.8 MB for a 10-day
    blend): one computation under one lock (``name``), inside ``reads()`` (where the
    builds share their reads), each route's snapshot stored under its own key. A route
    other than the one requested that fails is left to its own request."""

    name: str
    params: str
    builds: Mapping[str, Callable[[datetime], Snapshot]]
    reads: Callable[[], AbstractContextManager[Any]] = nullcontext

    def snapshot(self, db: Session, route: str) -> Snapshot:
        return snapshot(db, route, self.params, self.builds[route], together=self)


def snapshot(
    db: Session,
    route: str,
    params: str,
    build: Callable[[datetime], Snapshot],
    *,
    together: Together | None = None,
) -> Snapshot:
    """The response of ``route`` for ``params`` this hour: from this process's memo, from
    Redis, or computed by ``build(public_now())`` (once per key, with the other routes of
    ``together``; see the module docstring). An exception of ``build`` (e.g. a 404)
    propagates and nothing is stored."""
    hour = public_now()
    client = _usable_client()
    if client is None:
        return build(hour)
    try:
        epoch, marker = _epoch_and_marker(client)
    except redis.RedisError as exc:
        _failed(client, exc)
        return build(hour)
    marker = marker or derived_marker(db)
    key = cache_key(epoch, marker, hour, route, params)
    hit = _memo.get(key)
    if hit is not None:
        return hit
    built: dict[str, Snapshot] = {}

    def build_own() -> Snapshot:
        built[key] = build(hour)
        return built[key]

    def build_all() -> dict[str, Snapshot]:
        if together is None:
            build_own()
            return built
        with together.reads():
            build_own()
            for other, other_build in together.builds.items():
                if other == route:
                    continue
                try:
                    built[cache_key(epoch, marker, hour, other, params)] = other_build(hour)
                except Exception as exc:  # computed by its own request instead
                    logger.warning(
                        "Public snapshot %s not computed with %s: %s",
                        other,
                        route,
                        type(exc).__name__,
                    )
        return built

    lock = cache_key(epoch, marker, hour, together.name if together else route, params)
    try:
        result = _stored(client, key) or _computed_once(
            client, key, f"{lock}:lock", build_all, build_own
        )
    except redis.RedisError as exc:
        _failed(client, exc)
        result = built.get(key) or build(hour)
    for name, snap in built.items():
        _memo.put(name, snap)
    _memo.put(key, result)
    return result


# --------------------------------------------------------------------------- routes
SPOT_LIST = TypeAdapter(list[SpotSummary])
MAP_SNAPSHOT = TypeAdapter(MapSnapshotOut)


def spot_list(db: Session) -> Snapshot:
    """GET /api/spots."""
    return snapshot(db, "spots", "", lambda now: render(SPOT_LIST, spot_summaries(db, now)))


def cached_map_time(at: datetime, hour: datetime) -> bool:
    """Whether /api/map/conditions?at= is cached for ``at``: a whole UTC hour (as the map
    sends its steps) within :data:`MAP_AT_CACHED_WINDOW` of ``hour``. Any other value is
    visitor input that would only fill Redis with keys nobody asks for again."""
    if at.utcoffset() != timedelta(0) or (at.minute, at.second, at.microsecond) != (0, 0, 0):
        return False
    return MAP_AT_CACHED_WINDOW[0] <= at - hour <= MAP_AT_CACHED_WINDOW[1]


def map_conditions(db: Session, at: datetime | None) -> Snapshot:
    """GET /api/map/conditions (``at``: the query's time, as FastAPI parsed it). An ``at``
    that is not cached (:func:`cached_map_time`) is computed as a miss is, unstored."""

    def build(now: datetime) -> Snapshot:
        return render(MAP_SNAPSHOT, map_snapshot(db, at, now))

    if at is not None and not cached_map_time(at, public_now()):
        return build(public_now())
    return snapshot(db, "map-conditions", at.isoformat() if at is not None else "", build)


def prewarm(db: Session) -> bool:
    """Compute the snapshots every visitor's first page needs (the spot list and the
    conditions now) for the current hour, if no request has: the sync can call it after
    :func:`note_published` while its database connection is open, so the first visitor
    after a publication does not wait for them. Redis unavailable: nothing is read or
    computed (they could not be stored) and the result is False."""
    client = _usable_client()
    if client is None:
        return False
    try:
        _epoch_and_marker(client)
    except redis.RedisError as exc:
        _failed(client, exc)
        return False
    spot_list(db)
    map_conditions(db, None)
    return True
