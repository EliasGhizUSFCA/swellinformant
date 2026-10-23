"""Retention cleanup and Alembic migration round-trips."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import ForecastRun, UserSession, UserToken
from app.services import accounts
from app.services.forecasts.base import RunInfo
from app.services.forecasts.demo import DemoForecastProvider
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.maintenance import cleanup
from tests.conftest import direct_database_url

pytestmark = pytest.mark.integration


def test_cleanup_respects_retention(db: Session, few_spots: list[str]) -> None:
    old = utcnow() - timedelta(days=30)
    for i in range(4):
        issued = old + timedelta(hours=6 * i)
        acquire_from_provider(
            db, DemoForecastProvider(3), run_info=RunInfo(f"demo:old{i}", issued, issued, issued)
        )
    db.execute(update(ForecastRun).values(started_at=old))
    acquire_from_provider(db, DemoForecastProvider(3))
    user = accounts.register(db, "X", "x@example.com", "Barrels4Days!")
    db.execute(update(UserToken).values(expires_at=old))
    db.add(
        UserSession(
            user_id=user.id, token_hash="a" * 64, created_at=old, last_seen_at=old, expires_at=old
        )
    )
    db.commit()

    out = cleanup(db)
    remaining = db.scalars(select(ForecastRun.run_key)).all()
    # the two newest usable runs per source (the fresh one + the newest old one) always stay
    assert out["forecast_runs"] == 3 and "demo:old3" in remaining and len(remaining) == 2
    assert out["tokens"] >= 1 and out["sessions"] == 1
    assert db.scalar(select(func.count(UserSession.id))) == 0


def test_migrations_upgrade_downgrade_and_match_models() -> None:
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from app.core.database import Base

    base_url = make_url(direct_database_url())  # a pooler refuses DROP DATABASE
    # One scratch database per run: test runs on the same server (each with its own test
    # database) dropped each other's fixed-name scratch database mid-migration.
    name = f"{(base_url.database or 'swell')[:40]}_migcheck_{uuid.uuid4().hex[:8]}"
    scratch = base_url.set(database=name)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on DB privileges
        pytest.skip(f"cannot create scratch database: {exc}")
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    previous = os.environ["DATABASE_URL"]
    from app.core.config import get_settings

    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = scratch.render_as_string(hide_password=False)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        engine = create_engine(scratch)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
            tables = set(
                conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                ).scalars()
            )
        engine.dispose()
        assert diff == [], diff
        assert {
            "users",
            "surf_spots",
            "swell_events",
            "notifications",
            "background_job_logs",
        } <= tables
    finally:
        os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
