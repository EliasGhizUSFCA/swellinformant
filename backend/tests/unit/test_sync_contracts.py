"""Sync contracts: types, the Redis state helpers and their failure mode.

Redis: fakeredis when it is installed, else the session's TEST_REDIS_URL Redis (the keys
used here are deleted before and after each test).
"""

from __future__ import annotations

import json
import typing
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.redis import get_redis
from app.services.sync import state
from app.services.sync.types import (
    STAGE_JOB_NAMES,
    STAGES,
    ComponentStatus,
    KnownRuns,
    StageName,
    StageResult,
    SyncBusy,
    SyncPlan,
    SyncResult,
    known_key,
)

T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
CYCLE = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
KEYS = (state.HEARTBEAT_KEY, state.KNOWN_KEY, state.MATCH_KICK_KEY, state.FLIGHTS_KICK_KEY)


def component(kind: str = "spots", state_: str = "new", **kw: object) -> ComponentStatus:
    base: dict[str, object] = {
        "kind": kind,
        "source_code": f"src_{kind}",
        "provider": "open_meteo",
        "state": state_,
        "stored_run_key": "old",
        "available_run_key": "new",
        "available_cycle": CYCLE,
    }
    return ComponentStatus(**{**base, **kw})  # type: ignore[arg-type]


def plan(*components: ComponentStatus, due: bool = True, forced: bool = False) -> SyncPlan:
    return SyncPlan(
        checked_at=T0,
        due=due,
        components=tuple(components),
        anchor_cycle=CYCLE,
        reasons=("new GFS run",),
        forced=forced,
    )


