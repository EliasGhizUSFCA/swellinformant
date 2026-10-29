"""``python -m app.cli migrate --on-build`` (D9).

MIGRATE_ON_BUILD=off never migrates; production migrates only production builds
(VERCEL_ENV=production), so preview builds and Neon preview branches are left alone; all
migrates every build. A migrating build then seeds the reference data as init-db does
(review r2-spec-SPEC-2: without the optional daemon nothing else seeds a new database). A
failed migration or seed fails the build (exit 1). Alembic and the seeding are patched.
"""

from __future__ import annotations

import json

import pytest

from app import cli
from app.core.config import get_settings

SEEDED = {"seed": {"airports": 3, "spots_created": 2}, "tide_catalog": {"stations": 1}}


@pytest.fixture
def seeds(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls: list[bool] = []

    def seed() -> dict[str, object]:
        calls.append(True)
        return dict(SEEDED)  # SYNTHETIC counts

    monkeypatch.setattr(cli, "seed_database", seed)
    return calls


@pytest.fixture
def migrations(monkeypatch: pytest.MonkeyPatch, seeds: list[bool]) -> list[bool]:
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
        **SEEDED,
    }


def test_a_migrating_build_seeds_the_reference_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    seeds: list[bool],
) -> None:
    code, out = on_build(monkeypatch, capsys, "all", "preview")
    assert code == 0 and migrations == [True] and seeds == [True]
    assert out["seed"] == SEEDED["seed"] and out["tide_catalog"] == SEEDED["tide_catalog"]
    # A skipped build seeds nothing either.
    code, out = on_build(monkeypatch, capsys, "production", "preview")
    assert code == 0 and out["status"] == "skipped" and seeds == [True]


def test_a_failed_seed_fails_the_build(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrations: list[bool]
) -> None:
    def broken() -> None:
        raise FileNotFoundError("spots.json not found in any of: [...]")

    monkeypatch.setattr(cli, "seed_database", broken)
    code, out = on_build(monkeypatch, capsys, "all", "production")
    assert code == 1 and out["status"] == "failed" and out["stage"] == "seed"
    assert migrations == [True] and "spots.json" in str(out["error"])


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


def _no_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import seed

    def missing(data_dir: object = None) -> None:
        raise FileNotFoundError("spots.json not found in any of: ['/nowhere']")

    monkeypatch.setattr(seed, "resolve_data_dir", missing)


def test_a_build_without_the_data_directory_skips_the_seed_of_a_seeded_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    seeds: list[bool],
) -> None:
    """An image without data/ (the local stack's lean api image) against a database that is
    seeded already (50 active spots, SYNTHETIC count): nothing to seed from, the build
    succeeds and says why it did not seed."""
    _no_data_dir(monkeypatch)
    monkeypatch.setattr(cli, "_active_spot_count", lambda: 50)
    code, out = on_build(monkeypatch, capsys, "all", "production")
    assert code == 0 and out["status"] == "migrated" and out["seed"] == "skipped"
    assert "50 active spots already seeded" in str(out["seed_reason"])
    assert migrations == [True] and seeds == []


def test_a_build_without_the_data_directory_fails_on_an_empty_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrations: list[bool],
    seeds: list[bool],
) -> None:
    """Review r3-spec-SPEC-6: the data live at the repository root, outside the api service's
    root (backend/). A build that cannot see them used to succeed unseeded, and the site then
    published nothing until someone ran init-db by hand. With no active spot the build fails
    and says what to do; a database whose count cannot be read fails it too."""
    _no_data_dir(monkeypatch)
    monkeypatch.setattr(cli, "_active_spot_count", lambda: 0)
    code, out = on_build(monkeypatch, capsys, "production", "production")
    assert code == 1 and out["status"] == "failed" and out["stage"] == "seed"
    assert "data/ not found from the api build" in str(out["error"])
    assert "init-db" in str(out["error"]) and migrations == [True] and seeds == []

    def unreadable() -> int:
        raise RuntimeError("could not connect: password=hunter2x")

    monkeypatch.setattr(cli, "_active_spot_count", unreadable)
    code, out = on_build(monkeypatch, capsys, "all", "production")
    assert code == 1 and out["stage"] == "seed" and "hunter2x" not in str(out["error"])
