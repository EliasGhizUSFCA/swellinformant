"""Migration 0008_publish_stored_runs: a database from before Upgrade 4 serves at once.

A scratch database is migrated to 0006 (the schema before sync_runs existed), filled the way
the pre-sync pipeline left it (SYNTHETIC demo forecasts and predictions for two spots, and a
synthetic map-field run with every core variable at one valid time), then migrated to head
with the application pointed at it. The data counts as published from the first request on:
the spots and the map show it, the map index and the status report name the publication,
and the sync's known runs hold what is stored, so the first check does not resync it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal, reset_engine
from app.core.redis import get_redis
from app.main import app
from app.models import ForecastRun, ForecastSource, SurfSpot
from app.models.enums import ForecastRunStatus
from app.models.forecast_engine import CORE_MAP_FIELD_VARIABLES, MapField
from tests.conftest import direct_database_url

pytestmark = pytest.mark.integration

KEEP = ("jeffreys-bay", "pipeline")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
PUBLISHED_BY = "migration 0008_publish_stored_runs"
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)


def _alembic(action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    getattr(command, action)(Config(str(ALEMBIC_INI)), revision)


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    """An empty scratch database the application (engine, migrations) is pointed at."""
    base_url = make_url(direct_database_url())  # a pooler refuses DROP DATABASE
    name = f"{(base_url.database or 'swell')[:40]}_m0008_{uuid.uuid4().hex[:8]}"
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
            reset_engine()
            yield url
            reset_engine()
    finally:
        reset_engine()
        get_redis().flushdb()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _store_pre_upgrade_data(now: datetime) -> tuple[int, int]:
    """What the pipeline stored before Upgrade 4, at schema 0006: (forecast run, map run)."""
    from app.services.forecasts.ingestion import acquire_all
    from app.services.seed import seed_reference_data
    from app.services.surf_quality.predict import compute_pending_predictions

    with SessionLocal() as db:
        seed_reference_data(db)
        db.execute(
            text("UPDATE surf_spots SET is_active = (slug = ANY(:keep))"), {"keep": list(KEEP)}
        )
        db.commit()
        # SYNTHETIC: the demo provider's generated forecasts and their predictions.
        acquire_all(db, now=now)
        compute_pending_predictions(db, now)
        forecast_run = db.scalars(select(ForecastRun).where(ForecastRun.status.in_(USABLE))).one()
        # SYNTHETIC map fields (not model data): one run, every core variable at one time.
        source = ForecastSource(
            code="noaa_gfs_fields",
            name="Synthetic test fields (not real data)",
            provider="noaa",
            is_demo=True,
            attribution="",
            license_notes="",
        )
        db.add(source)
        db.flush()
        cycle = now.replace(hour=0, minute=0, second=0, microsecond=0)
        map_run = ForecastRun(
            source_id=source.id,
            run_key=f"noaa-gfs-fields:{cycle:%Y%m%d%H}",
            issued_at=cycle,
            started_at=now - timedelta(minutes=30),
            completed_at=now - timedelta(minutes=20),
            status=ForecastRunStatus.SUCCESS,
            details={},
        )
        db.add(map_run)
        db.flush()
        for variable in CORE_MAP_FIELD_VARIABLES:
            db.add(
                MapField(
                    run_id=map_run.id,
                    source_code=source.code,
                    run_key=map_run.run_key,
                    cycle_time=cycle,
                    valid_time=cycle + timedelta(hours=3),
                    variable=variable,
                    nx=2,
                    ny=2,
                    lat0=-10.0,
                    lon0=0.0,
                    dlat=10.0,
                    dlon=10.0,
                    scale=0.01,
                    offset=0.0,
                    data=bytes(8),
                )
            )
        db.commit()
        return forecast_run.id, map_run.id


def _publications(db: Session) -> list[tuple[str, str, datetime, int | None, int | None, str]]:
    return [
        tuple(row)  # type: ignore[misc]
        for row in db.execute(
            text(
                "SELECT trigger, status, published_at, map_run_id, blend_run_id, "
                "plan ->> 'published_by' FROM sync_runs ORDER BY id"
            )
        )
    ]


def test_a_pre_upgrade_database_is_published_and_served_at_once(scratch: URL) -> None:
    # A new database: nothing stored, nothing published (its first sync publishes).
    _alembic("upgrade", "head")
    with SessionLocal() as db:
        assert _publications(db) == []
    _alembic("downgrade", "0006_forecast_upgrade")

    now = datetime.now(UTC).replace(microsecond=0)
    forecast_run_id, map_run_id = _store_pre_upgrade_data(now)
    _alembic("upgrade", "head")

    with SessionLocal() as db:
        newest = db.scalar(select(func.max(ForecastRun.completed_at)))
        assert _publications(db) == [
            ("pipeline", "success", newest, map_run_id, None, PUBLISHED_BY)
        ]
        forecast_key = db.get(ForecastRun, forecast_run_id).run_key  # type: ignore[union-attr]
        from app.services.sync.detect import load_known

        known = load_known(db)
        # Every stored run counts as stored (and published): the first check finds none new.
        # The demo run, stored by today's acquisition, also records the run key of each of
        # its components (Swell 2.0, design D6).
        assert known.stored == {
            "demo": forecast_key,
            "demo:wave": forecast_key,
            "demo:atmosphere": forecast_key,
            "noaa_gfs_fields": f"noaa-gfs-fields:{now:%Y%m%d}00",
        }
        assert known.map_cycle == now.replace(hour=0, minute=0, second=0)
        active = db.scalar(select(func.count()).where(SurfSpot.is_active))

    with TestClient(app) as client:
        spots = client.get("/api/spots")
        assert spots.status_code == 200
        served = [s for s in spots.json() if s["current"] is not None]
        assert {s["slug"] for s in served} == set(KEEP) and len(KEEP) == active
        conditions = client.get("/api/map/conditions").json()
        assert conditions["valid_time"] is not None
        assert sum(1 for s in conditions["spots"] if s["conditions"]) == len(KEEP)
        index = client.get("/api/map/fields").json()
        assert index["available"] is True
        assert index["run_key"] == f"noaa-gfs-fields:{now:%Y%m%d}00"
        assert index["published_at"] is not None
        sync = client.get("/api/health/data").json()["sync"]
        assert sync["last_published"]["trigger"] == "pipeline"
        assert sync["last_published"]["published_at"] == index["published_at"]
        timeline = client.get(f"/api/spots/{KEEP[0]}/timeline").json()
        assert timeline["available"] is True and timeline["steps"]

    # Downgrade removes only that publication; upgrading again records it again.
    _alembic("downgrade", "0007_sync_runs")
    with SessionLocal() as db:
        assert _publications(db) == []
    _alembic("upgrade", "head")
    with SessionLocal() as db:
        assert len(_publications(db)) == 1


def test_a_database_with_a_publication_is_left_alone(scratch: URL) -> None:
    _alembic("upgrade", "0006_forecast_upgrade")
    now = datetime.now(UTC).replace(microsecond=0)
    _store_pre_upgrade_data(now)
    _alembic("upgrade", "0007_sync_runs")
    published_at = now - timedelta(hours=1)
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO sync_runs (trigger, status, started_at, finished_at, published_at) "
                "VALUES ('daemon', 'success', :at, :at, :at)"
            ),
            {"at": published_at},
        )
        db.commit()
    _alembic("upgrade", "head")
    with SessionLocal() as db:
        assert _publications(db) == [("daemon", "success", published_at, None, None, None)]
