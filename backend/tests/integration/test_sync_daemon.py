"""The sync daemon (``python -m app.cli sync daemon``, docs/sync.md).

``--once``: init-db runs, the API's kicks are consumed, the heartbeat is written and a due
sync runs. An idle check makes no database query at all (SQLAlchemy cursor and pool
checkout events counted). Periodic jobs fall due on in-memory timers, batched; SIGTERM
stops the loop. Demo spot forecasts (synthetic by design); no live provider is contacted.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import cli
from app.core.config import get_settings
from app.core.database import get_engine
from app.jobs.schedule import DailyAt, Every, JobEntry
from app.models import BackgroundJobLog, TideStation
from app.models.sync import SyncRun
from app.services.sync import daemon as daemon_module
from app.services.sync import state
from app.services.sync.daemon import PeriodicJobs, SyncDaemon, periodic_entries, run_daemon
from app.services.sync.orchestrator import run_sync
from tests.sync_fixtures import count_queries

pytestmark = pytest.mark.integration


def job_names(db: Session) -> list[str]:
    db.expire_all()
    return [
        n for (n,) in db.execute(select(BackgroundJobLog.job_name).order_by(BackgroundJobLog.id))
    ]


@pytest.fixture
def published(db: Session, few_spots: list[str]) -> None:
    """The current demo run is stored and published: nothing is due."""
    assert run_sync(trigger="cli").status == "success"
    db.execute(BackgroundJobLog.__table__.delete())
    db.commit()


# --------------------------------------------------------------------------- --once
def test_once_initialises_consumes_kicks_writes_the_heartbeat_and_syncs(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.flights import service as flights

    asked: list[Any] = []
    real = flights.discover_flights

    def discover(db: Session, now: Any = None, limit: int = 50, match_ids: Any = None) -> Any:
        asked.append(match_ids)
        return real(db, now, limit, match_ids)

    monkeypatch.setattr(flights, "discover_flights", discover)
    assert db.scalar(select(func.count()).select_from(TideStation)) == 0
    assert state.request_kick("matching.match")
    ids = sorted(str(uuid.uuid4()) for _ in range(2))  # matches the API asked flights for
    assert state.request_kick("flights.discover", ids)

    assert run_daemon(once=True, init_db=True) == 0
    # Alembic's logging setup did not replace the application's (redaction filter, level).
    import logging

    from app.core.logging import RedactingFilter

    root = logging.getLogger()
    assert root.handlers and all(getattr(h, "_swell", False) for h in root.handlers)
    assert any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters)

    db.expire_all()
    # init-db: migrated (already at head), seeded, tide catalog loaded.
    assert int(db.scalar(select(func.count()).select_from(TideStation)) or 0) > 0
    # The kicks were consumed: matching and the stages after it, then the flights of the
    # two matches asked for (and the stages after it).
    assert state.pop_kicks() == {}
    names = job_names(db)
    kick_jobs = [
        "matching.match",
        "flights.discover",
        "alerts.generate",
        "notifications.deliver",
        "flights.discover",
        "alerts.generate",
        "notifications.deliver",
    ]
    assert names[: len(kick_jobs)] == kick_jobs
    assert asked[:2] == [None, ids]
    # Nothing was published yet, so the first check found the demo run new and synced.
    assert "sync.run" in names
    row = db.scalar(select(SyncRun))
    assert row is not None and row.trigger == "daemon" and row.published_at is not None
    beat = state.read_heartbeat()
    assert beat is not None and beat.runner == "daemon" and beat.due
    assert beat.note == f"sync {row.id} {row.status}"
    assert beat.cpu_s_month is not None and beat.cpu_s_month > 0
    known = state.read_known()
    assert known is not None and known.stored["demo"].startswith("demo:")


def test_a_failed_init_db_still_restores_the_redacting_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alembic's fileConfig replaces the root handlers (and the redaction filter) during
    migrate; when init-db then fails, the start-up error must still be logged through the
    application's redacting handlers."""
    import logging

    from app.core.logging import RedactingFilter, configure_logging

    configure_logging()

    def init_database() -> dict[str, Any]:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.addHandler(logging.StreamHandler())  # what alembic/env.py's fileConfig does
        raise RuntimeError("the database is still waking up (test)")

    monkeypatch.setattr(cli, "init_database", init_database)
    with pytest.raises(RuntimeError, match="still waking"):
        SyncDaemon(get_settings()).start(init_db=True)
    root = logging.getLogger()
    assert root.handlers and all(getattr(h, "_swell", False) for h in root.handlers)
    assert any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters)


