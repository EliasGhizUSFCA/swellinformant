"""The sync daemon, ``python -m app.cli sync daemon`` (interface F7; docs/sync.md).

:func:`run_daemon` initialises the database when asked (SYNC_DAEMON_INIT_DB: migrate, seed,
tide catalog; idempotent), loads the known runs from the database (keeping the first
sightings and attempts cached in Redis), and loops until ``stop`` is set (SIGTERM, SIGINT):

* every SYNC_DAEMON_POLL_SECONDS: the API's kicks (Redis only). "Match my search" runs
  matching.match and the stages after it (flights, alerts, delivery), "refresh flights"
  runs flights.discover for those matches and the stages after it;
* every SYNC_CHECK_MINUTES: :func:`plan_sync` from the known runs (provider metadata only,
  no database access) and the heartbeat in Redis; when a sync is due, one
  :func:`run_sync` and a reload of the known runs;
* the periodic jobs of app.jobs.schedule.build_schedule (all but the sync's own jobs),
  each at least JOB_INTERVAL_FLOOR_MINUTES apart. ``Every`` jobs are due by in-memory
  timers that start one interval after the daemon (no database read for scheduling), so
  jobs with the same interval fall due together and run back to back in one batch (one
  database wake); ``DailyAt`` jobs are due by the wall clock (UTC).

An idle iteration (nothing due, no kick, no periodic job) reads Redis and the providers'
metadata only, and no database connection stays open between database wakes (the pool is
emptied after each one), so a scale-to-zero database can suspend. ``stop`` ends the loop
after the current stage (the sync releases its lease). Never imports Celery or kombu.

CPU accounting: every job logs its own ``cpu_s`` (background_job_logs); the heartbeat's
``cpu_s_month`` is this month's daemon CPU outside those jobs (checks, kick polls, start-up),
carried over restarts through the previous heartbeat, so job logs plus the heartbeat count
each CPU second once.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, get_engine, utcnow
from app.jobs.registry import JOB_SPECS, discover_flights_run, job_spec
from app.jobs.runner import cpu_seconds, run_job
from app.jobs.schedule import DailyAt, Every, JobEntry, build_schedule
from app.services.sync import state
from app.services.sync.detect import plan_sync, refresh_known
from app.services.sync.orchestrator import remember, run_sync
from app.services.sync.types import KnownRuns, SyncBusy, SyncPlan

logger = logging.getLogger(__name__)

# Jobs a sync runs (and Celery's sync check): never on a timer in the daemon. The D14
# schedule (app.jobs.schedule) no longer lists the acquisition, prediction and detection
# jobs; excluding them here keeps the daemon right with an older schedule too.
SYNC_OWNED_TASKS = frozenset(
    {
        "sync.tick",
        "forecasts.acquire",
        "forecasts.acquire_ensemble",
        "forecasts.map_fields",
        "quality.predict",
        "detection.detect",
    }
)
START_RETRY_SECONDS = 60
MIN_WAIT_SECONDS = 1.0
ERROR_WAIT_SECONDS = 60.0


# --------------------------------------------------------------------------- periodic jobs
@dataclass
class _Timer:
    entry: JobEntry
    interval_s: float
    next_due: float  # monotonic seconds


class PeriodicJobs:
    """Which periodic jobs are due, without any database read.

    ``Every`` entries: first due one interval after ``start`` (like Celery beat), then every
    interval; the interval is at least ``floor_minutes``. ``DailyAt`` entries: due when one
    of their UTC times passed since the previous call (not at start-up for earlier times).
    """

    def __init__(
        self,
        entries: Iterable[JobEntry],
        *,
        start: float,
        start_wall: datetime,
        floor_minutes: float = 0,
    ) -> None:
        self.timers: list[_Timer] = []
        self.daily: list[JobEntry] = []
        self.last_wall = start_wall
        for entry in entries:
            if isinstance(entry.schedule, Every):
                interval = max(float(entry.schedule.minutes), float(floor_minutes)) * 60
                self.timers.append(_Timer(entry, interval, start + interval))
            elif isinstance(entry.schedule, DailyAt):
                self.daily.append(entry)

    @staticmethod
    def _slots(schedule: DailyAt, after: datetime, until: datetime) -> list[datetime]:
        out: list[datetime] = []
        day = after.replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= until:
            for hour in sorted(set(schedule.hours)):
                slot = day.replace(hour=hour, minute=schedule.minute)
                if after < slot <= until:
                    out.append(slot)
            day += timedelta(days=1)
        return out

    def due(self, now: float, now_wall: datetime) -> list[JobEntry]:
        """The entries due now (each once), in schedule order; advances their timers."""
        out: list[JobEntry] = []
        for t in self.timers:
            if now >= t.next_due:
                out.append(t.entry)
                t.next_due += t.interval_s
                if t.next_due <= now:  # far behind (e.g. a long sync): restart the timer
                    t.next_due = now + t.interval_s
        if now_wall > self.last_wall:
            for entry in self.daily:
                assert isinstance(entry.schedule, DailyAt)
                if self._slots(entry.schedule, self.last_wall, now_wall):
                    out.append(entry)
            self.last_wall = now_wall
        return out

    def seconds_until_next(self, now: float, now_wall: datetime) -> float:
        waits = [t.next_due - now for t in self.timers]
        for entry in self.daily:
            assert isinstance(entry.schedule, DailyAt)
            slots = self._slots(entry.schedule, now_wall, now_wall + timedelta(days=1, hours=1))
            if slots:
                waits.append((slots[0] - now_wall).total_seconds())
        return max(0.0, min(waits)) if waits else float("inf")


def periodic_entries(settings: Settings) -> list[JobEntry]:
    """The schedule's entries the daemon runs on timers (known jobs, not the sync's own)."""
    entries = []
    for entry in build_schedule(settings).values():
        if entry.task in SYNC_OWNED_TASKS:
            continue
        if entry.task not in JOB_SPECS:
            logger.warning("Periodic job %s (%s) has no job spec; skipped", entry.name, entry.task)
            continue
        entries.append(entry)
    return entries


# --------------------------------------------------------------------------- CPU meter
@dataclass
class CpuMeter:
    """This month's daemon CPU outside job runs (see the module docs)."""

    month: tuple[int, int]
    base: float = 0.0  # process CPU at the start of the month (0: the process start)
    carry: float = 0.0  # earlier processes' share this month (previous heartbeat)
    jobs: float = 0.0  # CPU logged by jobs this month

    def outside_jobs(self, now: datetime) -> float:
        if (now.year, now.month) != self.month:
            self.month, self.base, self.carry, self.jobs = (
                (now.year, now.month),
                cpu_seconds(),
                0.0,
                0.0,
            )
        return round(self.carry + max(0.0, cpu_seconds() - self.base - self.jobs), 3)


