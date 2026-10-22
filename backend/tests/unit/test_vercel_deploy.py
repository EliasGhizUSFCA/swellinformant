"""The Vercel deployment files keep the layout contract (design F8/F9, D10, D15).

vercel.json: the web and api services, /api routed to the api and everything else to the web,
the sync daemon, and one daily cron on the watchdog route. .vercelignore keeps what the
services and the daemon are built from. .github/workflows/data-sync.yml is manual only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.api.internal import CRON_DAILY_PATH
from app.services.sync.types import SYNC_RUN_JOB

ROOT = Path(__file__).resolve().parents[3]
VERCEL_JSON = ROOT / "vercel.json"
VERCELIGNORE = ROOT / ".vercelignore"
DATA_SYNC = ROOT / ".github" / "workflows" / "data-sync.yml"
CONTAINERFILE = ROOT / "Containerfile"


@pytest.fixture(scope="module")
def vercel() -> dict[str, Any]:
    return dict(json.loads(VERCEL_JSON.read_text()))


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    yaml = pytest.importorskip("yaml")
    return dict(yaml.safe_load(DATA_SYNC.read_text()))


def test_services_and_routing(vercel: dict[str, Any]) -> None:
    assert vercel["$schema"] == "https://openapi.vercel.sh/vercel.json"
    web, api = vercel["services"]["web"], vercel["services"]["api"]
    # The framework is explicit: frontend/ also holds the classic stack's Dockerfile, and
    # Vercel CLI 64.5.0 then stops with MULTIPLE_FRAMEWORKS_SERVICE.
    assert web == {"root": "frontend/", "framework": "nextjs"}
    assert api["root"] == "backend/"
    assert api["framework"] == "fastapi"
    assert api["entrypoint"] == "app.main:app"
    assert (ROOT / "backend" / "app" / "main.py").is_file()
    function = api["functions"]["app/main.py"]
    assert function["maxDuration"] == 60
    for pattern in ("tests/**", "**/*.rdb", "swell_travel_agent.egg-info/**"):
        assert pattern in function["excludeFiles"]
    # Migrations before promotion; MIGRATE_ON_BUILD decides whether it runs (D9).
    assert api["buildCommand"] == "python -m app.cli migrate --on-build"
    # First match wins: /api/* to the api (it receives the original path), the rest to web.
    assert vercel["rewrites"] == [
        {"source": "/api/(.*)", "destination": {"service": "api"}},
        {"source": "/(.*)", "destination": {"service": "web"}},
    ]
    assert CRON_DAILY_PATH.startswith("/api/")


def test_one_daily_cron_on_the_watchdog_route(vercel: dict[str, Any]) -> None:
    crons = vercel["crons"]
    assert [c["path"] for c in crons] == [CRON_DAILY_PATH]
    minute, hour, *rest = crons[0]["schedule"].split()
    # Hobby allows once a day at most; a more frequent expression fails the deployment.
    assert minute.isdigit() and 0 <= int(minute) < 60
    assert hour.isdigit() and 0 <= int(hour) < 24
    assert rest == ["*", "*", "*"]


def test_sync_daemon(vercel: dict[str, Any]) -> None:
    daemon = vercel["daemons"]["sync"]
    # The repository root: the image needs backend/ and data/. Vercel cannot add daemons to
    # a project whose Root Directory is a subfolder.
    assert daemon["root"] == "./"
    assert daemon["entrypoint"] == "Containerfile"
    assert daemon["instances"] == {"iad1": 1}  # one instance: the sync lease allows one sync
    # Hobby offers 1 GiB or 2 GiB instances.
    assert daemon["resources"] == {"requests": {"memory": "2Gi"}}
    assert daemon["terminationGracePeriodSeconds"] == 120
    # No `command`: the image's CMD runs the daemon.
    assert set(daemon) == {
        "root",
        "entrypoint",
        "instances",
        "resources",
        "terminationGracePeriodSeconds",
    }


def test_the_daemon_image_runs_the_sync_daemon() -> None:
    if not CONTAINERFILE.is_file():
        pytest.skip("Containerfile is added by the local-vercel-stack package")
    text = CONTAINERFILE.read_text()
    assert re.search(r'^CMD \["python", "-m", "app\.cli", "sync", "daemon"\]', text, re.M)
    # Vercel passes the deployment's environment variables to the build as build arguments.
    assert not re.search(r"^\s*ARG\s+\w*(SECRET|TOKEN|PASSWORD|KEY|DATABASE_URL)", text, re.M)


def _vercelignore_patterns() -> list[str]:
    lines = (line.strip() for line in VERCELIGNORE.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_vercelignore_keeps_the_build_inputs_and_drops_secrets() -> None:
    patterns = _vercelignore_patterns()
    kept = ("backend", "frontend", "data", "Containerfile", "vercel.json")
    for pattern in patterns:
        bare = pattern.strip("/")
        assert bare not in kept, pattern
        assert bare not in {"*", "**"}, pattern
        assert not any(bare.startswith(f"{name}/") for name in ("frontend", "data")), pattern
    for required in (".env", ".env.*", "node_modules", "backend/.venv", ".claude"):
        assert required in patterns, required


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads a bare `on` key as the boolean True.
    return dict(workflow.get("on") or workflow[True])


def test_data_sync_is_manual_only(workflow: dict[Any, Any]) -> None:
    triggers = _triggers(workflow)
    assert set(triggers) == {"workflow_dispatch", "repository_dispatch"}
    assert triggers["repository_dispatch"] == {"types": ["data-sync"]}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"force", "only", "migrate"}
    assert not re.search(r"^\s*schedule\s*:", DATA_SYNC.read_text(), re.M)
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "data-sync", "cancel-in-progress": False}


def test_data_sync_runs_the_sync_cli(workflow: dict[Any, Any]) -> None:
    steps = workflow["jobs"]["sync"]["steps"]
    for step in steps:
        if "uses" in step:  # actions pinned by commit SHA, like ci.yml
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    scripts = [step["run"] for step in steps if "run" in step]
    sync = next(s for s in scripts if "app.cli sync run" in s)
    assert 'python -m app.cli sync run "${args[@]}"' in sync
    assert "args=(--trigger github)" in sync
    assert SYNC_RUN_JOB == "sync.run"
    for script in scripts:
        # Inputs and dispatch payloads reach scripts only through env, never pasted in.
        assert "${{" not in script


def test_data_sync_reads_the_documented_secrets(workflow: dict[Any, Any]) -> None:
    steps = workflow["jobs"]["sync"]["steps"]
    env = next(step["env"] for step in steps if "app.cli sync run" in step.get("run", ""))
    assert env["DATABASE_URL_UNPOOLED"] == "${{ secrets.DATABASE_URL_UNPOOLED }}"
    assert env["DATABASE_URL"] == env["DATABASE_URL_UNPOOLED"]
    assert env["REDIS_URL"] == "${{ secrets.REDIS_URL || secrets.KV_URL }}"
    assert env["SECRET_KEY"] == "${{ secrets.SECRET_KEY }}"
    assert "OPEN_METEO_API_KEY" in env
