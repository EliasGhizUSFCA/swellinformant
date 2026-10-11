"""ECMWF open-data provider through Job A (``acquire_from_provider``) into PostgreSQL.

The bucket is the SYNTHETIC fake from the unit tests (no network): every GRIB2 message
is generated with ecCodes and every value is made up for the test.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ForecastRun, ForecastSource, WaveForecast
from app.services.forecasts.ecmwf import EcmwfIfsProvider
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.forecasts.registry import ECMWF_IFS_CODE
from tests.unit.test_ecmwf_provider import CYCLE, FakeBucket, Scenario, settings

STEPS = [0, 3, 6]
NOW = CYCLE + timedelta(hours=9)  # 00Z cycle is due (ECMWF_AVAILABILITY_LAG_HOURS = 8)


def make_provider(bucket: FakeBucket) -> EcmwfIfsProvider:
    return EcmwfIfsProvider(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(bucket)),
        steps=STEPS,
        attempts=2,
        backoff_seconds=0,
        request_interval_seconds=0,
    )


def test_ecmwf_run_is_stored_with_period_kind_extras_and_no_partitions(
    db: Session, few_spots: list[str]
) -> None:
    bucket = FakeBucket()
    for step in STEPS:
        bucket.add_step(CYCLE, step, Scenario())
    provider = make_provider(bucket)

    outcome = acquire_from_provider(db, provider, now=NOW)
    assert outcome.status == "success", outcome.as_dict()
    assert outcome.run_key == "ecmwf-ifs:2026101000"
    assert outcome.records == len(few_spots) * len(STEPS)

    source = db.scalar(select(ForecastSource).where(ForecastSource.code == ECMWF_IFS_CODE))
    assert source is not None and source.family == "ecmwf_ifs" and source.kind == "deterministic"
    assert "CC BY 4.0" in source.attribution and not source.is_demo
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None and run.issued_at == CYCLE and run.wave_model_run_at == CYCLE
    assert run.request_count == provider.last_fetch_stats["requests"] > 0
    assert run.details["metadata_source"] == "ecmwf_index_probe"

    rows = db.scalars(select(WaveForecast).where(WaveForecast.run_id == run.id)).all()
    assert len(rows) == outcome.records
    for row in rows:
        assert row.wave_period_kind == "tp"
        assert row.sig_wave_height_m == pytest.approx(1.0, abs=1e-3)
        assert row.wave_period_s == pytest.approx(14.0, abs=0.01)
        assert row.primary_swell_height_m is None and row.wind_wave_height_m is None
        assert "primary_swell_height_m" in row.missing_fields
        assert "sig_wave_height_m" not in row.missing_fields
        assert row.wind_speed_kmh == pytest.approx(18.0, abs=0.05)
        assert row.pressure_msl_hpa == pytest.approx(1013.25, abs=0.01)
        assert set(row.extra["hs_band_m"]) == {
            "10-12s",
            "12-14s",
            "14-17s",
            "17-21s",
            "21-25s",
            "25-30s",
        }
        assert row.extra["hs_ge_12s_m"] == pytest.approx(0.885, abs=2e-3)
        assert row.extra["wave_cell"]["distance_km"] <= 400
        # Waves at the offshore point, wind at the break: both cells are recorded.
        assert "wind_cell" in row.extra
        if row.valid_time == CYCLE:
            assert row.wind_gust_kmh is None  # no gust window at step 0
        else:
            assert row.wind_gust_kmh == pytest.approx(36.0, abs=0.05)

    again = acquire_from_provider(db, provider, now=NOW)
    assert again.status == "skipped" and again.run_id == outcome.run_id


def test_missing_step_makes_the_run_partial_and_is_reported(
    db: Session, few_spots: list[str]
) -> None:
    bucket = FakeBucket()
    bucket.add_step(CYCLE, 0, Scenario())
    bucket.add_step(CYCLE, 3, Scenario(), streams=("oper",))  # wave file missing
    bucket.add_step(CYCLE, 6, Scenario())

    outcome = acquire_from_provider(db, make_provider(bucket), now=NOW)
    assert outcome.status == "partial", outcome.as_dict()
    assert outcome.records == len(few_spots) * 2
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None
    assert sorted(run.details["spot_errors"]) == sorted(few_spots)
    assert all(e.startswith("incomplete:") for e in run.details["spot_errors"].values())
    assert any(w.startswith("+3h wave: not published") for w in run.details["warnings"])
    assert len(run.details["spot_ids"]) == len(few_spots)  # the spots are still usable


def test_unpublished_cycle_fails_the_acquisition(db: Session, few_spots: list[str]) -> None:
    outcome = acquire_from_provider(db, make_provider(FakeBucket()), now=NOW)
    assert outcome.status == "failed" and outcome.run_key is None
    assert "no complete 00/12Z cycle" in outcome.message
