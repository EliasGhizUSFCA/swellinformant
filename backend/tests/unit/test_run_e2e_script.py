"""scripts/run-e2e.sh: --help, the dry run and an unknown option touch nothing.

The script drops and recreates the database of E2E_DATABASE_URL, so a ``--help`` that ran it
with its defaults recreated swell_e2e once (2026-10-30). Every run here has a guard
environment that would fail on any real step: the database and Redis URLs point at a closed
port, the commands the script would run (npx, redis-cli, docker, curl, psql, python3) are
stubs that record the call and fail, and its temporary directory must stay empty.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "run-e2e.sh"
PLAYWRIGHT = ROOT / "frontend" / "node_modules" / ".bin" / "playwright"
GUARD_DB = "postgresql+psycopg://guard:guard@127.0.0.1:9/guard_never_e2e"
TRAPPED = ("npx", "redis-cli", "docker", "curl", "psql", "python3")
BASH = shutil.which("bash") or "/bin/bash"


def run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    trap = tmp_path / "trap"
    trap.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    for name in TRAPPED:
        stub = trap / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 97\n')
        stub.chmod(0o755)
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    base = {
        "PATH": f"{trap}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "TMPDIR": str(scratch),
        "E2E_DATABASE_URL": GUARD_DB,
        "E2E_REDIS_URL": "redis://127.0.0.1:9/0",
    }
    return subprocess.run(  # noqa: S603 - fixed argument list
        [BASH, str(SCRIPT), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def untouched(tmp_path: Path, result: subprocess.CompletedProcess[str]) -> None:
    out = result.stdout + result.stderr
    assert "preparing database" not in out and "starting backend" not in out, out
    assert not (tmp_path / "calls.log").exists(), (tmp_path / "calls.log").read_text()
    assert list((tmp_path / "tmp").iterdir()) == []  # no outbox directory was made


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_and_touches_nothing(tmp_path: Path, flag: str) -> None:
    result = run(tmp_path, flag)
    assert result.returncode == 0, result.stderr
    usage = result.stdout
    for text in (
        "DROPS and recreates the PostgreSQL database of E2E_DATABASE_URL",
        "FLUSHDB on the Redis database of E2E_REDIS_URL",
        "[postgresql+psycopg://swell:swell@localhost:5432/swell_e2e]",
        "[redis://localhost:6379/14]",
        "E2E_BACKEND_PORT     [8100]",
        "E2E_FRONTEND_PORT    [3100]",
        "E2E_DRY_RUN",
        "exit status 64",
    ):
        assert text in usage, text
    assert not usage.startswith("#!")  # the comment text, not the source
    untouched(tmp_path, result)


def test_help_wins_wherever_it_stands(tmp_path: Path) -> None:
    result = run(tmp_path, "--project=chromium", "e2e/scenario.spec.ts", "--help")
    assert result.returncode == 0 and "Environment (default in brackets)" in result.stdout
    untouched(tmp_path, result)


def test_an_unknown_option_fails_before_anything_is_reset(tmp_path: Path) -> None:
    result = run(tmp_path, "--project=chromium", "--no-such-option")
    # 64 with the frontend's Playwright installed (it lists the options); 2, a setup error,
    # where frontend/node_modules is missing (the backend CI job). Nothing runs either way.
    if PLAYWRIGHT.exists():
        assert result.returncode == 64, result.stderr
        assert "unknown option: --no-such-option" in result.stderr
    else:
        assert result.returncode == 2 and "npm ci" in result.stderr
    untouched(tmp_path, result)


@pytest.mark.skipif(not PLAYWRIGHT.exists(), reason="frontend/node_modules is not installed")
@pytest.mark.parametrize(
    "args",
    [
        ["--project=chromium", "--project=mobile"],  # the CI job
        ["e2e/scenario.spec.ts"],  # README
        ["--project", "chromium", "e2e/live-update.spec.ts"],
        ["-g", "-a grep that starts with a dash", "--repeat-each", "2", "-x", "-j4"],
        ["--grep=foo", "--headed", "--reporter=line", "-u"],
    ],
)
def test_a_dry_run_accepts_playwright_arguments_and_touches_nothing(
    tmp_path: Path, args: list[str]
) -> None:
    result = run(tmp_path, *args, E2E_DRY_RUN="1")
    assert result.returncode == 0, result.stderr
    assert f"drop and recreate the database of {GUARD_DB}" in result.stdout
    assert "npx playwright test" in result.stdout
    untouched(tmp_path, result)
