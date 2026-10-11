"""Developer accuracy dashboard API, on SYNTHETIC verification rows (labelled as such).

The rows below are made up to exercise the endpoints; they are not observations.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import (
    BuoyObservation,
    BuoyStation,
    ForecastAccuracyMetric,
    MLModelVersion,
    PredictionValidation,
    User,
)
from app.models.enums import MLModelStatus
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration


def _seed(db: Session) -> None:
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    station = BuoyStation(
        code="synthetic:test-buoy",
        provider="synthetic",
        name="Synthetic test buoy",
        latitude=-33.0,
        longitude=115.0,
        metadata_={"paired_spots": [{"slug": "margaret-river", "relation": "test"}]},
    )
    db.add(station)
    db.flush()
    db.add(BuoyObservation(station_id=station.id, observed_at=now, hs_m=2.0, dataset="synthetic"))
    cycle = now - timedelta(days=3)
    for i in range(20):
        valid = cycle + timedelta(hours=3 * i)
        err = 0.1 * ((i % 5) - 2)  # -0.2 .. 0.2 m
        db.add(
            PredictionValidation(
                station_id=station.id,
                source_code="gfswave_bulletin",
                cycle_time=cycle,
                valid_time=valid,
                lead_hours=3.0 * i,
                variable="hs",
                forecast_value=2.0 + err,
                observed_value=2.0,
                error=err,
                observed_kind="spectral_hm0",
            )
        )
    for end_offset, rmse in ((1, 0.30), (0, 0.25)):  # two periods: only the newest is listed
        end = now - timedelta(days=end_offset)
        db.add(
            ForecastAccuracyMetric(
                scope="station",
                scope_key="synthetic:test-buoy",
                source_code="gfswave_bulletin",
                model_version="raw",
                variable="hs",
                lead_bucket_h=24,
                period_start=end - timedelta(days=30),
                period_end=end,
                n=40,
                bias=0.0,
                mae=0.12,
                rmse=rmse,
            )
        )
    db.add(
        MLModelVersion(
            name="offshore_hs_residual",
            version=1,
            model_type="ridge",
            target="log_hs_residual",
            training={"dataset_version": "sha256:abc", "period": ["2026-01-01", "2026-06-30"]},
            decision={"passed": False, "criteria": {"rmse_reduction": {"passed": False}}},
            metrics={"test": {"24": {"rmse": 0.2}}},
            status=MLModelStatus.REJECTED,
        )
    )
    db.commit()


def test_accuracy_dashboard_requires_admin(app_client: ApiClient, db: Session) -> None:
    assert app_client.get("/api/admin/accuracy/overview").status_code == 401
    app_client.register()
    assert app_client.get("/api/admin/accuracy/overview").status_code == 403
    assert app_client.get("/api/admin/accuracy/pairs").status_code == 403


def test_accuracy_dashboard_reads_verification_tables(app_client: ApiClient, db: Session) -> None:
    app_client.register()
    user = db.scalar(select(User).where(User.email == "surfer@example.com"))
    assert user is not None
    user.is_admin = True
    db.commit()

    empty = app_client.get("/api/admin/accuracy/overview").json()
    assert empty["pair_counts"] == [] and empty["models"] == [] and empty["stations"] == []
    assert empty["latest_validation_at"] is None
    assert any("not surf" in n for n in empty["notes"])

    _seed(db)
    ov = app_client.get("/api/admin/accuracy/overview").json()
    assert ov["pair_counts"][0]["n"] == 20 and ov["pair_counts"][0]["variable"] == "hs"
    station = ov["stations"][0]
    assert station["paired_spots"] == ["margaret-river"]
    assert station["observations"] == 1 and station["pairs"] == 20
    assert ov["models"][0]["label"] == "offshore_hs_residual@v1"
    assert ov["models"][0]["status"] == "rejected"
    assert ov["models"][0]["dataset_version"] == "sha256:abc"
    assert ov["models"][0]["test_metrics"] == {"24": {"rmse": 0.2}}
    assert ov["lead_buckets_h"] == [24]

    rows = app_client.get("/api/admin/accuracy/metrics?variable=hs&scope=station").json()
    assert len(rows) == 1 and rows[0]["rmse"] == 0.25  # newest period only
    assert app_client.get("/api/admin/accuracy/metrics?variable=tp").json() == []

    p = app_client.get("/api/admin/accuracy/pairs?variable=hs&lead_max=30").json()
    assert p["unit"] == "m" and p["n_total"] == 11  # leads 0, 3, ..., 30
    assert len(p["sample"]) == 11
    assert sum(b["count"] for b in p["histogram"]) == 11
    assert sum(d["n"] for d in p["daily"]) == 11
    allp = app_client.get("/api/admin/accuracy/pairs?variable=hs").json()
    assert allp["n_total"] == 20
    assert abs(sum(d["bias"] * d["n"] for d in allp["daily"])) < 1e-9  # errors sum to zero
    assert app_client.get("/api/admin/accuracy/pairs?variable=wind").status_code == 422