# --------------------------------------------------------------------------- the daemon
@dataclass
class SyncDaemon:
    """One daemon: :meth:`start`, then :meth:`step` until stopped (see run_daemon)."""

    settings: Settings
    stop: threading.Event = field(default_factory=threading.Event)
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = utcnow
    known: KnownRuns | None = None
    jobs: PeriodicJobs | None = None
    next_poll: float = 0.0
    next_check: float = 0.0
    last_plan: SyncPlan | None = None
    cpu: CpuMeter | None = None

    def start(self, *, init_db: bool) -> None:
        """Initialise the database (optional), load the known runs, start the timers."""
        if init_db:
            from app.cli import init_database

            try:
                done = init_database()
            finally:  # also when it fails: the error is logged through the redaction filter
                restore_logging()
            logger.info("Sync daemon: init-db %s", done)
        cached = state.read_known()
        with SessionLocal() as db:
            self.known = refresh_known(db, cached)  # first sightings, attempts kept
        state.write_known(self.known)
        now, now_wall = self.monotonic(), self.wall()
        self.jobs = PeriodicJobs(
            periodic_entries(self.settings),
            start=now,
            start_wall=now_wall,
            floor_minutes=self.settings.job_interval_floor_minutes,
        )
        self.next_poll = self.next_check = now
        previous = state.read_heartbeat()
        carry = 0.0
        same_month = previous is not None and (previous.at.year, previous.at.month) == (
            now_wall.year,
            now_wall.month,
        )
        if previous is not None and same_month and previous.cpu_s_month is not None:
            carry = previous.cpu_s_month
        self.cpu = CpuMeter((now_wall.year, now_wall.month), carry=carry)
        release_connections()

    # ---------------------------------------------------------------- one iteration
    def step(self) -> float:
        """One iteration: kicks, the check (and a sync), periodic jobs. Returns the seconds
        until something is due next."""
        assert self.jobs is not None, "start() first"
        now = self.monotonic()
        if now >= self.next_poll:
            self.next_poll = now + self.settings.sync_daemon_poll_seconds
            self.poll_kicks()
        if not self.stop.is_set() and self.monotonic() >= self.next_check:
            self.next_check = self.monotonic() + self.settings.sync_check_minutes * 60
            self.check()
        if not self.stop.is_set():
            self.run_periodic()
        now = self.monotonic()
        nxt = min(
            self.next_poll, self.next_check, now + self.jobs.seconds_until_next(now, self.wall())
        )
        return max(0.0, nxt - now)

    def check(self) -> SyncPlan:
        """Plan from the known runs (no database), heartbeat, and sync when due."""
        assert self.known is not None and self.cpu is not None
        cached = state.read_known()  # e.g. written by `sync run` from the command line
        if cached is not None and cached.loaded_at > self.known.loaded_at:
            self.known = cached
        plan = plan_sync(None, settings=self.settings, known=self.known)
        self.last_plan = plan
        state.write_known(self.known)
        self._heartbeat(plan, None)
        if not plan.due:
            logger.debug("Sync check: not due (%s)", "; ".join(plan.reasons))
            return plan
        logger.info("Sync due: %s", "; ".join(plan.reasons))
        cpu0 = cpu_seconds()
        try:
            result = run_sync(trigger="daemon", plan=plan, stop=self.stop)
        except SyncBusy as exc:
            release_connections()
            self._heartbeat(plan, f"busy: {exc}")
            return plan
        finally:
            self.cpu.jobs += cpu_seconds() - cpu0
        remember(self.known, result)
        release_connections()
        self._heartbeat(plan, f"sync {result.sync_run_id} {result.status}")
        return plan

    def _heartbeat(self, plan: SyncPlan | None, note: str | None) -> None:
        assert self.cpu is not None
        state.write_heartbeat(
            plan, runner="daemon", cpu_s_month=self.cpu.outside_jobs(self.wall()), note=note
        )

    def _run(
        self,
        name: str,
        body: Callable[[Session], Any],
        *,
        chain: bool,
        on_success: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        assert self.cpu is not None
        spec = job_spec(name)
        cpu0 = cpu_seconds()
        try:
            out = run_job(
                name,
                body,
                lock_ttl=spec.lock_ttl,
                judge=spec.judge,
                chain=chain,
                on_success=on_success,
            )
        except Exception:  # run_job logged it FAILED; the daemon carries on
            logger.exception("Job %s failed", name)
            self.cpu.jobs += cpu_seconds() - cpu0
            return None
        self.cpu.jobs += float(out.get("cpu_s", cpu_seconds() - cpu0))
        return out

    def _chain(self, first: str, body: Callable[[Session], Any]) -> list[str]:
        """Run ``first`` and, while they succeed, the stages after it (NEXT_STAGE), as
        Celery chains them. Returns the jobs that succeeded."""
        queue: list[tuple[str, Callable[[Session], Any]]] = [(first, body)]
        done: list[str] = []
        while queue and not self.stop.is_set():
            name, fn = queue.pop(0)
            following: list[str] = []
            out = self._run(name, fn, chain=True, on_success=following.append)
            if out is not None and out.get("status") == "success":
                done.append(name)
            queue.extend((n, job_spec(n).run) for n in following)
        return done

    def poll_kicks(self) -> dict[str, list[str]]:
        """Take the API's kicks (Redis) and run them."""
        kicks = state.pop_kicks()
        if not kicks:
            return kicks
        logger.info("Kicks: %s", {k: len(v) for k, v in kicks.items()})
        ran: list[str] = []
        if "matching.match" in kicks:
            ran = self._chain("matching.match", job_spec("matching.match").run)
        ids = kicks.get("flights.discover")
        if ids and not self.stop.is_set():
            match_ids = None if state.ALL_MATCHES in ids else ids
            if match_ids is not None or "flights.discover" not in ran:
                self._chain("flights.discover", discover_flights_run(match_ids))
        release_connections()
        return kicks

    def run_periodic(self) -> list[str]:
        """Run the periodic jobs that are due, back to back."""
        assert self.jobs is not None
        due = self.jobs.due(self.monotonic(), self.wall())
        names: list[str] = []
        for entry in due:
            if self.stop.is_set():
                break
            self._run(entry.task, job_spec(entry.task).run, chain=False)
            names.append(entry.task)
        if names:
            release_connections()
            logger.info("Periodic jobs: %s", ", ".join(names))
        return names


def restore_logging() -> None:
    """Back to the application's logging after a migration: Alembic's ``fileConfig``
    (alembic/env.py) replaces the root handlers, and with them the redaction filter, and
    sets the root level to WARNING."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        if not getattr(handler, "_swell", False):
            root.removeHandler(handler)
    configure_logging()


def release_connections() -> None:
    """Close the pool's idle connections: between two database wakes the daemon holds no
    connection open (a pooled connection kept open could keep a scale-to-zero database
    awake)."""
    try:
        get_engine().dispose()
    except Exception:  # never fails the daemon
        logger.debug("closing idle database connections failed", exc_info=True)


def _install_signals(stop: threading.Event) -> Callable[[], None]:
    """SIGTERM and SIGINT set ``stop`` (main thread only); returns the restore function."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: object) -> None:
        logger.info("Sync daemon: signal %s, stopping after the current stage", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous.items():
            signal.signal(sig, old)

    return restore


def run_daemon(
    *,
    stop: threading.Event | None = None,
    once: bool = False,
    init_db: bool | None = None,
) -> int:
    """Run the daemon until ``stop`` (or one iteration with ``once``); the exit code.

    ``init_db`` None follows SYNC_DAEMON_INIT_DB. A start-up failure (e.g. the database
    still waking) is retried every START_RETRY_SECONDS; with ``once`` it returns 1.
    """
    settings = get_settings()
    stop = stop or threading.Event()
    restore = _install_signals(stop)
    try:
        daemon = SyncDaemon(settings, stop)
        init = settings.sync_daemon_init_db if init_db is None else init_db
        while True:
            try:
                daemon.start(init_db=init)
                break
            except Exception:
                logger.exception("Sync daemon start failed")
                release_connections()
                if once:
                    return 1
                if stop.wait(START_RETRY_SECONDS):
                    return 0
        logger.info(
            "Sync daemon started: checks every %d min, kicks every %d s, %d periodic jobs",
            settings.sync_check_minutes,
            settings.sync_daemon_poll_seconds,
            len(daemon.jobs.timers) + len(daemon.jobs.daily) if daemon.jobs else 0,
        )
        code = 0
        while not stop.is_set():
            try:
                wait = daemon.step()
            except Exception:
                logger.exception("Sync daemon iteration failed")
                release_connections()
                code, wait = 1, ERROR_WAIT_SECONDS
            if once:
                return code
            code = 0
            stop.wait(max(MIN_WAIT_SECONDS, wait))
        logger.info("Sync daemon stopped")
        return 0
    finally:
        restore()
