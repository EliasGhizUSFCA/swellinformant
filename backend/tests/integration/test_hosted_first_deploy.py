"""A first Vercel deployment without the optional daemon (review r2-spec-R2-SPEC-2).

The api service's build command (``python -m app.cli migrate --on-build``) is the only step
that touches a new database when no daemon runs ``init-db``. It migrates and seeds the
reference data, so the first refresh check publishes every spot. A database that was only
migrated (``MIGRATE_ON_BUILD=off`` and no ``init-db``) is named by health
(``no_spots_seeded``) and its refresh fails and backs off instead of waking the database at
every check.

Each test runs against a scratch database of its own; the forecasts come from the demo
generator (SYNTHETIC by design).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.engine import URL

from app import cli
from app.core.config import get_settings
from app.core.database import SessionLocal, reset_engine, utcnow
from app.core.redis import get_redis
from app.models import SurfSpot
from app.models.sync import SyncRun
from app.services.forecasts.versions import current_version
from app.services.health import data_health
from app.services.sync import orchestrator
from tests.conftest import direct_database_url

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    """An empty scratch database the application (engine, migrations) is pointed at, with
    the hosted profile's runner settings (no daemon)."""
    base_url = make_url(direct_database_url())  # a pooler refuses DROP DATABASE
    name = f"{(base_url.database or 'swell')[:40]}_first_{uuid.uuid4().hex[:8]}"
    url = base_url.set(database=name)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on DB privileges
        pytest.skip(f"cannot create scratch database: {exc}")
    settings = get_settings()
    get_redis().flushdb()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(settings, "database_url", url.render_as_string(hide_password=False))
            patch.setattr(settings, "db_use_unpooled", False)
            patch.setattr(settings, "job_runner", "daemon")
            patch.setattr(settings, "sync_daemon_kinds", "map")
            patch.setattr(settings, "map_fields_enabled", False)
            reset_engine()
            yield url
            reset_engine()
    finally:
        reset_engine()
        get_redis().flushdb()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def active_spots() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).where(SurfSpot.is_active.is_(True))) or 0)


def test_the_build_step_seeds_so_the_first_refresh_publishes_every_spot(
    scratch: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(get_settings(), "migrate_on_build", "production")
    monkeypatch.setenv("VERCEL_ENV", "production")
    assert cli.main(["migrate", "--on-build"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "migrated"
    spots = active_spots()
    assert spots == 50 and report["seed"]["spots_created"] == 50
    out = orchestrator.refresh_check(trigger="http")
    assert out["status"] in ("success", "published"), out
    assert out["version_ids"]
    with SessionLocal() as db:
        version = current_version(db)
        assert version is not None and version.spots_updated == spots
        keys = {p.key for p in data_health(db).problems}
    assert "no_spots_seeded" not in keys
    # Seeding again (the next production build) changes nothing.
    assert cli.main(["migrate", "--on-build"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["seed"]["spots_created"] == 0 and active_spots() == spots


def test_a_database_never_seeded_is_named_and_its_refresh_backs_off(scratch: URL) -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ALEMBIC_INI)), "head")  # migrated only: no init-db
    # One minute into the demo's current 6-hour cycle, so the checks below see the same run
    # (a new run would get attempts of its own) and never run ahead of the wall clock the
    # attempts are recorded at.
    wall = utcnow()
    now = wall.replace(hour=wall.hour // 6 * 6, minute=1, second=0, microsecond=0)
    with SessionLocal() as db:
        problems = {p.key: p for p in data_health(db, now=now).problems}
    assert problems["no_spots_seeded"].severity == "failing"
    assert "init-db" in problems["no_spots_seeded"].message
    first = orchestrator.refresh_check(trigger="http", now=now)
    assert first["status"] == "failed", first
    assert [r["status"] for r in first["refreshes"]] == ["failed"]
    with SessionLocal() as db:
        run = db.get(SyncRun, first["sync_run_id"])
        assert run is not None and "init-db" in json.dumps(run.stages)
    # The failure is an attempt: the next checks within SYNC_RETRY_MINUTES are not due, so
    # no sync (and no database wake) follows every few minutes.
    for minutes in (5, 10):
        later = orchestrator.refresh_check(trigger="http", now=now + timedelta(minutes=minutes))
        assert later["status"] == "not_due", later
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SyncRun)) == 1
