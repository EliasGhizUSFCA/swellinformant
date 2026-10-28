"""The Vercel deployment files keep the layout contract (design D10, D15).

vercel.json: the web and api services, /api routed to the api and everything else to the web,
the sync daemon, and one daily cron on the watchdog route (which also runs a refresh check;
the frequent refresh checks come from an external scheduler on Hobby, docs/sync.md).
.vercelignore keeps what the services and the daemon are built from.
.github/workflows/data-sync.yml is manual only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.api.internal import CRON_DAILY_PATH, REFRESH_PATH
from app.core.config import Settings
from app.core.profiles import profile_env
from app.services.sync.types import SYNC_RUN_JOB

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
VERCEL_JSON = ROOT / "vercel.json"
VERCELIGNORE = ROOT / ".vercelignore"
DATA_SYNC = ROOT / ".github" / "workflows" / "data-sync.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CONTAINERFILE = ROOT / "Containerfile"
DEPLOY_DOC = ROOT / "docs" / "deploy-vercel.md"
# What the Configuration step writes for every run, whatever the repository variables say.
FIXED_RUN_ENV = {
    "APP_ENV": "production",
    "COOKIE_SECURE": "true",
    "ENABLE_DEV_ENDPOINTS": "false",
    "JOB_RUNNER": "daemon",
    "DB_USE_UNPOOLED": "true",
}


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
    # 300 s, the Hobby maximum: a spot-only refresh (/api/internal/refresh) runs inside the
    # request (Swell 2.0, design D8; measured in docs/sync.md).
    assert function["maxDuration"] == 300
    # Nothing the function never reads: tests, a local setuptools build/ and the ML models
    # (the lean install has no joblib to load them).
    excluded = function["excludeFiles"].strip("{}").split(",")
    for pattern in (
        "tests/**",
        "**/*.rdb",
        "swell_travel_agent.egg-info/**",
        "build/**",
        "ml_models/**",
    ):
        assert pattern in excluded, pattern
    assert not {"app/**", "alembic/**", "alembic.ini", "pyproject.toml"} & set(excluded)
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
    # No sub-daily refresh cron: Hobby refuses the deployment (an external scheduler calls
    # /api/internal/refresh instead; on Pro a */5 entry is documented in docs/sync.md).
    assert REFRESH_PATH not in [c["path"] for c in crons]
    for cron in crons:
        minute, hour, *rest = cron["schedule"].split()
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
    # SIGTERM stops the daemon after its current sync stage; a full map stage is estimated at
    # 165-287 CPU s on one vCPU (docs/sync.md), so it gets 600 s (the schema allows 3600).
    assert daemon["terminationGracePeriodSeconds"] == 600
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


def test_the_deploy_doc_lists_the_hosted_profile() -> None:
    # docs/deploy-vercel.md tells the owner what to paste into Vercel: it must stay the profile.
    text = DEPLOY_DOC.read_text()
    block = text.split("**The hosted profile.**", 1)[1].split("```env\n", 1)[1].split("```", 1)[0]
    pairs = dict(line.split("=", 1) for line in block.splitlines() if line.strip())
    assert pairs == profile_env("hosted", "free-database")


def _step(workflow: dict[Any, Any], marker: str) -> dict[str, Any]:
    steps = workflow["jobs"]["sync"]["steps"]
    return dict(next(s for s in steps if marker in s.get("name", "") + s.get("run", "")))


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    return bash


def _github_env(text: str) -> dict[str, str]:
    """The NAME<<DELIMITER ... DELIMITER entries a step appended to $GITHUB_ENV."""
    found: dict[str, str] = {}
    for name, _delimiter, value in re.findall(r"^(\w+)<<(\S+)\n(.*?)\n\2$", text, re.M | re.S):
        found[name] = value
    return found


def _run_configuration(
    workflow: dict[Any, Any], tmp_path: Path, variables: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    # A wrapper, not a symlink: a symlinked venv interpreter would lose its venv.
    python = bindir / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "GITHUB_ENV": str(github_env),
        # As the step's env passes them (VARIABLE_<name>: ${{ vars.<name> }}); every name is
        # given here, also those the workflow does not list, to test the script's own filter.
        **{f"VARIABLE_{name}": value for name, value in variables.items()},
    }
    script = _step(workflow, "Configuration")["run"]
    result = subprocess.run(  # noqa: S603 - a fixed script from the repository
        [_bash(), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result, _github_env(github_env.read_text())


REQUIRED_VARIABLES = {
    "APP_BASE_URL": "https://swell-review.example.test",
    "EMAIL_PROVIDER": "resend",
    "EMAIL_FROM": "Swell <alerts@swell-review.example.test>",
}


def test_data_sync_configuration_applies_the_profile_then_variables(
    workflow: dict[Any, Any], tmp_path: Path
) -> None:
    variables = {
        **REQUIRED_VARIABLES,
        "GEFS_ENABLED": "true",
        # Never taken from variables: fixed for a production run, credentials, not settings.
        "APP_ENV": "development",
        "SECRET_KEY": "variable-value-1",
        "WINDY_API_KEY": "variable-value-2",
        "DATABASE_URL": "variable-value-3",
        "AMADEUS_CLIENT_ID": "variable-value-4",
        "PATH": "/variable-value-5",
        "NOT_A_SETTING": "variable-value-6",
    }
    result, written = _run_configuration(workflow, tmp_path, variables)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = {
        **profile_env("hosted", "free-database"),
        **REQUIRED_VARIABLES,
        "GEFS_ENABLED": "true",
        **FIXED_RUN_ENV,
    }
    assert written == expected
    ignored = ["AMADEUS_CLIENT_ID", "APP_ENV", "DATABASE_URL", "NOT_A_SETTING", "PATH"]
    ignored += ["SECRET_KEY", "WINDY_API_KEY"]
    warning = "::warning::repository variables left out (not app settings, secrets or fixed): "
    assert warning + ", ".join(ignored) in result.stdout
    # A public repository's logs are public: no value of a repository variable is printed.
    for value in ("swell-review.example.test", "variable-value"):
        assert value not in result.stdout + result.stderr
    assert "EMAIL_FROM=(set)  [repository variable]" in result.stdout
    assert "APP_ENV=production  [fixed]" in result.stdout


# Settings whose values are private (an address, an account name, a phone number): secrets,
# never variables, because GitHub prints a step's environment with the values in the log.
PRIVATE_SETTINGS = {"SMTP_USERNAME", "OPS_ALERT_EMAILS", "TWILIO_FROM_NUMBER", "CRON_SECRET"}


def test_data_sync_reads_only_listed_public_variables(workflow: dict[Any, Any]) -> None:
    """toJSON(vars) would put every repository variable, a credential saved there by
    mistake included, into the step's environment, which the runner prints in the public
    log. Only named public settings are read, each by its own name."""
    text = DATA_SYNC.read_text()
    assert not re.search(r"\$\{\{[^}]*toJSON\((vars|secrets)\)", text)
    env = _step(workflow, "Configuration")["env"]
    names = set()
    for key, value in env.items():
        assert key.startswith("VARIABLE_"), key
        name = key.removeprefix("VARIABLE_")
        assert value == f"${{{{ vars.{name} }}}}", key
        names.add(name)
    assert {"APP_BASE_URL", "EMAIL_PROVIDER", "EMAIL_FROM", "GEFS_ENABLED"} <= names
    settings = {field.upper() for field in Settings.model_fields}
    assert names <= settings
    assert not names & PRIVATE_SETTINGS
    assert not [n for n in names if n.endswith(("_KEY", "_SECRET", "_TOKEN", "_PASSWORD"))]
    assert not [n for n in names if "URL" in n and n != "APP_BASE_URL"]
    # vars.* is read in that step's env only.
    assert len(re.findall(r"\$\{\{\s*vars\.", text)) == len(env)


def test_data_sync_takes_private_values_from_secrets(
    workflow: dict[Any, Any], tmp_path: Path
) -> None:
    sync_env = _step(workflow, "app.cli sync run")["env"]
    for name in ("SMTP_USERNAME", "TWILIO_FROM_NUMBER"):
        assert sync_env[name] == f"${{{{ secrets.{name} }}}}", name
    variables = {**REQUIRED_VARIABLES, **{n: f"private-{n}" for n in PRIVATE_SETTINGS}}
    result, written = _run_configuration(workflow, tmp_path, variables)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not PRIVATE_SETTINGS & set(written)
    assert "private-" not in result.stdout + result.stderr


def test_data_sync_configuration_never_reads_credentials_from_variables(
    workflow: dict[Any, Any], tmp_path: Path
) -> None:
    sync_env = _step(workflow, "app.cli sync run")["env"]
    from_secrets = {name for name, value in sync_env.items() if "secrets." in value}
    credential_like = {
        name.upper()
        for name in Settings.model_fields
        if name.upper().endswith(("_KEY", "_SECRET", "_TOKEN", "_PASSWORD"))
        or name.upper() in {"DATABASE_URL_UNPOOLED", "REDIS_URL", "CELERY_BROKER_URL"}
    }
    names = from_secrets | credential_like
    assert {"SECRET_KEY", "OPEN_METEO_API_KEY", "TWILIO_ACCOUNT_SID", "REDIS_URL"} <= names
    variables = {**REQUIRED_VARIABLES, **{name: f"leak-{name}" for name in names}}
    result, written = _run_configuration(workflow, tmp_path, variables)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not names & set(written)
    assert "leak-" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("variables", "error"),
    [
        ({"EMAIL_PROVIDER": "resend"}, "APP_BASE_URL"),
        ({"APP_BASE_URL": "https://swell.example.test"}, "EMAIL_PROVIDER"),
        (
            {"APP_BASE_URL": "https://swell.example.test", "EMAIL_PROVIDER": "console"},
            "EMAIL_PROVIDER",
        ),
        (
            {"APP_BASE_URL": "https://swell.example.test", "EMAIL_PROVIDER": "mailpit"},
            "EMAIL_PROVIDER",
        ),
    ],
)
def test_data_sync_configuration_stops_without_the_required_variables(
    workflow: dict[Any, Any], tmp_path: Path, variables: dict[str, str], error: str
) -> None:
    result, written = _run_configuration(workflow, tmp_path, variables)
    assert result.returncode == 1
    assert f"::error::set the repository variable {error}" in result.stdout
    assert written == {}


def _run_sync_step(
    workflow: dict[Any, Any], tmp_path: Path, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The Sync step's script with `python` replaced by a shim that records its arguments
    (FAIL_ON names one argument line that exits FAIL_CODE)."""
    bindir = tmp_path / "shim"
    bindir.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "calls.log"
    shim = bindir / "python"
    shim.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'if [ "$*" = "${FAIL_ON:-}" ]; then exit "${FAIL_CODE:-1}"; fi\n'
    )
    shim.chmod(0o755)
    base = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "CALLS": str(calls),
        "DATABASE_URL_UNPOOLED": "postgresql://placeholder@db.invalid/swell",
        "REDIS_URL": "rediss://placeholder@redis.invalid:6379",
        "SECRET_KEY": "k" * 64,
    }
    script = _step(workflow, "app.cli sync run")["run"]
    result = subprocess.run(  # noqa: S603 - a fixed script from the repository
        [_bash(), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_data_sync_step_runs_the_sync_with_the_inputs(
    workflow: dict[Any, Any], tmp_path: Path
) -> None:
    result, calls = _run_sync_step(workflow, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == [
        "-m app.cli check-config",
        "-m app.cli sync run --trigger github",
        "-m app.cli sync status --limit 3",
    ]
    result, calls = _run_sync_step(
        workflow, tmp_path / "all", SYNC_FORCE="true", SYNC_ONLY="spots,map", SYNC_MIGRATE="true"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == "-m app.cli migrate"
    assert "-m app.cli sync run --trigger github --force --only spots,map" in calls


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ({"SYNC_ONLY": "spots; touch pwned"}, "only must be a comma-separated subset"),
        ({"SYNC_ONLY": "spots,tides"}, "only must be a comma-separated subset"),
        ({"SYNC_FORCE": "yes"}, "force must be true or false"),
        ({"SYNC_MIGRATE": "1"}, "migrate must be true or false"),
        ({"REDIS_URL": ""}, "the secret KV_URL (or REDIS_URL) is not set"),
        ({"SECRET_KEY": ""}, "the secret SECRET_KEY is not set"),
    ],
)
def test_data_sync_step_refuses_bad_inputs_before_running_anything(
    workflow: dict[Any, Any], tmp_path: Path, env: dict[str, str], error: str
) -> None:
    result, calls = _run_sync_step(workflow, tmp_path, **env)
    assert result.returncode == 1
    assert f"::error::{error}" in result.stdout
    assert not [c for c in calls if "sync run" in c]
    assert not (tmp_path / "pwned").exists()


def test_data_sync_step_reports_a_busy_lease(workflow: dict[Any, Any], tmp_path: Path) -> None:
    result, calls = _run_sync_step(
        workflow, tmp_path, FAIL_ON="-m app.cli sync run --trigger github", FAIL_CODE="3"
    )
    assert result.returncode == 3
    assert "another sync holds the lease" in result.stdout
    assert calls[-1] == "-m app.cli sync status --limit 3"


def test_data_sync_installs_the_locked_versions(workflow: dict[Any, Any]) -> None:
    """The job with the production credentials installs the lock's files with their
    hashes checked (uv sync), not versions alone (a hash-less constraints file)."""
    install = _step(workflow, "uv sync")["run"]
    assert 'python -m pip install "uv==${UV_VERSION}"' in install
    assert "uv sync --locked --no-dev --extra sync" in install
    assert 'echo "$PWD/.venv/bin" >> "$GITHUB_PATH"' in install
    text = DATA_SYNC.read_text()
    assert "--no-hashes" not in text and "--constraint" not in text
    assert "pip install -e" not in text
    uv_version = workflow["env"]["UV_VERSION"]
    # The same uv everywhere the lock is installed: CI, this runner and the three images.
    ci = re.search(r'^\s*UV_VERSION: "([^"]+)"', CI_WORKFLOW.read_text(), re.M)
    assert ci, "ci.yml pins UV_VERSION"
    assert ci.group(1) == uv_version
    for image in (
        CONTAINERFILE,
        ROOT / "docker" / "api-lean" / "Dockerfile",
        BACKEND / "Dockerfile",
    ):
        pins = re.findall(r"uv==([\w.]+)", image.read_text())
        assert pins == [uv_version], f"{image.relative_to(ROOT)}: {pins}"
