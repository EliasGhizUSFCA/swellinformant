"""Cross-package interfaces and contracts declared by the foundation (F7, F8).

The signatures are frozen: the packages replace the stub bodies, never the parameters. The
stubs say plainly that they are not implemented; the interfaces that keep today's
behaviour (acquire_all without ``only``, dispatch.kick under Celery) do exactly that.
"""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.core.config import get_settings
from app.services.sync import cli as sync_cli
from app.services.sync import daemon, detect, orchestrator, publish
from app.services.sync.types import SyncBusy
from app.workers import dispatch

EMPTY = inspect.Parameter.empty
KW = inspect.Parameter.KEYWORD_ONLY
POS = inspect.Parameter.POSITIONAL_OR_KEYWORD


def params(fn: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


# --------------------------------------------------------------------------- F7 signatures
def test_sync_core_signatures() -> None:
    assert params(detect.load_known) == [("db", POS, EMPTY)]
    assert params(detect.plan_sync) == [
        ("db", POS, EMPTY),
        ("settings", KW, None),
        ("now", KW, None),
        ("known", KW, None),
        ("force", KW, False),
    ]
    assert params(orchestrator.run_sync) == [
        ("trigger", KW, EMPTY),
        ("force", KW, False),
        ("only", KW, None),
        ("now", KW, None),
        ("stop", KW, None),
        ("plan", KW, None),
    ]
    assert params(orchestrator.tick) == [("trigger", KW, EMPTY)]
    assert orchestrator.SyncBusy is SyncBusy
    assert params(publish.publish) == [
        ("db", POS, EMPTY),
        ("sync_run", KW, None),
        ("trigger", KW, "pipeline"),
        ("now", KW, None),
    ]
    assert params(daemon.run_daemon) == [
        ("stop", KW, None),
        ("once", KW, False),
        ("init_db", KW, None),
    ]


def test_other_package_signatures() -> None:
    from app.services.forecasts.ingestion import acquire_all
    from app.services.health import data_health
    from app.services.maintenance import prune_pass
    from app.services.notifications.ops_alerts import run_data_health_check

    assert params(acquire_all) == [
        ("db", POS, EMPTY),
        ("now", KW, None),
        ("force", KW, False),
        ("only", KW, None),
    ]
    assert params(prune_pass) == [("db", POS, EMPTY), ("now", POS, None)]
    assert params(run_data_health_check) == [("db", POS, EMPTY), ("now", POS, None)]
    assert callable(data_health)
    assert params(dispatch.kick) == [("job", POS, EMPTY), ("match_ids", KW, ())]


@pytest.mark.parametrize(
    "call",
    [
        lambda: detect.load_known(None),  # type: ignore[arg-type]
        lambda: detect.plan_sync(None),
        lambda: orchestrator.run_sync(trigger="cli", stop=threading.Event()),
        lambda: orchestrator.tick(trigger="celery"),
        lambda: publish.publish(None),  # type: ignore[arg-type]
        lambda: daemon.run_daemon(once=True),
    ],
)
def test_sync_core_stubs_say_they_are_not_implemented(call: Callable[[], object]) -> None:
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        call()


# --------------------------------------------------------------------------- acquire_all
def test_acquire_all_only_limits_the_providers_and_keeps_priorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.forecasts import ingestion

    settings = get_settings()
    monkeypatch.setattr(settings, "forecast_providers", "open_meteo,open_meteo_ecmwf,noaa_gfs")
    built: list[str] = []
    acquired: list[tuple[str, int]] = []

    def build(name: str, _settings: object, overrides: object = None) -> str:
        built.append(name)
        return name

    def acquire(_db: object, provider: str, **kw: Any) -> str:
        acquired.append((provider, kw["priority"]))
        return provider

    monkeypatch.setattr(ingestion, "build_provider", build)
    monkeypatch.setattr(ingestion, "acquire_from_provider", acquire)
    every = ingestion.acquire_all(None)  # type: ignore[arg-type]
    assert every == ["open_meteo", "open_meteo_ecmwf", "noaa_gfs"]  # type: ignore[comparison-overlap]
    assert acquired == [("open_meteo", 0), ("open_meteo_ecmwf", 1), ("noaa_gfs", 2)]
    built.clear()
    acquired.clear()
    some = ingestion.acquire_all(None, only={"noaa_gfs", "open_meteo"})  # type: ignore[arg-type]
    assert some == ["open_meteo", "noaa_gfs"]  # type: ignore[comparison-overlap]
    assert built == ["open_meteo", "noaa_gfs"]
    assert acquired == [("open_meteo", 0), ("noaa_gfs", 2)]
    assert ingestion.acquire_all(None, only=()) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- dispatch.kick
class FakeSignature:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]], name: str, kwargs: Any) -> None:
        self.calls, self.name, self.kwargs = calls, name, kwargs

    def delay(self) -> None:
        self.calls.append((self.name, self.kwargs))


