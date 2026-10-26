"""GEFS-Wave ensemble job against PostgreSQL: run bookkeeping and isolation from the forecast
pipeline. GRIB data is SYNTHETIC (tests/unit/gefs_synthetic)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import (
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    SurfSpot,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.forecasts.ensemble import GefsWaveEnsembleProvider, acquire_ensemble
from app.services.forecasts.ingestion import acquire_all
from app.services.forecasts.queries import candidate_runs, latest_runs_by_spot
from app.services.forecasts.registry import GEFS_WAVE_CODE, SOURCE_CODES, configured_source_codes
from app.services.surf_quality.predict import compute_pending_predictions
from tests.unit.gefs_synthetic import BASE_URL, CYCLE, GLOBAL_1DEG, FakeBucket, publish_cycle

pytestmark = pytest.mark.integration

# Shortly after the synthetic 12Z cycle is complete (nominal cycle = 12Z with a 7.5 h lag).
NOW = CYCLE + timedelta(hours=8)


@pytest.fixture
def gefs_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    s = get_settings()
    for key, value in {
        "gefs_enabled": True,
        "noaa_gefs_base_url": BASE_URL,
        "gefs_max_lead_hours": 24,
        "gefs_step_hours": 12,
        "gefs_availability_lag_hours": 7.5,
        "grib_max_cell_distance_km": 150.0,  # the synthetic grid is 1 degree
        "grib_download_workers": 2,
    }.items():
        monkeypatch.setattr(s, key, value)
    return s


@pytest.fixture
def bucket() -> FakeBucket:
    b = FakeBucket()
    publish_cycle(b, GLOBAL_1DEG, [0, 12, 24])
    return b


def provider_for(
    bucket: FakeBucket, settings: Settings, members: bool = False
) -> GefsWaveEnsembleProvider:
    return GefsWaveEnsembleProvider(
        settings, client=bucket.client(), members=members, backoff_seconds=0
    )


def _gefs_run(db: Session) -> ForecastRun:
    return db.scalars(
        select(ForecastRun).join(ForecastSource).where(ForecastSource.code == GEFS_WAVE_CODE)
    ).one()


def test_disabled_by_default_and_touches_nothing(db: Session) -> None:
    assert get_settings().gefs_enabled is False
    assert acquire_ensemble(db) == {"skipped": "GEFS_ENABLED is false"}
    assert db.scalar(select(func.count()).select_from(ForecastSource)) == 0


def test_acquire_writes_ensemble_stats_only(
    db: Session, few_spots: list[str], gefs_settings: Settings, bucket: FakeBucket
) -> None:
    out = acquire_ensemble(db, now=NOW, provider=provider_for(bucket, gefs_settings))
    assert out["status"] == "success", out
    assert out["run_key"] == "gefs-wave:2026101012"
    assert out["records"] == len(few_spots) * 3 and out["spots"] == len(few_spots)
    # GEFS_STAT_FIELDS default (HTSGW,PERPW): swell partitions are not read, so there is no
    # partition-order check (tests/unit/test_ensemble.py covers it with SWELL enabled).
    assert "partition_order" not in out["checks"]

    run = _gefs_run(db)
    db.refresh(run)
    spot_ids = set(db.scalars(select(SurfSpot.id).where(SurfSpot.slug.in_(few_spots))))
    assert set(run.details["spot_ids"]) == spot_ids
    assert run.record_count == len(few_spots) * 3
    assert run.request_count == out["requests"] == len(bucket.log)  # GETs + HEAD probe
    assert run.predictions_computed_at is not None
    assert run.status == ForecastRunStatus.SUCCESS
    assert run.details["bytes_downloaded"] > 0 and run.details["steps_read"] == 3
    assert set(run.details["cells"]) == set(few_spots)
    assert run.source.kind == "ensemble" and run.source.family == "ncep_gefs"
    assert db.scalar(select(func.count()).select_from(ForecastEnsembleStat)) == len(few_spots) * 3
    assert db.scalar(select(func.count()).select_from(WaveForecast)) == 0

    row = db.scalars(
        select(ForecastEnsembleStat).where(ForecastEnsembleStat.lead_hours == 12)
    ).first()
    assert row is not None and row.valid_time == CYCLE + timedelta(hours=12)
    assert row.hs_mean_m == pytest.approx(2.5, abs=1e-3)
    assert row.tp_mean_s == pytest.approx(12.0, abs=1e-3) and row.tp_spread_s is not None
    assert row.wind_speed_mean_kmh is None and row.swell1_height_mean_m is None  # not read
    assert row.members == 31 and row.extra["period_kind"] == "tp"
    assert run.details["stat_fields"] == ["HTSGW", "PERPW"]
    assert run.details["stats"]["bytes_downloaded"] == run.details["bytes_downloaded"]
    assert run.details["model_runs"][0]["init_time"] == CYCLE.isoformat()


def test_rerun_is_idempotent_and_force_refetches(
    db: Session, few_spots: list[str], gefs_settings: Settings, bucket: FakeBucket
) -> None:
    provider = provider_for(bucket, gefs_settings)
    assert acquire_ensemble(db, now=NOW, provider=provider)["status"] == "success"
    gets = len(bucket.gets())
    again = acquire_ensemble(db, now=NOW, provider=provider)
    assert again["status"] == "skipped" and again["message"] == "run already ingested"
    assert len(bucket.gets()) == gets  # only the cheap HEAD probe ran
    forced = acquire_ensemble(db, now=NOW, force=True, provider=provider)
    assert forced["status"] == "success" and forced["run_id"] == again["run_id"]
    assert db.scalar(select(func.count()).select_from(ForecastEnsembleStat)) == len(few_spots) * 3
    assert db.scalar(select(func.count()).select_from(ForecastRun)) == 1


def test_stuck_and_failed_runs_are_retried(
    db: Session, few_spots: list[str], gefs_settings: Settings, bucket: FakeBucket
) -> None:
    provider = provider_for(bucket, gefs_settings)
    acquire_ensemble(db, now=NOW, provider=provider)
    run = _gefs_run(db)

    # started_at is the real start time of the provider's run, so "stuck" is judged on the
    # real clock, not on the cycle-selecting ``now``.
    run.status, run.started_at = ForecastRunStatus.RUNNING, utcnow() - timedelta(minutes=10)
    db.commit()
    busy = acquire_ensemble(db, now=NOW, provider=provider)
    assert busy["status"] == "skipped" and busy["message"] == "run in progress"

    run.started_at = utcnow() - timedelta(hours=2)  # older than STUCK_RUN_AFTER: dead worker
    db.commit()
    assert acquire_ensemble(db, now=NOW, provider=provider)["status"] == "success"

    run.status = ForecastRunStatus.FAILED
    db.commit()
    assert acquire_ensemble(db, now=NOW, provider=provider)["status"] == "success"
    db.refresh(run)
    assert run.status == ForecastRunStatus.SUCCESS and run.error_message is None


def test_partial_and_failed_runs_report_what_happened(
    db: Session, few_spots: list[str], gefs_settings: Settings, bucket: FakeBucket
) -> None:
    bucket.status[bucket.key(CYCLE, "spread", 12) + ".idx"] = 404
    out = acquire_ensemble(db, now=NOW, provider=provider_for(bucket, gefs_settings))
    assert out["status"] == "partial" and out["steps_failed"] == ["f012"]
    assert out["records"] == len(few_spots) * 2
    run = _gefs_run(db)
    assert "f012" in run.details["steps_failed"]
    assert any("1 of 3 steps unreadable" in w for w in run.details["warnings"])

    for f in (0, 12, 24):  # the cycle exists (.idx) but its GRIB data keeps failing
        for kind in ("mean", "spread"):
            bucket.status[bucket.key(CYCLE, kind, f)] = 503
    failed = acquire_ensemble(db, now=NOW, force=True, provider=provider_for(bucket, gefs_settings))
    assert failed["status"] == "failed" and failed["retryable"] is True
    db.refresh(run)
    assert run.status == ForecastRunStatus.FAILED and "none of the first" in (
        run.error_message or ""
    )
    assert db.scalar(select(func.count()).select_from(ForecastEnsembleStat)) == len(few_spots) * 2


def test_member_mode_stores_quantiles(
    db: Session, few_spots: list[str], gefs_settings: Settings
) -> None:
    bucket = FakeBucket()
    publish_cycle(bucket, GLOBAL_1DEG, [0, 12, 24], member_hs=lambda m, f: 2.0 + 0.01 * int(m[1:]))
    out = acquire_ensemble(
        db, now=NOW, members=True, provider=provider_for(bucket, gefs_settings, True)
    )
    assert out["status"] == "success" and out["members_mode"] is True
    row = db.scalars(select(ForecastEnsembleStat)).first()
    assert row is not None
    assert row.extra["hs_members"] == 31
    assert row.extra["hs_p10_m"] < row.extra["hs_p50_m"] < row.extra["hs_p90_m"]


def test_ensemble_runs_are_never_authoritative_or_scored(
    db: Session, few_spots: list[str], gefs_settings: Settings, bucket: FakeBucket
) -> None:
    acquire_all(db)  # the configured (demo) forecast source
    acquire_ensemble(db, now=NOW, provider=provider_for(bucket, gefs_settings))
    gefs = _gefs_run(db)

    pending = compute_pending_predictions(db)
    assert pending["runs"] == 1  # the demo run only; the ensemble run is never pending
    assert (
        db.scalar(
            select(func.count())
            .select_from(SurfQualityPrediction)
            .where(SurfQualityPrediction.run_id == gefs.id)
        )
        == 0
    )

    for require in (True, False):
        chosen = latest_runs_by_spot(db, require_predictions=require)
        assert chosen and all(r.source.code != GEFS_WAVE_CODE for r in chosen.values())
        assert gefs.id not in {r.id for r in candidate_runs(db, require)}
    # GEFS cannot be configured as an authoritative forecast source at all.
    assert GEFS_WAVE_CODE not in SOURCE_CODES.values()
    assert GEFS_WAVE_CODE not in configured_source_codes()
    with pytest.raises(ValidationError):
        Settings(forecast_providers="noaa_gefs_wave")
