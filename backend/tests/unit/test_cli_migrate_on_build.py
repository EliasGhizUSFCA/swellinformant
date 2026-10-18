"""``python -m app.cli migrate --on-build`` (contract F8). Owned by the sync-core package.

Created by the foundation with the stub's behaviour only: sync-core replaces it with the
off / production / all cases of its design.
"""

from __future__ import annotations

import json

import pytest

from app import cli


def test_migrate_on_build_is_a_stub_and_migrates_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def must_not_run() -> None:
        raise AssertionError("migrate --on-build must not migrate yet")

    monkeypatch.setattr(cli, "migrate", must_not_run)
    assert cli.main(["migrate", "--on-build"]) == cli.EXIT_NOT_IMPLEMENTED == 4
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "status": "not_implemented",
        "command": "migrate --on-build",
        "detail": "not implemented yet: nothing was migrated",
    }
