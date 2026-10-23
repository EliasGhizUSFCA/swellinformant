"""Sync state in Redis (foundation contract F3; frozen): heartbeat, known runs, kicks.

Best effort: a Redis error is logged and swallowed (writers return, readers return None or
an empty result), so neither the API nor the daemon fails because Redis is briefly down.

Keys:

* :data:`HEARTBEAT_KEY` ``sync:heartbeat``: JSON written by the daemon at every check
  (expires after 6 h). Data health reads it under JOB_RUNNER=daemon. ``file_problems``
  are the daemon's checks of files only its image ships (the tide model's constants): the
  API function, deployed without data/, reports these instead of its own.
* :data:`KNOWN_KEY` ``sync:known``: :meth:`KnownRuns.to_json` (expires after 7 days), so a
  restarted daemon can check without reading the database.
* :data:`KICK_PREFIX` ``sync:kick:<job>``: requests from the API for the daemon (expire
  after 1 day): ``sync:kick:matching.match`` holds the ISO time of the newest request,
  ``sync:kick:flights.discover`` is a set of match ids (:data:`ALL_MATCHES` = every match).
  :func:`pop_kicks` reads and deletes both in one Lua script.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import redis

from app.core.database import utcnow
from app.core.redis import get_redis
from app.services.sync.types import KnownRuns, SyncPlan

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "sync:heartbeat"
KNOWN_KEY = "sync:known"
KICK_PREFIX = "sync:kick:"
HEARTBEAT_TTL_SECONDS = 21600
KNOWN_TTL_SECONDS = 7 * 86400
KICK_TTL_SECONDS = 86400

KickJob = Literal["matching.match", "flights.discover"]
KICK_JOBS: tuple[KickJob, ...] = ("matching.match", "flights.discover")
MATCH_KICK_KEY = f"{KICK_PREFIX}matching.match"
FLIGHTS_KICK_KEY = f"{KICK_PREFIX}flights.discover"
# Member of the flights kick set meaning "every match" (a kick without match ids).
ALL_MATCHES = "*"

# GET+DEL the matching flag and SMEMBERS+DEL the flights set, atomically.
_POP_SCRIPT = """
local requested = redis.call('GET', KEYS[1])
if requested then
    redis.call('DEL', KEYS[1])
end
local ids = redis.call('SMEMBERS', KEYS[2])
if #ids > 0 then
    redis.call('DEL', KEYS[2])
end
return {requested or '', ids}
"""


@dataclass(frozen=True)
class Heartbeat:
    """The daemon's last check (:func:`read_heartbeat`)."""

    at: datetime
    runner: str
    due: bool
    reasons: list[str] = field(default_factory=list)
    components: list[dict[str, Any]] = field(default_factory=list)
    cpu_s_month: float | None = None
    note: str | None = None
    # Problems (app.services.tides.health problem dicts) of the files the daemon reads;
    # None from a daemon that does not report them.
    file_problems: list[dict[str, Any]] | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.at).total_seconds()


def write_heartbeat(
    plan: SyncPlan | None,
    *,
    runner: str,
    cpu_s_month: float | None = None,
    note: str | None = None,
    file_problems: list[dict[str, Any]] | None = None,
) -> None:
    """Record a check: when, by which runner, and what the plan found."""
    payload = {
        "at": utcnow().isoformat(),
        "runner": runner,
        "due": bool(plan.due) if plan is not None else False,
        "reasons": list(plan.reasons) if plan is not None else [],
        "components": [c.as_dict() for c in plan.components] if plan is not None else [],
        "cpu_s_month": cpu_s_month,
        "note": note,
        "file_problems": file_problems,
    }
    try:
        get_redis().set(HEARTBEAT_KEY, json.dumps(payload), ex=HEARTBEAT_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Sync heartbeat not written: %s", type(exc).__name__)


def read_heartbeat() -> Heartbeat | None:
    """The last heartbeat, or None (none written, expired, unreadable or Redis down)."""
    try:
        raw = get_redis().get(HEARTBEAT_KEY)
    except redis.RedisError as exc:
        logger.warning("Sync heartbeat not read: %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        cpu = data.get("cpu_s_month")
        files = data.get("file_problems")
        return Heartbeat(
            at=datetime.fromisoformat(data["at"]),
            runner=str(data["runner"]),
            due=bool(data.get("due", False)),
            reasons=[str(r) for r in data.get("reasons", [])],
            components=[dict(c) for c in data.get("components", [])],
            cpu_s_month=float(cpu) if cpu is not None else None,
            note=data.get("note"),
            file_problems=[dict(p) for p in files] if files is not None else None,
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Sync heartbeat unreadable: %s", type(exc).__name__)
        return None


def write_known(known: KnownRuns) -> None:
    try:
        get_redis().set(KNOWN_KEY, known.to_json(), ex=KNOWN_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("Known sync runs not written: %s", type(exc).__name__)


def read_known() -> KnownRuns | None:
    try:
        raw = get_redis().get(KNOWN_KEY)
    except redis.RedisError as exc:
        logger.warning("Known sync runs not read: %s", type(exc).__name__)
        return None
    if not raw:
        return None
    try:
        return KnownRuns.from_json(raw)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Known sync runs unreadable: %s", type(exc).__name__)
        return None


def request_kick(job: KickJob, match_ids: Iterable[str] = ()) -> bool:
    """Ask the daemon to run ``job`` soon. False when Redis is unavailable.

    matching.match: the flag holds the time of the newest request (``match_ids`` is
    ignored). flights.discover: the ids are added to the set; no ids = :data:`ALL_MATCHES`.
    """
    if job not in KICK_JOBS:
        raise ValueError(f"unknown kick job {job!r}")
    try:
        client = get_redis()
        if job == "matching.match":
            client.set(MATCH_KICK_KEY, utcnow().isoformat(), ex=KICK_TTL_SECONDS)
        else:
            members = [str(m) for m in match_ids] or [ALL_MATCHES]
            pipe = client.pipeline(transaction=True)
            pipe.sadd(FLIGHTS_KICK_KEY, *members)
            pipe.expire(FLIGHTS_KICK_KEY, KICK_TTL_SECONDS)
            pipe.execute()
    except redis.RedisError as exc:
        logger.warning("Kick %s not recorded: %s", job, type(exc).__name__)
        return False
    return True


def pop_kicks() -> dict[str, list[str]]:
    """Take every pending kick: {job: [request time] or [match ids]}; {} when none.

    Atomic (one Lua script), so a kick written meanwhile is either returned now or kept for
    the next call, never lost. {} when Redis is unavailable (the kicks stay in Redis).
    """
    try:
        requested, ids = get_redis().eval(_POP_SCRIPT, 2, MATCH_KICK_KEY, FLIGHTS_KICK_KEY)
    except redis.RedisError as exc:
        logger.warning("Kicks not read: %s", type(exc).__name__)
        return {}
    out: dict[str, list[str]] = {}
    if requested:
        out["matching.match"] = [str(requested)]
    if ids:
        out["flights.discover"] = sorted(str(i) for i in ids)
    return out
