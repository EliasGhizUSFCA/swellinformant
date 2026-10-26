"""scripts/profile_sensitivity.py: every sub-command accepts the shared options --json,
--spots and --decisions, whether or not it reads them, so an existing invocation such as
``wind --runs 2,3 --spots FILE`` keeps parsing. No database is opened: the sub-command
functions are replaced before main() dispatches to them."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "profile_sensitivity.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("profile_sensitivity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # the main() guard: importing connects to nothing
    return module


def parsed(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    module = load()
    seen: list[argparse.Namespace] = []
    for name in ("cmd_rescore", "cmd_decisions", "cmd_wind"):
        monkeypatch.setattr(module, name, lambda args: seen.append(args) or [])
    assert module.main(argv) == 0
    (args,) = seen
    return args


@pytest.mark.parametrize(
    "argv",
    [
        ["rescore", "--run-id", "4"],
        ["decisions", "--run-id", "4"],
        ["wind", "--runs", "2,3"],
    ],
)
def test_every_sub_command_accepts_spots_and_decisions(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = parsed([*argv, "--spots", "s.json", "--decisions", "d.json"], monkeypatch)
    assert args.cmd == argv[0]
    assert args.spots == Path("s.json")
    assert args.decisions == Path("d.json")
    assert capsys.readouterr().out == "[]\n"


@pytest.mark.parametrize(
    "argv", [["rescore", "--run-id", "4"], ["decisions", "--run-id", "4"], ["wind", "--runs", "2"]]
)
def test_spots_and_decisions_default_to_the_shipped_files(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    args = parsed(argv, monkeypatch)
    data = ROOT / "data" / "surf_spots"
    assert args.spots == data / "spots.json"
    assert args.decisions == data / "profile_decisions.json"
    assert args.spots.is_file() and args.decisions.is_file()
