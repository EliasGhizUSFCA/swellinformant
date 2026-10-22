"""The dependency split (design D5 and F9): each runtime imports with what its install has.

[project].dependencies is the lean set the hosted API (a Vercel Python function) installs.
The extras add the rest: worker = Celery, server = uvicorn[standard], sync = pandas, ecCodes,
pyarrow, scikit-learn and joblib (the sync daemon's image), all = the three (the classic
image), dev = all plus the tools.

(a) the API, app.main and every app.api module, imports with every extra missing;
(b) the sync runner, the daemon's modules, imports without Celery (the sync image has none);
(c) the light jobs the daemon runs, and the API kicks rely on, import without Celery or
    the data libraries;
(d) pyproject.toml and uv.lock keep that split;
(e) scripts/check_api_bundle.py attributes installed bytes to distributions correctly;
(f) with LEAN_PYTHON set to the interpreter of a real lean install (CI makes one from uv.lock),
    (a) and (c) again with nothing blocked, and `uvicorn app.main:app` serves /api/health.

(a)-(c) run in fresh interpreters whose import system refuses the missing packages as
ModuleNotFoundError, as an install without them would, and also fail on an import attempt
that the code catches.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import tomllib
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
CELERY = ("celery", "kombu")
# Top-level modules of the sync extra (gribapi is ecCodes' legacy module name).
DATA_LIBRARIES = ("pandas", "eccodes", "gribapi", "pyarrow", "sklearn", "scipy", "joblib")
# Top-level modules of the server extra (uvicorn[standard]).
SERVER_EXTRAS = ("uvloop", "httptools", "watchfiles", "websockets")
API_MODULES = sorted(
    f"app.api.{path.stem}"
    for path in (BACKEND / "app" / "api").glob("*.py")
    if path.stem != "__init__"
)
# The sync daemon's modules, and the job core it runs (the image has the sync extra only).
SYNC_RUNNER_MODULES = [
    "app.services.sync.state",
    "app.jobs.runner",
    "app.jobs.registry",
    "app.jobs.schedule",
    "app.services.sync.types",
    "app.services.sync.published",
    "app.services.sync.detect",
    "app.services.sync.orchestrator",
    "app.services.sync.publish",
    "app.services.sync.daemon",
    "app.services.sync.cli",
]
# Jobs the daemon runs between syncs, and that the API's kicks (JOB_RUNNER=daemon) start.
LIGHT_JOB_MODULES = [
    "app.services.matching.service",
    "app.services.flights.service",
    "app.services.notifications.delivery",
    "app.services.notifications.alerts",
    "app.services.notifications.ops_alerts",
    "app.services.swell_detection.service",
]
# Distributions that must never be in the lean closure of uv.lock.
NOT_LEAN = {
    "celery",
    "kombu",
    "billiard",
    "pandas",
    "eccodes",
    "eccodeslib",
    "eckitlib",
    "pyarrow",
    "scikit-learn",
    "scipy",
    "joblib",
    "uvloop",
    "httptools",
    "watchfiles",
    "websockets",
}


def import_without(modules: list[str], missing: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """Import ``modules`` in a fresh interpreter in which the ``missing`` packages are absent."""
    code = textwrap.dedent(
        f"""
        import importlib
        import sys

        MISSING = {tuple(missing)!r}
        attempts = []

        class Missing:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in MISSING:
                    attempts.append(name)
                    raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
                return None

        sys.meta_path.insert(0, Missing())
        for module in {modules!r}:
            importlib.import_module(module)
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in MISSING)
        assert not attempts and not loaded, (attempts, loaded)
        print("ok")
        """
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=180
    )


def assert_imported(done: subprocess.CompletedProcess[str]) -> None:
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-4000:]


# ------------------------------------------------------------------------------ (a)-(c)
def test_the_api_imports_with_every_extra_missing() -> None:
    assert "app.api.internal" in API_MODULES and "app.api.dev" in API_MODULES
    missing = CELERY + DATA_LIBRARIES + SERVER_EXTRAS
    assert_imported(import_without(["app.main", *API_MODULES], missing))


def test_the_sync_runner_imports_without_celery() -> None:
    assert_imported(import_without(SYNC_RUNNER_MODULES, CELERY))


def test_the_light_jobs_import_without_celery_or_the_data_libraries() -> None:
    assert_imported(import_without(LIGHT_JOB_MODULES, CELERY + DATA_LIBRARIES))


def test_the_blocker_catches_a_heavy_import() -> None:
    """The check above is not vacuous: a module that imports pandas fails it."""
    done = import_without(["app.services.surf_quality.predict"], DATA_LIBRARIES)
    assert done.returncode != 0
    assert "No module named" in done.stderr


# ---------------------------------------------------------------------------------- (d)
def _pyproject() -> dict[str, object]:
    return tomllib.loads((BACKEND / "pyproject.toml").read_text())


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, requirement
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def test_the_extras_split_the_dependencies_as_designed() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    lean = {_requirement_name(r) for r in project["dependencies"]}
    assert lean == {
        "fastapi",
        "uvicorn",
        "pydantic",
        "pydantic-settings",
        "email-validator",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "redis",
        "httpx",
        "numpy",
        "argon2-cffi",
        "python-multipart",
        "tzdata",
        "cryptography",
    }
    # Plain uvicorn and psycopg's binary wheel; no extras of uvicorn in the lean set.
    assert "uvicorn>=0.30" in project["dependencies"]
    assert any(r.startswith("psycopg[binary]") for r in project["dependencies"])
    extras = project["optional-dependencies"]
    assert set(extras) == {"worker", "server", "sync", "all", "dev", "tides-extract"}
    assert {_requirement_name(r) for r in extras["worker"]} == {"celery"}
    assert extras["server"] == ["uvicorn[standard]>=0.30"]
    assert {_requirement_name(r) for r in extras["sync"]} == {
        "pandas",
        "eccodes",
        "eccodeslib",
        "pyarrow",
        "scikit-learn",
        "joblib",
    }
    assert extras["all"] == ["swell-travel-agent[worker,server,sync]"]
    assert "swell-travel-agent[all]" in extras["dev"]
    assert {"pytest", "pytest-cov", "ruff", "mypy"} <= {_requirement_name(r) for r in extras["dev"]}


def test_python_is_pinned_to_3_13() -> None:
    assert (BACKEND / ".python-version").read_text().strip() == "3.13"


def _lock() -> dict[str, dict[str, object]]:
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())
    return {package["name"]: package for package in lock["package"]}


def _closure(packages: dict[str, dict[str, object]], roots: Iterable[dict[str, Any]]) -> set[str]:
    """Every package reachable from the ``roots`` (uv.lock dependency entries: a name and
    perhaps extras) through locked dependencies and their extras; markers are ignored."""
    seen: set[tuple[str, str | None]] = set()
    todo = [(d["name"], extra) for d in roots for extra in [None, *d.get("extra", [])]]
    while todo:
        name, extra = todo.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        package = packages[name]
        if extra is None:
            dependencies = package.get("dependencies", [])
        else:
            optional = package.get("optional-dependencies", {})
            assert isinstance(optional, dict)
            dependencies = optional[extra]
        assert isinstance(dependencies, list)
        todo += [(d["name"], e) for d in dependencies for e in [None, *d.get("extra", [])]]
    return {name for name, _extra in seen}


def test_the_lock_matches_the_split() -> None:
    packages = _lock()
    project = packages["swell-travel-agent"]
    assert project["source"] == {"editable": "."}
    # The lean closure: what the Vercel builder installs for the api service.
    roots = project["dependencies"]
    assert isinstance(roots, list)
    lean = _closure(packages, roots)
    assert not lean & NOT_LEAN, sorted(lean & NOT_LEAN)
    assert {"fastapi", "sqlalchemy", "psycopg-binary", "numpy", "redis"} <= lean
    # Every extra is locked, so `uv sync --locked --extra <name>` works for each of them.
    optional = project["optional-dependencies"]
    assert isinstance(optional, dict)
    assert set(optional) == {"worker", "server", "sync", "all", "dev", "tides-extract"}
    sync = _closure(packages, optional["sync"])
    # The ecCodes C library is locked with the bindings (see pyproject.toml).
    assert {"eccodes", "eccodeslib", "eckitlib", "pandas", "scikit-learn"} <= sync
    assert "celery" not in sync


def test_the_lock_resolves_python_3_13() -> None:
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())
    assert lock["requires-python"] == _pyproject()["project"]["requires-python"]  # type: ignore[index]
    # cp313 wheels for the Linux x86_64 function and daemon platform.
    for name in ("numpy", "pydantic-core", "psycopg-binary"):
        wheels = _lock()[name]["wheels"]
        assert isinstance(wheels, list)
        urls = [w["url"] for w in wheels]
        assert any("cp313-cp313-manylinux" in u and "x86_64" in u for u in urls), name


# ---------------------------------------------------------------------------------- (e)
def _bundle_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_api_bundle", ROOT / "scripts" / "check_api_bundle.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_the_bundle_check_attributes_files_to_distributions(tmp_path: Path) -> None:
    script = _bundle_script()
    target = tmp_path / "site"
    _write(target / "alpha" / "__init__.py", 100)
    _write(target / "alpha" / "core.cpython-313-x86_64-linux-gnu.so", 1000)
    _write(target / "alpha" / "__pycache__" / "__init__.cpython-313.pyc", 50)
    _write(target / "beta.py", 10)
    _write(target / "stray.txt", 7)
    dist = target / "alpha-1.2.dist-info"
    _write(dist / "METADATA", 20)
    record_rows = [
        "alpha/__init__.py,sha256=x,100",
        "alpha/core.cpython-313-x86_64-linux-gnu.so,sha256=x,1000",
        "alpha-1.2.dist-info/METADATA,sha256=x,20",
        "alpha-1.2.dist-info/RECORD,,",
        "../../bin/alpha,sha256=x,5",
    ]
    (dist / "RECORD").write_text("\n".join(record_rows) + "\n")
    record_size = (dist / "RECORD").stat().st_size
    beta = target / "beta_pkg-0.1.dist-info"
    (beta).mkdir()
    (beta / "RECORD").write_text("beta.py,,10\nbeta_pkg-0.1.dist-info/RECORD,,\n")
    beta_record = (beta / "RECORD").stat().st_size

    sizes = script.measure_target(target)

    assert [(d.name, d.version) for d in sizes.distributions] == [
        ("alpha", "1.2"),
        ("beta_pkg", "0.1"),
    ]
    alpha, beta_dist = sizes.distributions
    # Its RECORD's files, its own RECORD and the bytecode compiled from its sources.
    assert alpha.size == 100 + 1000 + 20 + record_size + 50
    assert alpha.files == 5
    assert beta_dist.size == 10 + beta_record
    assert sizes.unattributed == 7
    assert sizes.total == alpha.size + beta_dist.size + 7


def test_the_bundle_check_fails_above_the_limit(tmp_path: Path) -> None:
    script = _bundle_script()
    sizes = script.TargetSizes(
        [script.Distribution("numpy", "2.5.3", 150 * script.MB, 10)], 0, 150 * script.MB
    )
    text, within = script.report(sizes, 5 * script.MB, 200)
    assert within and "OK: within the limit (155.0 of 200 MB)" in text
    assert "numpy 2.5.3" in text and "1 distributions" in text
    text, within = script.report(sizes, 5 * script.MB, 150)
    assert not within and text.endswith("FAIL: above the limit (155.0 of 150 MB)")


def test_the_bundle_check_counts_only_bundled_project_files(tmp_path: Path) -> None:
    script = _bundle_script()
    _write(tmp_path / "app" / "main.py", 300)
    _write(tmp_path / "alembic.ini", 20)
    _write(tmp_path / "app" / "__pycache__" / "main.cpython-313.pyc", 999)
    _write(tmp_path / "tests" / "test_x.py", 999)
    _write(tmp_path / ".venv" / "lib" / "big.so", 999)
    _write(tmp_path / "build" / "lib" / "app" / "main.py", 999)
    _write(tmp_path / "swell_travel_agent.egg-info" / "PKG-INFO", 999)
    _write(tmp_path / "dump.rdb", 999)
    _write(tmp_path / ".env", 999)
    _write(tmp_path / ".env.local", 999)
    assert script.project_files_size(tmp_path) == 320


def test_the_bundle_check_without_uv_says_how_to_get_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _bundle_script()
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(script.shutil, "which", lambda _name: None)
    assert script.main([]) == 2
    assert "uv not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------------- (f)
LEAN_PYTHON = os.environ.get("LEAN_PYTHON", "")
needs_lean_install = pytest.mark.skipif(
    not LEAN_PYTHON, reason="LEAN_PYTHON names no lean install (CI sets it)"
)


def _lean(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - interpreter chosen by the CI job
        [LEAN_PYTHON, "-c", textwrap.dedent(code)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=180,
    )


@needs_lean_install
def test_the_lean_install_has_no_extra() -> None:
    absent = CELERY + DATA_LIBRARIES + SERVER_EXTRAS + ("pytest", "ruff", "mypy")
    done = _lean(
        f"""
        import importlib.util, json
        print(json.dumps([m for m in {absent!r} if importlib.util.find_spec(m)]))
        """
    )
    assert done.returncode == 0, done.stderr[-4000:]
    assert json.loads(done.stdout) == []


@needs_lean_install
def test_the_lean_install_imports_the_api_and_the_light_jobs() -> None:
    done = _lean(
        f"""
        import importlib
        for module in {["app.main", *API_MODULES, *LIGHT_JOB_MODULES]!r}:
            importlib.import_module(module)
        print("ok")
        """
    )
    assert_imported(done)


@needs_lean_install
def test_the_lean_install_runs_the_build_time_cli() -> None:
    done = subprocess.run(  # noqa: S603 - interpreter chosen by the CI job
        [LEAN_PYTHON, "-m", "app.cli", "--help"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    for command in ("migrate", "init-db", "check-config"):
        assert command in done.stdout


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@needs_lean_install
def test_the_lean_install_serves_the_health_check() -> None:
    port = _free_port()
    server = subprocess.Popen(  # noqa: S603 - interpreter chosen by the CI job
        [LEAN_PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1"]
        + ["--port", str(port), "--no-proxy-headers"],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            assert server.poll() is None, server.communicate()[0][-4000:]
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2)
                break
            except httpx.TransportError:
                assert time.monotonic() < deadline, "uvicorn did not start within 60 s"
                time.sleep(0.5)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        server.terminate()
        server.wait(timeout=30)
