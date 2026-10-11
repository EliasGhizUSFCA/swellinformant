"""An ACTIVE ML correction inside the forecast pipeline (ml.hook), on SYNTHETIC data.

The correction is fitted on a synthetic over-forecasting case (tests.synthetic_waves); the
forecast runs come from the synthetic FakeProvider of test_blend. Nothing here is real.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    BuoyStation,
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    WaveForecast,
)
from app.models.enums import MLModelStatus
from app.services.forecasts import blend
from app.services.ml import registry
from app.services.ml.apply import clear_correction_cache
from app.services.ml.dataset import assemble_frame
from app.services.ml.features import FEATURE_NAMES, OBSERVATION_FEATURES
from app.services.ml.models import fit_correction
from app.services.surf_quality.predict import compute_pending_predictions
from tests.integration.test_blend import (  # noqa: F401 (two_families is a fixture)
    NOW,
    FakeProvider,
    _ingest,
    _spot_id,
    two_families,
)
from tests.synthetic_waves import frames_for_assembly, make_case

pytestmark = pytest.mark.integration

PASSED = {"passed_all": True, "failed": [], "criteria": [], "n_test": 1000}


@pytest.fixture
def active_correction(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """A SYNTHETIC over-forecast correction, active for ncep_gfs at Pipeline only."""
    monkeypatch.setattr(get_settings(), "ml_artifact_dir", str(tmp_path / "artifacts"))
    monkeypatch.setattr(get_settings(), "ml_corrections_enabled", True)
    clear_correction_cache()
    case = make_case("bias", n_stations=1, days=30, lead_step=12, max_lead=120, seed=8)
    fc, obs, stations = frames_for_assembly(case)
    features = tuple(n for n in FEATURE_NAMES if n not in OBSERVATION_FEATURES)
    model = fit_correction("linear", {"alpha": 1.0}, assemble_frame(fc, obs, stations), features)
    db.add(
        BuoyStation(
            code="synthetic:hook-buoy",
            provider="synthetic",
            name="Synthetic hook buoy",
            latitude=21.7,
            longitude=-158.1,
            metadata_={"paired_spots": [{"slug": "pipeline", "relation": "offshore_reference"}]},
        )
    )
    version = registry.register_version(
        db,
        name=registry.DEFAULT_MODEL_NAME,
        model=model,
        experiment=None,
        metrics={},
        decision=PASSED,
        applicability={
            "station_codes": ["synthetic:hook-buoy"],
            "spot_slugs": ["pipeline"],
            "families": ["ncep_gfs"],
            "sources": ["synthetic"],
            "lead_min_h": model.lead_min,
            "lead_max_h": model.lead_max,
            "hs_kinds": ["spectral_hm0"],
        },
        training={},
        hyperparameters={"alpha": 1.0},
        status=MLModelStatus.CANDIDATE,
    )
    db.commit()
    registry.activate(db, version.id)
    db.commit()
    yield version.label
    clear_correction_cache()


def _preds(db: Session, run_id: int, spot_id: int) -> list[SurfQualityPrediction]:
    return list(
        db.scalars(
            select(SurfQualityPrediction)
            .where(SurfQualityPrediction.run_id == run_id, SurfQualityPrediction.spot_id == spot_id)
            .order_by(SurfQualityPrediction.valid_time)
        )
    )


def test_single_source_run_is_corrected_where_the_correction_applies(
    db: Session, few_spots: list[str], active_correction: str
) -> None:
    gfs = FakeProvider("t_gfs", "ncep_gfs", 2.0, issued=NOW - timedelta(hours=6))
    ec = FakeProvider("t_ecmwf", "ecmwf_ifs", 2.0, issued=NOW - timedelta(hours=6))
    _ingest(db, gfs, ec)
    compute_pending_predictions(db)
    pipeline, jbay = _spot_id(db, "pipeline"), _spot_id(db, "jeffreys-bay")
    runs = {
        code: run
        for code, run in db.execute(
            select(ForecastSource.code, ForecastRun).join(
                ForecastRun, ForecastRun.source_id == ForecastSource.id
            )
        ).all()
    }

    corrected = [p for p in _preds(db, runs["t_gfs"].id, pipeline) if p.ml_model_version]
    assert corrected, "no Pipeline hour was corrected"
    assert all(p.ml_model_version == active_correction for p in corrected)
    info = corrected[0].components["ml_correction"]
    assert info["applied"] and info["station"] == "synthetic:hook-buoy"
    assert 0.5 <= info["factor"] < 1.0  # the synthetic model over-forecasts
    # Lead 0 is outside the trained lead range: raw.
    assert _preds(db, runs["t_gfs"].id, pipeline)[0].ml_model_version is None
    # Not a paired spot, and not the trained family: never corrected.
    assert not any(p.ml_model_version for p in _preds(db, runs["t_gfs"].id, jbay))
    assert not any(p.ml_model_version for p in _preds(db, runs["t_ecmwf"].id, pipeline))
    # The corrected offshore height reaches the breaking-height model: the same raw sea
    # state at the same hour gives a lower estimate where it was corrected.
    ec_same_hour = {p.valid_time: p for p in _preds(db, runs["t_ecmwf"].id, pipeline)}
    t = corrected[0].valid_time
    if t in ec_same_hour:
        assert corrected[0].breaking_height_mid_ft < ec_same_hour[t].breaking_height_mid_ft


@pytest.mark.usefixtures("two_families")
def test_blend_corrects_the_matching_member_before_combining(
    db: Session, active_correction: str
) -> None:
    gfs = FakeProvider("t_gfs", "ncep_gfs", 2.0, issued=NOW - timedelta(hours=6))
    ec = FakeProvider("t_ecmwf", "ecmwf_ifs", 2.0, issued=NOW - timedelta(hours=6))
    copy = FakeProvider("t_gfs_copy", "ncep_gfs", 2.0, issued=NOW - timedelta(hours=6))
    _ingest(db, gfs, ec, copy)
    out = blend.build_blend(db, NOW)
    assert out.status == "success"
    pipeline = _spot_id(db, "pipeline")
    rows = db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == out.run_id, WaveForecast.spot_id == pipeline)
        .order_by(WaveForecast.valid_time)
    ).all()
    hit = [r for r in rows if "ml_corrections" in r.extra]
    assert hit, "no blended hour used a corrected member"
    assert set(hit[0].extra["ml_corrections"]) == {"t_gfs"}  # never the ECMWF member or the copy
    # Equal raw members (2.0 m): the blend of a corrected and an uncorrected member is lower.
    assert hit[0].sig_wave_height_m is not None and hit[0].sig_wave_height_m < 2.0
    assert rows[0].extra.get("ml_corrections") is None  # lead 0: raw members

    # The blend's predictions record the correction once; they are not corrected again.
    compute_pending_predictions(db)
    preds = {p.valid_time: p for p in _preds(db, out.run_id, pipeline)}
    p = preds[hit[0].valid_time]
    assert p.ml_model_version == active_correction
    assert "t_gfs" in p.components["ml_correction"]
