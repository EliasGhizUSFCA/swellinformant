"""The prediction job runs without the data libraries (design D8: the refresh Function's
lean install has no pandas, scikit-learn, SciPy, pyarrow, joblib or ecCodes).

A demo run (SYNTHETIC forecasts of the demo provider) is acquired here; a fresh interpreter
in which those packages are absent (tests/unit/test_lean_imports.py's blocker) then runs the
whole prediction job on it, with the optional ML correction and the prediction archive both
switched on: no ACTIVE model exists, so the ML code is never imported.
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import PredictionArchive, SurfQualityPrediction
from app.services.forecasts import ingestion
from tests.unit.test_lean_imports import (
    BACKEND,
    CELERY,
    DATA_LIBRARIES,
    SERVER_EXTRAS,
    assert_imported,
    import_without,
)

pytestmark = pytest.mark.integration

RUN_THE_JOB = """
from app.core.database import SessionLocal
from app.services.surf_quality.predict import compute_pending_predictions

with SessionLocal() as db:
    result = compute_pending_predictions(db)
assert result["predictions"] > 0 and result["archived"] > 0, result
"""


def test_the_prediction_job_runs_without_the_data_libraries(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.status == "success", outcome
    db.commit()
    monkeypatch.setenv("ML_CORRECTIONS_ENABLED", "true")
    monkeypatch.setenv("PREDICTION_ARCHIVE_ENABLED", "true")
    assert os.environ["DATABASE_URL"] and BACKEND.is_dir() and sys.executable
    done = import_without(
        ["app.services.surf_quality.predict"],
        CELERY + DATA_LIBRARIES + SERVER_EXTRAS,
        then=RUN_THE_JOB,
    )
    assert_imported(done)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SurfQualityPrediction)) > 0
    assert db.scalar(select(func.count()).select_from(PredictionArchive)) > 0
