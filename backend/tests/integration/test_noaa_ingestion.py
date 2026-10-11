"""NOAA GFS direct ingestion end to end into wave_forecasts, from SYNTHETIC GRIB2 files.

The bucket is an httpx.MockTransport serving made-up fields (tests.synthetic_gfs); the
real NODD bucket is only used by the documented live check, never by tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ForecastRun, ForecastSource, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.forecasts.noaa import NoaaGfsProvider
from tests.synthetic_gfs import SyntheticGfs, noaa_settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # nominal cycle with a 5 h lag: 18Z
CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
KEEP = ["pipeline", "jeffreys-bay", "thurso-east"]  # global.0p16, gsouth.0p25, global.0p25


def test_acquire_stores_noaa_gfs_wave_forecasts(db: Session) -> None:
    db.execute(text("UPDATE surf_spots SET is_active = (slug = ANY(:keep))"), {"keep": KEEP})
    db.commit()
    world = SyntheticGfs()
    # Thurso's (interpolated) global.0p25 file holds no valid wave cell near the spot.
    world.add_cycle(CYCLE, [0, 12, 24], wave_valid={"global.0p25": lambda lat, lon: lat > 89})
    provider = NoaaGfsProvider(noaa_settings(), client=world.client(), backoff_seconds=0)

    outcome = acquire_from_provider(db, provider, now=NOW)

    assert outcome.status == ForecastRunStatus.PARTIAL.value
    assert outcome.run_key == "noaa-gfs:2026101018"
    assert outcome.records == 6 and outcome.spot_errors == 1
    assert "no valid GFS-Wave global.0p25 cell" in outcome.errors["thurso-east"]

    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "noaa_gfs_direct"))
    assert source is not None and source.family == "ncep_gfs" and source.provider == "noaa_nodd"
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None
    assert run.issued_at == run.wave_model_run_at == run.atmosphere_model_run_at == CYCLE
    assert run.details["metadata_source"] == "noaa-s3-idx"
    # 3 steps x (atmosphere index + 3 ranges, + LAND once) + 3 wave grids x (index + range).
    assert run.request_count == 31
    assert run.details["warnings"][0].startswith("downloaded ")
    assert run.details["spot_errors"].keys() == {"thurso-east"}

    slugs = {s.id: s.slug for s in db.scalars(select(SurfSpot)).all()}
    rows = db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == run.id)
        .order_by(WaveForecast.spot_id, WaveForecast.valid_time)
    ).all()
    assert sorted({slugs[r.spot_id] for r in rows}) == ["jeffreys-bay", "pipeline"]
    by_slug: dict[str, list[WaveForecast]] = {}
    for r in rows:
        by_slug.setdefault(slugs[r.spot_id], []).append(r)
    pipeline = by_slug["pipeline"]
    assert [r.valid_time for r in pipeline] == [CYCLE + timedelta(hours=h) for h in (0, 12, 24)]
    assert all(r.wave_period_kind == "tp" for r in rows)
    assert all(r.tertiary_swell_height_m == 0.3 for r in rows)
    assert all(r.tertiary_swell_period_s == 18.0 for r in rows)
    assert pipeline[0].sig_wave_height_m == 2.0 and pipeline[0].wind_speed_kmh == 18.0
    assert pipeline[0].pressure_msl_hpa == 1013.25 and pipeline[0].sea_level_m is None
    assert pipeline[0].missing_fields == ["sea_level_m"]
    # The chosen cells are on the first valid time of each spot only.
    assert pipeline[0].extra["grid"] == "global.0p16"
    assert pipeline[0].extra["wave_cell"]["anchor"] == "wave_point"
    assert pipeline[0].extra["wind_cell"]["grid"] == "pgrb2.0p25"
    assert by_slug["jeffreys-bay"][0].extra["grid"] == "gsouth.0p25"
    assert all(r.extra == {} for r in pipeline[1:])

    # Same upstream run again: idempotent, nothing re-downloaded.
    before = len(world.requests)
    again = acquire_from_provider(db, provider, now=NOW + timedelta(minutes=30))
    assert again.status == "skipped" and again.run_id == run.id
    assert all(r.method == "HEAD" for r in world.requests[before:])
