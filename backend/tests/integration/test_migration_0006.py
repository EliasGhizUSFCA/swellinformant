"""Migration 0006_forecast_upgrade on a scratch database: data kept up, partitions down."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.orm import Session

from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.models.forecast_engine import MapField

pytestmark = pytest.mark.integration

CYCLE = datetime(2026, 10, 14, 0, tzinfo=UTC)


def test_0006_round_trip_keeps_core_fields_and_drops_partition_rows() -> None:
    from alembic import command
    from alembic.config import Config

    from app.core.config import get_settings

    base_url = make_url(os.environ["DATABASE_URL"])
    name = f"{(base_url.database or 'swell')[:40]}_m0006_{uuid.uuid4().hex[:8]}"
    scratch = base_url.set(database=name)
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # pragma: no cover - depends on DB privileges
        pytest.skip(f"cannot create scratch database: {exc}")
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    previous = os.environ["DATABASE_URL"]
    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = scratch.render_as_string(hide_password=False)
    engine = create_engine(scratch)
    try:
        command.upgrade(cfg, "head")
        tables = set(inspect(engine).get_table_names())
        assert {"tide_stations", "tide_predictions", "spot_tide_stations"} <= tables
        columns = {c["name"] for c in inspect(engine).get_columns("spot_physical_profiles")}
        assert "wind_profile" in columns

        with Session(engine) as db:
            source = ForecastSource(
                code="noaa_gfs_fields",
                name="test fields",
                provider="noaa",
                is_demo=False,
                attribution="",
                license_notes="",
            )
            db.add(source)
            db.flush()
            run = ForecastRun(
                source_id=source.id,
                run_key="t",
                issued_at=CYCLE,
                started_at=CYCLE,
                status=ForecastRunStatus.SUCCESS,
                details={},
            )
            db.add(run)
            db.flush()
            for variable in ("hs", "swell1_dir"):  # a core and a partition variable
                db.add(
                    MapField(
                        run_id=run.id,
                        source_code=source.code,
                        run_key=run.run_key,
                        cycle_time=CYCLE,
                        valid_time=CYCLE,
                        variable=variable,
                        nx=1,
                        ny=1,
                        lat0=0.0,
                        lon0=0.0,
                        dlat=1.0,
                        dlon=1.0,
                        scale=1.0,
                        offset=0.0,
                        data=b"x",
                    )
                )
            db.commit()

        command.downgrade(cfg, "0005_research_retrieval")
        with engine.connect() as conn:
            kept = conn.execute(text("SELECT variable FROM map_fields")).scalars().all()
        assert kept == ["hs"]
        assert "tide_stations" not in set(inspect(engine).get_table_names())

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM map_fields")).scalar_one() == 1
    finally:
        engine.dispose()
        os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
