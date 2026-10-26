"""scripts/reset_database.py: only scratch databases (_e2e / _test, optional suffix) may be
dropped and recreated. No connection is made: refused names exit before connecting."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "reset_database.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("reset_database", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # the main() guard: importing connects to nothing
    return module


@pytest.mark.parametrize(
    "name",
    [
        "swell_e2e",
        "swell_test",
        "swell_e2e_ci2",
        "swell_test_alice",
        # Scratch suffixes with underscores (the package and integration databases).
        "swell_e2e_u3_map_flow",
        "swell_test_u3i_wire",
        "swell_e2e_u3iwire",
    ],
)
def test_scratch_names_are_resettable(name: str) -> None:
    assert load().resettable(name)


@pytest.mark.parametrize(
    "name",
    [
        "swell",
        "swell_live",
        "swell_live2",
        "swell_ndbc_live",
        "postgres",
        "swelle2e",
        "swell_e2e-ci",  # only letters, digits and underscores after the marker
        "swell_e2e_",
        "swell_testing",
        "e2e",
        "",
    ],
)
def test_other_names_are_refused(name: str) -> None:
    assert not load().resettable(name)


def test_the_script_refuses_before_connecting() -> None:
    env = {
        **os.environ,
        # Port 9 (discard): a connection attempt would fail with a different message.
        "DATABASE_URL": "postgresql+psycopg://nobody:nothing@127.0.0.1:9/swell_live",
    }
    out = subprocess.run(  # noqa: S603 - fixed argument list
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )
    assert out.returncode == 1
    assert "refusing to reset database 'swell_live'" in out.stderr