def test_an_idle_check_makes_no_database_query(
    db: Session, published: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    calls: list[str] = []
    from app.services.sync import detect

    real = detect.build_probes

    def probes(settings: Any) -> Any:
        calls.append("check")
        return real(settings)

    monkeypatch.setattr(detect, "build_probes", probes)
    counter, remove = count_queries(get_engine())
    try:
        wait = daemon.step()
    finally:
        remove()
    assert calls == ["check"]  # the check ran (provider metadata only)
    assert daemon.last_plan is not None and not daemon.last_plan.due
    assert counter.statements == [] and counter.checkouts == 0
    assert get_engine().pool.checkedin() == 0  # start() left no idle connection open
    beat = state.read_heartbeat()
    assert beat is not None and not beat.due and beat.note is None
    # Next: the kick poll (SYNC_DAEMON_POLL_SECONDS), well before the next check.
    assert 0 < wait <= get_settings().sync_daemon_poll_seconds
    assert job_names(db) == []


def test_kicks_wake_the_database_and_idle_polls_do_not(db: Session, published: None) -> None:
    now = [1000.0]
    daemon = SyncDaemon(get_settings(), monotonic=lambda: now[0])
    daemon.start(init_db=False)
    daemon.step()  # first check (idle)
    counter, remove = count_queries(get_engine())
    try:
        now[0] += get_settings().sync_daemon_poll_seconds
        daemon.step()  # a poll without kicks
        assert counter.total == 0
        state.request_kick("matching.match")
        now[0] += get_settings().sync_daemon_poll_seconds
        daemon.step()
    finally:
        remove()
    assert counter.total > 0
    assert job_names(db)[:1] == ["matching.match"]
    # After the wake no pooled connection stays open (a scale-to-zero database can sleep).
    assert get_engine().pool.checkedin() == 0


def test_a_flights_kick_without_ids_refreshes_every_match(
    db: Session, published: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Refresh flights" without match ids is stored as ALL_MATCHES: the daemon runs
    flights.discover for every match (match_ids None), never for a match id "*"."""
    from app.services.flights import service as flights

    asked: list[Any] = []
    real = flights.discover_flights

    def discover(db: Session, now: Any = None, limit: int = 50, match_ids: Any = None) -> Any:
        asked.append(match_ids)
        return real(db, now, limit, match_ids)

    monkeypatch.setattr(flights, "discover_flights", discover)
    assert state.request_kick("flights.discover")
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    assert daemon.poll_kicks() == {"flights.discover": [state.ALL_MATCHES]}
    assert asked[:1] == [None]
    assert job_names(db)[:1] == ["flights.discover"]


def test_sync_check_reads_the_known_runs_from_redis_not_the_database(
    db: Session, published: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sync check` loads the stored keys from the database once (Redis has none yet) and
    then, like the daemon's check, needs Redis and the providers only."""
    assert state.read_known() is None
    assert cli.main(["sync", "check", "--json"]) == 0
    capsys.readouterr()
    assert state.read_known() is not None
    counter, remove = count_queries(get_engine())
    try:
        assert cli.main(["sync", "check", "--json"]) == 0
    finally:
        remove()
    out = json.loads(capsys.readouterr().out)
    assert out["due"] is False
    assert counter.statements == [] and counter.checkouts == 0


def test_periodic_jobs_run_in_one_batch_from_in_memory_timers(
    db: Session, published: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "job_interval_floor_minutes", 60)
    now = [5000.0]
    daemon = SyncDaemon(get_settings(), monotonic=lambda: now[0])
    daemon.start(init_db=False)
    assert daemon.jobs is not None
    every = [t for t in daemon.jobs.timers]
    assert every and all(t.interval_s >= 3600 for t in every)
    assert not {t.entry.task for t in every} & daemon_module.SYNC_OWNED_TASKS
    daemon.step()
    assert job_names(db) == []  # nothing is due at start
    now[0] += 3600
    expected = [t.entry.task for t in every if t.interval_s == 3600]
    assert len(expected) >= 5  # everything at the 60-minute floor falls due together
    assert daemon.run_periodic() == expected  # one batch, back to back
    assert job_names(db) == expected
    assert daemon.run_periodic() == []  # not again until the next hour


def test_the_daemon_takes_the_known_runs_a_command_line_sync_wrote(
    db: Session, few_spots: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A `sync run` by hand while the daemon runs reloads the known runs into Redis; the
    daemon's next check uses them and does not sync the same run again."""
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    assert daemon.known is not None and daemon.known.stored == {}  # nothing published yet
    assert cli.main(["sync", "run"]) == 0
    capsys.readouterr()
    plan = daemon.check()
    assert not plan.due
    assert daemon.known.stored.get("demo", "").startswith("demo:")
    assert db.scalar(select(func.count()).select_from(SyncRun)) == 1  # no second sync


def test_the_cpu_meter_carries_this_month_over_a_restart(db: Session, published: None) -> None:
    from app.core.database import utcnow

    state.write_heartbeat(None, runner="daemon", cpu_s_month=1234.5)  # the previous process
    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    assert daemon.cpu is not None and daemon.cpu.carry == 1234.5
    daemon.check()
    beat = state.read_heartbeat()
    assert beat is not None and beat.cpu_s_month is not None and beat.cpu_s_month >= 1234.5
    # A heartbeat of an earlier month is not carried into a new month.
    later = utcnow() + timedelta(days=40)
    nxt = SyncDaemon(get_settings(), wall=lambda: later)
    nxt.start(init_db=False)
    assert nxt.cpu is not None and nxt.cpu.carry == 0.0


def test_sigterm_stops_the_loop(db: Session, published: None) -> None:
    timer = threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
    previous = signal.getsignal(signal.SIGTERM)
    timer.start()
    try:
        assert run_daemon(init_db=False) == 0
    finally:
        timer.cancel()
    assert signal.getsignal(signal.SIGTERM) == previous  # the handler is restored
    beat = state.read_heartbeat()
    assert beat is not None and beat.runner == "daemon"


def test_a_stop_event_ends_the_daemon(db: Session, published: None) -> None:
    stop = threading.Event()
    out: dict[str, int] = {}
    worker = threading.Thread(target=lambda: out.setdefault("code", run_daemon(stop=stop)))
    worker.start()
    threading.Timer(1.0, stop.set).start()
    worker.join(30)
    assert out == {"code": 0}


def test_the_sync_command_runs_the_daemon_once(
    db: Session, published: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(daemon_module, "run_daemon", lambda **kw: seen.append(kw) or 0)
    assert cli.main(["sync", "daemon", "--once", "--no-init-db"]) == 0
    assert cli.main(["sync", "daemon"]) == 0
    assert seen == [{"once": True, "init_db": False}, {"once": False, "init_db": None}]


# --------------------------------------------------------------------------- timers
T = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)


def test_every_jobs_start_one_interval_after_start_and_respect_the_floor() -> None:
    entries = [
        JobEntry("delivery", "notifications.deliver", Every(2)),
        JobEntry("health", "ops.data_health", Every(60)),
        JobEntry("tides", "tides.acquire", DailyAt((2,), 20)),
    ]
    jobs = PeriodicJobs(entries, start=0.0, start_wall=T, floor_minutes=60)
    assert jobs.due(10.0, T + timedelta(seconds=10)) == []
    assert [e.name for e in jobs.due(3600.0, T + timedelta(hours=1))] == ["delivery", "health"]
    assert jobs.due(3601.0, T + timedelta(hours=1, seconds=1)) == []
    # 02:20 UTC passes: the daily job is due once.
    assert [e.name for e in jobs.due(5000.0, T + timedelta(hours=1, minutes=21))] == ["tides"]
    assert jobs.due(5001.0, T + timedelta(hours=1, minutes=22)) == []
    assert jobs.seconds_until_next(5001.0, T + timedelta(hours=1, minutes=22)) == 2199.0
    without_floor = PeriodicJobs(entries[:1], start=0.0, start_wall=T)
    assert [e.name for e in without_floor.due(120.0, T)] == ["delivery"]


def test_the_daemon_never_schedules_the_syncs_own_jobs() -> None:
    tasks = {e.task for e in periodic_entries(get_settings())}
    assert tasks and not tasks & daemon_module.SYNC_OWNED_TASKS
    assert "notifications.deliver" in tasks and "ops.data_health" in tasks
