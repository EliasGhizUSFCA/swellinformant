"""Celery-free job core (foundation F5): app.jobs.schedule / runner / registry, the Celery
adapters built on them, and the lean API import path."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest
from celery.schedules import crontab

from app.core.config import Settings
from app.jobs import registry
from app.jobs.schedule import (
    DailyAt,
    Every,
    build_schedule,
    describe_schedule,
    max_interval,
)
from app.services.sync.types import STAGE_JOB_NAMES
from app.workers import schedule as beat
from app.workers import tasks as _tasks  # noqa: F401  (registers the Celery tasks)
from app.workers.celery_app import celery_app

BACKEND = Path(__file__).resolve().parents[2]
HEAVY = ("pandas", "eccodes", "gribapi", "pyarrow", "sklearn", "scipy", "joblib")
CELERY = ("celery", "kombu")
# Celery tasks that are not background jobs (they deliver one message each).
MESSAGING_TASKS = {
    "notifications.send_account_email",
    "notifications.send_sms",
}


def every_job_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        gefs_enabled=True,
        observations_enabled=True,
        verification_enabled=True,
        map_fields_enabled=True,
        tide_providers="noaa_coops",
    )


def import_blocked(
    modules: list[str], blocked: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    """Import ``modules`` in a fresh interpreter in which importing ``blocked`` fails."""
    code = textwrap.dedent(
        f"""
        import sys

        BLOCKED = {blocked!r}

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(f"blocked import of {{name}}")
                return None

        sys.meta_path.insert(0, Blocker())
        import importlib
        for module in {modules!r}:
            importlib.import_module(module)
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
        assert not loaded, loaded
        print("ok")
        """
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120
    )


def test_the_api_imports_without_celery_and_the_heavy_libraries() -> None:
    api_modules = sorted(
        f"app.api.{p.stem}" for p in (BACKEND / "app" / "api").glob("*.py") if p.stem != "__init__"
    )
    done = import_blocked(["app.main", *api_modules], CELERY + HEAVY)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-3000:]


def test_the_job_core_and_sync_contracts_import_without_celery() -> None:
    done = import_blocked(
        [
            "app.jobs.schedule",
            "app.jobs.runner",
            "app.jobs.registry",
            "app.services.sync.types",
            "app.services.sync.state",
            "app.services.sync.published",
            "app.services.health",
            "app.services.notifications.ops_alerts",
            "app.workers.dispatch",
            "app.core.http_cache",
        ],
        CELERY,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-3000:]


def test_the_cli_runs_without_the_optional_extras() -> None:
    """A lean install (the hosted API: no worker or sync extra) still runs migrate,
    init-db, seed and check-config: groups that need a missing extra are left out."""
    code = textwrap.dedent(
        f"""
        import sys

        BLOCKED = {CELERY + HEAVY!r}

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
                return None

        sys.meta_path.insert(0, Blocker())
        from app import cli

        try:
            cli.main(["--help"])
        except SystemExit as exc:
            assert exc.code == 0, exc.code
        """
    )
    done = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-3000:]
    for command in ("migrate", "init-db", "seed", "check-config"):
        assert command in done.stdout, command


# --------------------------------------------------------------------------- schedule
@pytest.mark.parametrize(
    "settings",
    [Settings(_env_file=None), every_job_settings()],  # type: ignore[call-arg]
)
def test_the_beat_adapter_matches_the_plain_schedule(settings: Settings) -> None:
    plain = build_schedule(settings)
    celery_shaped = beat.build_beat_schedule(settings)
    assert list(celery_shaped) == list(plain)
    for name, entry in plain.items():
        converted = celery_shaped[name]
        assert converted["task"] == entry.task
        assert converted["schedule"] == beat.to_celery(entry.schedule)
        assert beat.max_interval(converted["schedule"]) == max_interval(entry.schedule)
        assert beat.describe_schedule(converted["schedule"]) == describe_schedule(entry.schedule)


@pytest.mark.parametrize(
    ("sched", "gap", "text", "celery"),
    [
        (Every(2), timedelta(minutes=2), "every 2 min", timedelta(minutes=2)),
        (Every(180), timedelta(hours=3), "every 3 h", timedelta(hours=3)),
        (Every(60), timedelta(hours=1), "every hour", timedelta(hours=1)),
        (DailyAt((3,), 30), timedelta(hours=24), "daily at 03:30 UTC", crontab(hour=3, minute=30)),
        (
            DailyAt((1, 7, 13, 19), 40),
            timedelta(hours=6),
            "daily at 01:40, 07:40, 13:40, 19:40 UTC",
            crontab(hour="1,7,13,19", minute=40),
        ),
        (
            DailyAt((0, 6), 0),
            timedelta(hours=18),
            "daily at 00:00, 06:00 UTC",
            crontab(hour="0,6", minute=0),
        ),
    ],
)
def test_plain_schedules(sched: Every | DailyAt, gap: timedelta, text: str, celery: object) -> None:
    assert max_interval(sched) == gap
    assert describe_schedule(sched) == text
    assert beat.to_celery(sched) == celery
    assert beat.max_interval(sched) == gap and beat.describe_schedule(sched) == text


# --------------------------------------------------------------------------- registry
def test_every_celery_job_has_a_registry_spec() -> None:
    jobs = {name for name in celery_app.tasks if not name.startswith("celery.")}
    jobs -= MESSAGING_TASKS
    assert jobs <= set(registry.JOB_SPECS), jobs - set(registry.JOB_SPECS)
    # The synced pipeline's jobs (F5c), whether or not Celery has a task for them yet.
    assert {"sync.tick", "sync.publish", "maintenance.prune_pass"} <= set(registry.JOB_SPECS)
    for name, spec in registry.JOB_SPECS.items():
        assert spec.name == name and registry.job_spec(name) is spec
    with pytest.raises(KeyError, match="unknown job"):
        registry.job_spec("forecasts.nope")


def test_every_sync_stage_and_scheduled_job_has_a_registry_spec() -> None:
    """The orchestrator runs JOB_SPECS[STAGE_JOB_NAMES[stage]] and the daemon runs
    JOB_SPECS[entry.task] for every schedule entry (contracts F3, F5, F7)."""
    assert set(STAGE_JOB_NAMES.values()) <= set(registry.JOB_SPECS)
    for settings in (Settings(_env_file=None), every_job_settings()):  # type: ignore[call-arg]
        tasks = {entry.task for entry in build_schedule(settings).values()}
        assert tasks <= set(registry.JOB_SPECS), tasks - set(registry.JOB_SPECS)


def test_registry_bodies_import_their_service_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    class Result:
        def as_dict(self) -> dict[str, object]:
            return {"ok": True}

    def fake(db: object, match_ids: list[str] | None = None) -> Result:
        calls.append(match_ids)
        return Result()

    monkeypatch.setattr("app.services.flights.service.discover_flights", fake)
    assert registry.discover_flights_run(("m1",))(None) == {"ok": True}  # type: ignore[arg-type]
    assert registry.JOB_SPECS["flights.discover"].run(None) == {"ok": True}  # type: ignore[arg-type]
    assert calls == [["m1"], None]
