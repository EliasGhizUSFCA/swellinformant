"""Data health as the hosted deployment runs it (Upgrade 4 integration).

* The tide model's constants file is deployed with the job runners, not with the Vercel API
  function: there the report takes the sync daemon's own check from its heartbeat, so
  /api/health/data and the daily watchdog never report a file that is simply not deployed,
  and both runs of the alert job see the same problems.
* Map freshness judges the run the map serves (the newest publication's under
  SYNC_PUBLISH_GATE), not the newest stored one.
* The expected jobs follow the configured runner's schedule, and the messages name it.

Forecast runs, map fields, sync rows and heartbeats are SYNTHETIC test rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.jobs.schedule import Every, JobEntry
from app.models.enums import JobStatus
from app.models.sync import SyncRun
from app.services.health import data_health, expected_jobs
from app.services.map_fields import field_index
from app.services.sync import state as sync_state
from app.services.sync.daemon import SyncDaemon, file_problems, periodic_entries
from app.services.sync.types import STAGE_JOB_NAMES, SYNC_OWNED_TASKS
from app.services.tides.health import CONSTANTS_KEY, constants_file_present
from tests.integration.test_data_health import heartbeat, job_log, map_fields, run

pytestmark = pytest.mark.integration

MISSING = "/nonexistent-glue-test/constants_eot20.json"  # an absolute path that never exists


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    get_settings.cache_clear()


def set_env(mp: pytest.MonkeyPatch, **values: str) -> None:
    for k, v in values.items():
        mp.setenv(k, v)
    get_settings.cache_clear()


def problems(db: Session) -> dict[str, str]:
    return {p.key: p.severity for p in data_health(db).problems}


# --------------------------------------------------------------------------- tide constants
def test_the_daemon_reports_its_constants_check_in_the_heartbeat(
    db: Session, env: pytest.MonkeyPatch
) -> None:
    set_env(env, JOB_RUNNER="daemon")
    assert constants_file_present() and file_problems() == []  # data/ of the repository
    set_env(env, TIDE_CONSTANTS_PATH=MISSING)
    found = file_problems()
    assert [(p["key"], p["severity"]) for p in found] == [(CONSTANTS_KEY, "failing")]
    assert MISSING in found[0]["detail"]  # the path goes to the admin email only

    daemon = SyncDaemon(get_settings())
    daemon.start(init_db=False)
    daemon._heartbeat(None, "synthetic check")  # what every check writes
    beat = sync_state.read_heartbeat()
    assert beat is not None and beat.file_problems is not None
    assert [p["key"] for p in beat.file_problems] == [CONSTANTS_KEY]


def test_without_the_file_the_api_reports_the_daemons_check_not_its_own(
    db: Session, env: pytest.MonkeyPatch
) -> None:
    """The API function under JOB_RUNNER=daemon (no data/): the constants problem comes
    from the daemon's heartbeat, the same key and severity as the daemon's own report."""
    set_env(env, JOB_RUNNER="daemon")
    run(db, "demo", 1)
    heartbeat(db)
    sync_state.write_heartbeat(None, runner="daemon", file_problems=[])
    set_env(env, TIDE_CONSTANTS_PATH=MISSING)  # this process has no constants file
    assert not constants_file_present()
    assert CONSTANTS_KEY not in problems(db)  # the daemon read its file fine

    broken = {
        "key": CONSTANTS_KEY,
        "severity": "failing",
        "message": "The tide model's constants file cannot be read; model tides are unavailable.",
        "detail": "synthetic: the daemon's file is unreadable",
    }
    sync_state.write_heartbeat(None, runner="daemon", file_problems=[broken])
    report = data_health(db)
    found = next(p for p in report.problems if p.key == CONSTANTS_KEY)
    assert found.severity == "failing" and found.detail == broken["detail"]
    assert "synthetic: the daemon" not in report.out.model_dump_json()  # detail stays private

    # An older daemon that does not report its files, or no heartbeat: nothing is claimed.
    sync_state.write_heartbeat(None, runner="daemon")
    assert CONSTANTS_KEY not in problems(db)


def test_where_the_file_is_deployed_it_is_checked_directly(
    db: Session, env: pytest.MonkeyPatch
) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    # Classic compose (JOB_RUNNER=celery): every process ships data/; a missing file fails.
    set_env(env, TIDE_CONSTANTS_PATH=MISSING)
    assert problems(db).get(CONSTANTS_KEY) == "failing"
    # The daemon's own report (and any process with the file) reads it, whatever the
    # heartbeat says.
    set_env(env, JOB_RUNNER="daemon", TIDE_CONSTANTS_PATH="data/tides/constants_eot20.json")
    sync_state.write_heartbeat(
        None,
        runner="daemon",
        file_problems=[{"key": CONSTANTS_KEY, "severity": "failing", "message": "stale"}],
    )
    assert CONSTANTS_KEY not in problems(db)


# --------------------------------------------------------------------------- map freshness
def test_map_freshness_judges_the_published_run_the_map_serves(
    db: Session, env: pytest.MonkeyPatch
) -> None:
    set_env(env, MAP_FIELDS_ENABLED="true")
    run(db, "demo", 1)
    heartbeat(db)
    now = utcnow()
    cycle = (now - timedelta(hours=26)).replace(minute=0, second=0, microsecond=0)
    published_run = run(db, "noaa_gfs_fields", 24, kind="fields")
    map_fields(db, published_run, cycle)
    newer = run(db, "noaa_gfs_fields", 2, kind="fields")
    map_fields(db, newer, cycle + timedelta(hours=24))
    db.add(
        SyncRun(
            trigger="daemon",
            status="success",
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4),
            plan={},
            stages=[],
            published_at=now - timedelta(minutes=4),
            map_run_id=published_run.id,
        )
    )
    db.commit()

    out = data_health(db, now=now).out.map_fields
    index = field_index(db, now)
    # The map serves the published run, 26 h old (stale after 21 h): health says so too,
    # where the newest stored run (2 h) would have hidden it.
    assert out.run_key == published_run.run_key and out.cycle_time == cycle == index.cycle_time
    assert out.status == "stale" and index.stale is True
    assert "map_fields_stale" in {p.key for p in data_health(db, now=now).problems}

    set_env(env, SYNC_PUBLISH_GATE="false")  # without the gate both serve the newest
    out = data_health(db, now=now).out.map_fields
    assert out.run_key == newer.run_key and out.status == "ok"


# --------------------------------------------------------------------------- jobs
def test_expected_jobs_follow_the_daemons_timers(env: pytest.MonkeyPatch) -> None:
    set_env(env, JOB_RUNNER="daemon", TIDE_PROVIDERS="noaa_coops")
    settings = get_settings()
    scheduled = {job.name for job in expected_jobs(settings) if job.max_gap is not None}
    assert scheduled == {entry.task for entry in periodic_entries(settings)}
    assert not scheduled & SYNC_OWNED_TASKS
    staged = {job.name for job in expected_jobs(settings) if job.max_gap is None}
    assert staged <= set(STAGE_JOB_NAMES.values())
    for name in ("quality.predict", "detection.detect", "forecasts.acquire"):
        job = next(j for j in expected_jobs(settings) if j.name == name)
        assert job.max_gap is None  # a sync stage, never overdue

    set_env(env, JOB_RUNNER="celery")
    assert "sync.tick" in {job.name for job in expected_jobs(get_settings())}


def test_with_an_older_schedule_health_still_expects_only_what_the_daemon_runs(
    env: pytest.MonkeyPatch,
) -> None:
    """A schedule that still lists a sync-owned job (the pre-D14 quality.predict timer) or a
    task without a job spec: the daemon runs neither on a timer, so neither is overdue
    ('Job quality.predict has not run for 2.0 h' was reported under the daemon)."""
    from app.jobs import schedule as schedule_module
    from app.services import health as health_module
    from app.services.sync import daemon as daemon_module

    real = schedule_module.build_schedule

    def older(settings: Settings) -> dict[str, JobEntry]:
        extra = [
            JobEntry("quality-prediction-safety-net", "quality.predict", Every(30)),
            JobEntry("synthetic-unknown", "synthetic.unknown_task", Every(60)),
        ]
        return real(settings) | {e.name: e for e in extra}

    env.setattr(health_module, "build_schedule", older)
    env.setattr(daemon_module, "build_schedule", older)
    set_env(env, JOB_RUNNER="daemon")
    settings = get_settings()
    scheduled = {job.name for job in expected_jobs(settings) if job.max_gap is not None}
    assert scheduled == {entry.task for entry in periodic_entries(settings)}
    assert not {"quality.predict", "synthetic.unknown_task"} & scheduled
    set_env(env, JOB_RUNNER="celery")  # beat runs every entry
    names = {job.name for job in expected_jobs(get_settings()) if job.max_gap is not None}
    assert {"quality.predict", "synthetic.unknown_task", "sync.tick"} <= names


def test_an_overdue_job_names_the_runner_that_runs_it(db: Session, env: pytest.MonkeyPatch) -> None:
    run(db, "demo", 1)
    heartbeat(db)
    job_log(db, "alerts.generate", JobStatus.SUCCESS, 60 * 5)
    celery = next(p for p in data_health(db).problems if p.key == "job_overdue:alerts.generate")
    assert "beat" in celery.message and "worker" in celery.message

    set_env(env, JOB_RUNNER="daemon")
    sync_state.write_heartbeat(None, runner="daemon", file_problems=[])
    report = data_health(db)
    daemon = next(p for p in report.problems if p.key == "job_overdue:alerts.generate")
    assert "sync daemon" in daemon.message
    assert not any("beat" in p.message or "worker" in p.message for p in report.problems)
