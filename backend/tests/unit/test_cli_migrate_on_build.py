"""``python -m app.cli migrate --on-build`` (D9).

MIGRATE_ON_BUILD=off never migrates; production migrates only production builds
(VERCEL_ENV=production), so preview builds and Neon preview branches are left alone; all
migrates every build. A failed migration fails the build (exit 1). Alembic is patched.
"""

from __future__ import annotations

import json

import pytest

from app import cli
from app.core.config import get_settings


@pytest.fixture
def migrations(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []
    monkeypatch.setattr(cli, "migrate", lambda: calls.append(True))
    return calls


def on_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
    vercel_env: str | None,
) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(get_settings(), "migrate_on_build", setting)
    if vercel_env is None:
        monkeypatch.delenv("VERCEL_ENV", raising=False)
    else:
        monkeypatch.setenv("VERCEL_ENV", vercel_env)
    code = cli.main(["migrate", "--on-build"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("vercel_env", [None, "preview", "production"])
def test_off_never_migrates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    vercel_env: str | None,
) -> None:
    code, out = on_build(monkeypatch, capsys, "off", vercel_env)
    assert code == 0 and migrations == []
    assert out["status"] == "skipped" and out["reason"] == "MIGRATE_ON_BUILD=off"


@pytest.mark.parametrize("vercel_env", [None, "preview", "development"])
def test_production_skips_other_builds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    vercel_env: str | None,
) -> None:
    code, out = on_build(monkeypatch, capsys, "production", vercel_env)
    assert code == 0 and migrations == []
    assert out["status"] == "skipped"
    assert f"VERCEL_ENV={vercel_env or 'unset'}" in str(out["reason"])
    assert out["vercel_env"] == vercel_env


def test_production_migrates_a_production_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrations: list[bool]
) -> None:
    code, out = on_build(monkeypatch, capsys, "production", "production")
    assert code == 0 and migrations == [True]
    assert out == {
        "command": "migrate --on-build",
        "migrate_on_build": "production",
        "vercel_env": "production",
        "status": "migrated",
    }


@pytest.mark.parametrize("vercel_env", [None, "preview", "production"])
def test_all_migrates_every_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    vercel_env: str | None,
) -> None:
    code, out = on_build(monkeypatch, capsys, "all", vercel_env)
    assert code == 0 and migrations == [True] and out["status"] == "migrated"


def test_a_failed_migration_fails_the_build_without_leaking_the_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise RuntimeError("could not connect: password=hunter2x host=db.example")

    monkeypatch.setattr(cli, "migrate", broken)
    code, out = on_build(monkeypatch, capsys, "all", "production")
    assert code == 1 and out["status"] == "failed"
    assert "hunter2x" not in str(out["error"]) and "RuntimeError" in str(out["error"])


def test_plain_migrate_ignores_the_build_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrations: list[bool]
) -> None:
    monkeypatch.setattr(get_settings(), "migrate_on_build", "off")
    assert cli.main(["migrate"]) == 0
    assert migrations == [True] and capsys.readouterr().out == "database migrated\n"