# --------------------------------------------------------------------------- types
def test_stage_order_and_job_names() -> None:
    assert typing.get_args(StageName) == STAGES
    assert STAGES == (
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
    assert dict(STAGE_JOB_NAMES) == {
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
    with pytest.raises(TypeError):
        STAGE_JOB_NAMES["spots"] = "x"  # type: ignore[index]


def test_due_kinds() -> None:
    p = plan(
        component("spots", "new"),
        component("ensemble", "new"),
        component("map", "waiting"),
        due=True,
    )
    # The ensemble is no trigger by default, but a sync started by the spots takes it along.
    assert p.due_kinds() == frozenset({"spots", "ensemble"})
    assert plan(component("map", "current"), component("spots", "failed")).due_kinds() == set()
    forced = plan(
        component("spots", "current"),
        component("map", "unknown"),
        component("ensemble", "disabled"),
        forced=True,
    )
    assert forced.due_kinds() == frozenset({"spots", "map"})


def test_plan_and_result_dicts_are_json_safe() -> None:
    c = component(first_seen_at=T0, detail="waiting for the map", retry_after_s=60.0, attempts=2)
    assert c.as_dict() == {
        "kind": "spots",
        "source_code": "src_spots",
        "provider": "open_meteo",
        "state": "new",
        "stored_run_key": "old",
        "available_run_key": "new",
        "available_cycle": CYCLE.isoformat(),
        "first_seen_at": T0.isoformat(),
        "detail": "waiting for the map",
        "retry_after_s": 60.0,
        "attempts": 2,
    }
    p = plan(c, component("map", "current"))
    d = p.as_dict()
    assert d["due"] is True and d["forced"] is False and d["due_kinds"] == ["spots"]
    assert d["anchor_cycle"] == CYCLE.isoformat() and d["waiting_until"] is None
    assert d["reasons"] == ["new GFS run"] and len(d["components"]) == 2
    stage = StageResult("spots", "success", T0, 1200, 0.85, {"at": T0, "ids": (1, 2)})
    assert stage.as_dict() == {
        "name": "spots",
        "status": "success",
        "started_at": T0.isoformat(),
        "duration_ms": 1200,
        "cpu_s": 0.85,
        "detail": {"at": T0.isoformat(), "ids": [1, 2]},
    }
    result = SyncResult(7, "success", p, [stage], T0)
    out = result.as_dict()
    assert out["sync_run_id"] == 7 and out["published_at"] == T0.isoformat()
    assert out["stages"] == [stage.as_dict()] and out["plan"] == d
    json.dumps(out)  # serialisable as is


def test_known_runs_round_trip() -> None:
    key = known_key("open_meteo_gfs", "gfs:2026101806")
    assert key == "open_meteo_gfs|gfs:2026101806"
    known = KnownRuns(
        stored={"open_meteo_gfs": "gfs:2026101800", "noaa_gfs_fields": "gfs:2026101800"},
        map_cycle=CYCLE,
        first_seen={key: T0},
        attempts={key: (2, T0 + timedelta(minutes=15))},
        loaded_at=T0,
    )
    again = KnownRuns.from_json(known.to_json())
    assert again == known
    empty = KnownRuns(stored={}, map_cycle=None, first_seen={}, attempts={}, loaded_at=T0)
    assert KnownRuns.from_json(empty.to_json().encode()) == empty
    with pytest.raises((ValueError, KeyError)):
        KnownRuns.from_json("{}")


def test_sync_busy_names_the_holder() -> None:
    busy = SyncBusy("host:42:abc", T0)
    assert isinstance(busy, RuntimeError)
    assert busy.holder == "host:42:abc" and busy.lease_expires_at == T0
    assert "host:42:abc" in str(busy) and T0.isoformat() in str(busy)


# --------------------------------------------------------------------------- Redis
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[redis.Redis]:
    try:
        import fakeredis  # type: ignore[import-not-found]
    except ImportError:
        real = get_redis()
        try:
            real.ping()
        except redis.RedisError:
            pytest.skip("Redis not available (TEST_REDIS_URL)")
        real.delete(*KEYS)
        yield real
        real.delete(*KEYS)
        return
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(state, "get_redis", lambda: fake)
    yield fake


def test_heartbeat_round_trip(client: redis.Redis) -> None:
    assert state.read_heartbeat() is None
    p = plan(component("spots", "new"), component("map", "waiting"))
    state.write_heartbeat(p, runner="daemon", cpu_s_month=1234.5, note="idle")
    beat = state.read_heartbeat()
    assert beat is not None
    assert beat.runner == "daemon" and beat.due is True and beat.note == "idle"
    assert beat.reasons == ["new GFS run"] and beat.cpu_s_month == 1234.5
    assert [c["kind"] for c in beat.components] == ["spots", "map"]
    assert 0 <= beat.age_seconds() < 60 and beat.at.tzinfo is not None
    ttl = int(client.ttl(state.HEARTBEAT_KEY))
    assert 0 < ttl <= state.HEARTBEAT_TTL_SECONDS == 21600
    state.write_heartbeat(None, runner="daemon")
    idle = state.read_heartbeat()
    assert idle is not None and idle.due is False and idle.components == []
    client.set(state.HEARTBEAT_KEY, "not json")
    assert state.read_heartbeat() is None


def test_known_runs_in_redis(client: redis.Redis) -> None:
    assert state.read_known() is None
    known = KnownRuns(
        stored={"open_meteo_gfs": "k1"}, map_cycle=CYCLE, first_seen={}, attempts={}, loaded_at=T0
    )
    state.write_known(known)
    assert state.read_known() == known
    assert 0 < int(client.ttl(state.KNOWN_KEY)) <= state.KNOWN_TTL_SECONDS == 7 * 86400
    client.set(state.KNOWN_KEY, '{"stored": {}}')
    assert state.read_known() is None


def test_kicks_are_written_and_popped_once(client: redis.Redis) -> None:
    assert state.pop_kicks() == {}
    assert state.request_kick("matching.match") is True
    assert state.request_kick("flights.discover", ["m2", "m1"]) is True
    assert state.request_kick("flights.discover", ("m1",)) is True
    assert state.KICK_PREFIX == "sync:kick:"
    assert state.MATCH_KICK_KEY == "sync:kick:matching.match"
    assert state.FLIGHTS_KICK_KEY == "sync:kick:flights.discover"
    for key in (state.MATCH_KICK_KEY, state.FLIGHTS_KICK_KEY):
        assert 0 < int(client.ttl(key)) <= state.KICK_TTL_SECONDS == 86400
    kicks = state.pop_kicks()
    assert set(kicks) == {"matching.match", "flights.discover"}
    assert datetime.fromisoformat(kicks["matching.match"][0]).tzinfo is not None
    assert kicks["flights.discover"] == ["m1", "m2"]
    assert state.pop_kicks() == {}  # consumed
    assert client.exists(state.MATCH_KICK_KEY, state.FLIGHTS_KICK_KEY) == 0
    # A flights kick without match ids means every match.
    state.request_kick("flights.discover")
    assert state.pop_kicks() == {"flights.discover": [state.ALL_MATCHES]}
    with pytest.raises(ValueError, match="unknown kick job"):
        state.request_kick("forecasts.acquire")  # type: ignore[arg-type]


def test_redis_down_is_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    down = redis.Redis(
        host="127.0.0.1",
        port=1,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
        retry=Retry(NoBackoff(), 0),  # redis-py retries 3 times with backoff by default
    )
    monkeypatch.setattr(state, "get_redis", lambda: down)
    known = KnownRuns(stored={}, map_cycle=None, first_seen={}, attempts={}, loaded_at=T0)
    state.write_heartbeat(plan(component()), runner="daemon")
    state.write_known(known)
    assert state.read_heartbeat() is None
    assert state.read_known() is None
    assert state.request_kick("matching.match") is False
    assert state.request_kick("flights.discover", ["m1"]) is False
    assert state.pop_kicks() == {}
    assert "ConnectionError" in caplog.text
