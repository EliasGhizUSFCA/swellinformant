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
    DATA_HEALTH_TASK,
    DailyAt,
    Every,
    JobEntry,
    build_schedule,
    describe_schedule,
    max_interval,
)
from app.workers import schedule as beat
from app.workers import tasks as _tasks  # noqa: F401  (registers the Celery tasks)
from app.workers.celery_app import celery_app

BACKEND = Path(__file__).resolve().parents[2]
HEAVY = ("pandas", "eccodes", "gribapi", "pyarrow", "sklearn", "scipy", "joblib")
CELERY = ("celery", "kombu")
# Celery tasks that are not background jobs (they deliver one message each).
MESSAGING_TASKS = {
    "notifications.send_account_email",
    "notifications.send_email",
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


# --------------------------------------------------------------------------- schedule
def test_the_schedule_has_today_s_entries() -> None:
    plain = build_schedule(every_job_settings())
    assert all(isinstance(e, JobEntry) and e.name == name for name, e in plain.items())
    assert {name: e.task for name, e in plain.items()} == {
        "forecast-acquisition": "forecasts.acquire",
        "quality-prediction-safety-net": "quality.predict",
        "swell-detection": "detection.detect",
        "user-matching": "matching.match",
        "flight-discovery": "flights.discover",
        "alert-generation": "alerts.generate",
        "notification-delivery": "notifications.deliver",
        "forecast-staleness-check": "forecasts.check_staleness",
        "data-health": DATA_HEALTH_TASK,
        "maintenance-cleanup": "maintenance.cleanup",
        "map-fields": "forecasts.map_fields",
        "ensemble-acquisition": "forecasts.acquire_ensemble",
        "tide-acquisition": "tides.acquire",
        "observation-ingest": "observations.ingest",
        "point-forecast-archive": "verification.collect_point_forecasts",
        "verification": "verification.run",
    }
    assert plain["maintenance-cleanup"].schedule == DailyAt((3,), 30)
    assert plain["ensemble-acquisition"].schedule == DailyAt((1, 7, 13, 19), 40)
    assert plain["notification-delivery"].schedule == Every(2)
    assert plain["forecast-acquisition"].schedule == Every(180)


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
    assert set(registry.JOB_SPECS) - jobs == {"sync.tick", "sync.publish", "maintenance.prune_pass"}
    for name, spec in registry.JOB_SPECS.items():
        assert spec.name == name and registry.job_spec(name) is spec
    with pytest.raises(KeyError, match="unknown job"):
        registry.job_spec("forecasts.nope")


def test_job_specs_keep_the_task_options() -> None:
    specs = registry.JOB_SPECS
    assert specs["forecasts.acquire"].lock_ttl == 3600
    assert specs["forecasts.acquire"].judge is registry.judge_acquisition
    assert specs["forecasts.map_fields"].judge is registry.judge_single
    assert specs["forecasts.map_fields"].chain is False
    assert specs["forecasts.acquire_ensemble"].judge is registry.judge_single
    assert specs["notifications.deliver"].lock_ttl == 600
    assert specs[DATA_HEALTH_TASK].lock_ttl == 600 and specs[DATA_HEALTH_TASK].chain is False
    assert specs["flights.discover"].lock_ttl == 3600
    assert specs["sync.tick"].lock_ttl == 3600 and specs["sync.tick"].chain is False
    for name in ("maintenance.cleanup", "forecasts.check_staleness", "sync.publish"):
        assert specs[name].chain is False, name
    for name in ("forecasts.blend", "quality.predict", "detection.detect", "matching.match"):
        assert specs[name].chain is True and specs[name].lock_ttl == 1800, name


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