def test_kick_under_celery_queues_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers.celery_app import celery_app

    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        celery_app, "signature", lambda name, kwargs=None: FakeSignature(calls, name, kwargs)
    )
    assert get_settings().job_runner == "celery"
    dispatch.kick("matching.match")
    dispatch.kick("flights.discover", match_ids=["m1"])
    dispatch.kick("flights.discover")
    assert calls == [
        ("matching.match", {}),
        ("flights.discover", {"match_ids": ["m1"]}),
        ("flights.discover", {}),
    ]


def test_kick_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers.celery_app import celery_app

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(celery_app, "signature", broken)
    dispatch.kick("matching.match")  # logged, not raised


def test_kick_under_the_daemon_writes_a_redis_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.sync import state

    calls: list[tuple[str, list[str]]] = []

    def request(job: str, match_ids: Any = ()) -> bool:
        calls.append((job, list(match_ids)))
        return False  # Redis down: logged, not raised

    monkeypatch.setattr(get_settings(), "job_runner", "daemon")
    monkeypatch.setattr(state, "request_kick", request)
    dispatch.kick("flights.discover", match_ids=("m1", "m2"))
    dispatch.kick("matching.match")
    assert calls == [("flights.discover", ["m1", "m2"]), ("matching.match", [])]


# --------------------------------------------------------------------------- F8 CLI
@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["sync", "check", "--json"], "sync check"),
        (
            ["sync", "run", "--force", "--only", "map,spots", "--if-due", "--trigger", "github"],
            "sync run",
        ),
        (["sync", "daemon", "--once", "--no-init-db"], "sync daemon"),
        (["sync", "publish"], "sync publish"),
        (["sync", "status", "--limit", "5"], "sync status"),
    ],
)
def test_sync_commands_are_registered_stubs(
    argv: list[str], command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == sync_cli.EXIT_NOT_IMPLEMENTED == 4
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "not_implemented" and out["command"] == command
    assert "not implemented yet" in out["detail"]


def test_sync_options_are_validated() -> None:
    assert sync_cli.component_kinds("map, spots") == ["spots", "map"]
    with pytest.raises(SystemExit):
        cli.main(["sync", "run", "--only", "spots,gefs"])
    with pytest.raises(SystemExit):
        cli.main(["sync", "run", "--trigger", "daemon"])
    with pytest.raises(SystemExit):
        cli.main(["sync"])


def test_migrate_on_build_is_a_stub_and_migrates_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def must_not_run() -> None:
        raise AssertionError("migrate --on-build must not migrate yet")

    monkeypatch.setattr(cli, "migrate", must_not_run)
    assert cli.main(["migrate", "--on-build"]) == cli.EXIT_NOT_IMPLEMENTED
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "status": "not_implemented",
        "command": "migrate --on-build",
        "detail": "not implemented yet: nothing was migrated",
    }


# --------------------------------------------------------------------------- F8 HTTP
@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_cron_route_is_absent_without_a_secret(client: TestClient) -> None:
    assert get_settings().cron_secret is None
    unknown = client.get("/api/internal/no-such-route")
    for headers in ({}, {"Authorization": "Bearer anything-at-all-123"}):
        r = client.get("/api/internal/cron/daily", headers=headers)
        assert r.status_code == 404 and r.json() == unknown.json()
    assert "/api/internal/cron/daily" not in client.get("/openapi.json").json()["paths"]


def test_cron_route_checks_the_bearer_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s3cret-token-for-the-watchdog"
    monkeypatch.setattr(get_settings(), "cron_secret", secret)
    for value in (None, secret, f"Basic {secret}", "Bearer wrong-token-1234567", "Bearer "):
        headers = {"Authorization": value} if value is not None else {}
        assert client.get("/api/internal/cron/daily", headers=headers).status_code == 401, value
    ok = client.get("/api/internal/cron/daily", headers={"Authorization": f"Bearer {secret}"})
    # Authorised, but the watchdog is not built yet: an explicit unavailable state.
    assert ok.status_code == 503
    assert ok.headers["cache-control"] == "private, no-store"
    body = ok.json()
    assert body["status"] == "unavailable" and body["problems"] == []
    assert body["alerts_sent"] == [] and body["heartbeat_age_minutes"] is None
