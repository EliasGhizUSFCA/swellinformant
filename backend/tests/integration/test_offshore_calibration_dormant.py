"""The calibrated offshore range (``ndbc-offshore-v1``) is not wired into the prediction job.

docs/decisions.md ADR-018b and docs/ml-evaluation.md 12.2: the prediction job does not pass
an ``OffshoreContext`` yet, so no stored prediction carries the calibrated label until a
pre-registered tail check decides otherwise. This test fails when the wiring appears, so
that change comes with its own decision and documentation. The forecast run is the
SYNTHETIC ``FakeProvider`` of test_blend; nothing here is real.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ForecastRun, SurfQualityPrediction
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.surf_quality.uncertainty import OFFSHORE_CALIBRATION, OffshoreContext
from tests.integration.test_blend import (  # noqa: F401 (two_families is a fixture)
    NOW,
    FakeProvider,
    _ingest,
    _spot_id,
    two_families,
)

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("two_families")
def test_no_prediction_carries_the_calibrated_offshore_range(db: Session) -> None:
    db.execute(text("UPDATE surf_spots SET is_active = true WHERE slug = 'trestles'"))
    db.commit()
    _ingest(db, FakeProvider("t_gfs", "ncep_gfs", 2.0, issued=NOW - timedelta(hours=6)))
    compute_pending_predictions(db)
    run = db.scalar(select(ForecastRun).where(ForecastRun.run_key.like("t_gfs:%")))
    assert run is not None
    preds = list(
        db.scalars(
            select(SurfQualityPrediction).where(
                SurfQualityPrediction.run_id == run.id,
                SurfQualityPrediction.spot_id == _spot_id(db, "trestles"),
            )
        )
    )
    # Not vacuous: raw single-family GFS-Wave hours at a listed spot, spread from the lead
    # table, inside 3-240 h, i.e. hours where the calibration would apply with a context.
    context = OffshoreContext("trestles", ("ncep_gfs",), ml_corrected=False)
    eligible = [
        p
        for p in preds
        if OFFSHORE_CALIBRATION.applies(
            context, p.components["uncertainty"]["source"], float(p.lead_hours or 0.0)
        )
    ]
    assert len(eligible) > 24, len(eligible)
    for p in preds:
        unc = p.components["uncertainty"]
        assert unc["offshore_kind"] != "calibrated" and "offshore_calibration" not in unc
        assert p.uncertainty_kind != "calibrated"
