"""Algorithm 2.2.0 through the prediction job and the public API: per-hour outputs, separate
confidences, the deprecated combined class, configurable weights, and the optional ML
correction and prediction archive being off by default.

Forecasts are the demo provider's SYNTHETIC values; research assessments are seeded from
the committed evidence (data/surf_spots/research_evidence.json) and, where a test changes
one, the change is SYNTHETIC.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models import (
    ForecastRun,
    MLModelVersion,
    PredictionArchive,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
)
from app.models.enums import MLModelStatus
from app.services.forecasts import ingestion
from app.services.ml import hook
from app.services.pipeline import run_pipeline
from app.services.research.assess import seed_research
from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality import predict
from app.services.surf_quality.forecast_confidence import METHOD
from app.services.surf_quality.scoring import ALGORITHM_VERSION
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration

HOUR_KEYS = (
    "favourable",
    "unfavourable",
    "offshore_class",
    "model_version",
    "forecast_confidence",
    "research_confidence",
)


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def _predictions(db: Session) -> list[SurfQualityPrediction]:
    db.expire_all()
    return list(
        db.scalars(
            select(SurfQualityPrediction).order_by(
                SurfQualityPrediction.spot_id, SurfQualityPrediction.valid_time
            )
        )
    )


def test_the_forecast_and_timeline_carry_the_hour_outputs(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    run_pipeline(db)
    spot = _spot(db, "pipeline")
    stored = {p.valid_time.isoformat(): p for p in _predictions(db) if p.spot_id == spot.id}
    research = db.scalar(
        select(SpotResearchAssessment.overall_class).where(
            SpotResearchAssessment.spot_id == spot.id, SpotResearchAssessment.is_current
        )
    )
    assert research in ("high", "moderate", "low")

    fc = app_client.get("/api/spots/pipeline/forecast?days=3").json()
    assert fc["available"] and fc["algorithm_version"] == ALGORITHM_VERSION == "2.2.0"
    reasons_seen = 0
    for point in fc["points"]:
        assert all(k in point for k in HOUR_KEYS), point.keys()
        pred = stored[point["valid_time"].replace("Z", "+00:00")]
        assert point["favourable"] == pred.components["favourable"]
        assert point["unfavourable"] == pred.components["unfavourable"]
        reasons_seen += len(point["favourable"]) + len(point["unfavourable"])
        assert point["model_version"] == pred.algorithm_version == "2.2.0"
        assert point["research_confidence"] == research
        assert point["forecast_confidence"] == {
            "level": point["forecast_confidence_class"],
            "kind": "heuristic",
            "method": METHOD,
        }
        relation, strength = pred.wind_relation, pred.components["wind_detail"]["strength"]
        expected = "glassy" if relation == "glassy" else f"{strength} {relation}"
        assert point["offshore_class"] == expected
    assert reasons_seen > 0

    tl = app_client.get("/api/spots/pipeline/timeline?days=3").json()
    assert tl["available"]
    for step in tl["steps"]:
        quality = step["quality"]
        assert all(k in quality for k in HOUR_KEYS)
        pred = stored[step["t"].replace("Z", "+00:00")]
        assert quality["favourable"] == pred.components["favourable"]
        assert quality["research_confidence"] == research
        assert quality["forecast_confidence"]["kind"] == "heuristic"
        assert quality["model_version"] == "2.2.0"

    # The summary hours (the spot page's "best upcoming" and "current", the map's
    # conditions) carry the same hour outputs as the hour's forecast point and timeline
    # step, so a summary reads the same conditions sentence before and after the charts
    # load, and on the map as on the spot page.
    detail = app_client.get("/api/spots/pipeline").json()
    snapshot = app_client.get("/api/map/conditions").json()
    on_map = next(s for s in snapshot["spots"] if s["slug"] == "pipeline")
    hours = [h for h in (detail["best_upcoming"], detail["current"], on_map["conditions"]) if h]
    assert detail["best_upcoming"] is not None and hours
    points = app_client.get("/api/spots/pipeline/forecast?days=10").json()["points"]
    for hour in hours:
        assert all(k in hour for k in HOUR_KEYS), hour.keys()
        pred = stored[hour["valid_time"].replace("Z", "+00:00")]
        assert hour["favourable"] == pred.components["favourable"]
        assert hour["unfavourable"] == pred.components["unfavourable"]
        assert hour["model_version"] == pred.algorithm_version
        point = next(p for p in points if p["valid_time"] == hour["valid_time"])
        assert {k: hour[k] for k in HOUR_KEYS} == {k: point[k] for k in HOUR_KEYS}


def test_the_combined_local_confidence_is_gone_from_the_schema() -> None:
    """Spec 20: research and forecast confidence are never combined. The combined "local
    confidence" fields, deprecated while the UI still read them, are removed in 2.0 (the UI
    no longer reads them); the two separate confidences stay."""
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "Conditions",
        "ForecastPoint",
        "StepQualityOut",
        "EventOut",
        "DailySummary",
        "MatchSummary",
        "NotificationOut",
    ):
        props = schemas[name]["properties"]
        assert "local_confidence_class" not in props, name
        assert "local_confidence_reason" not in props, name
    for name in ("Conditions", "ForecastPoint", "StepQualityOut"):
        props = schemas[name]["properties"]
        assert "forecast_confidence" in props and "research_confidence" in props, name
    assert schemas["ForecastConfidenceOut"]["properties"]["kind"]["const"] == "heuristic"


def test_research_and_forecast_confidence_never_move_each_other(
    db: Session, few_spots: list[str]
) -> None:
    """Rescoring with a different research class changes research confidence only; rescoring
    later (the run has gone stale) changes forecast confidence only."""
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.run_id is not None
    run = outcome.run_id
    # Demo data is always Low forecast confidence ("Demo data, not a real forecast."); the
    # SYNTHETIC forecasts are scored here as a real source's so the forecast class varies.
    scored_run = db.get(ForecastRun, run)
    assert scored_run is not None
    scored_run.source.is_demo = False
    db.commit()
    predict.compute_predictions_for_run(db, run)
    first = _predictions(db)
    base_now = first[0].issued_at
    assert base_now is not None

    def classes(rows: list[SurfQualityPrediction]) -> list[tuple[Any, ...]]:
        return [
            (
                r.forecast_id,
                r.forecast_confidence_class,
                (r.confidence_inputs or {}).get("local", {}).get("research_class"),
            )
            for r in rows
        ]

    predict.compute_predictions_for_run(db, run, force=True, now=base_now + timedelta(hours=1))
    before = classes(_predictions(db))
    assert len({b[1] for b in before}) > 1  # the forecast class varies (lead time)
    # SYNTHETIC research change: every current assessment of the run's spots flips class.
    flip = {"high": "low", "moderate": "low", "low": "high"}
    for a in db.scalars(select(SpotResearchAssessment).where(SpotResearchAssessment.is_current)):
        a.overall_class = flip[a.overall_class]
    db.commit()
    predict.compute_predictions_for_run(db, run, force=True, now=base_now + timedelta(hours=1))
    after_research = classes(_predictions(db))
    assert [b[1] for b in before] == [a[1] for a in after_research]  # forecast unchanged
    assert all(b[2] != a[2] for b, a in zip(before, after_research, strict=True))

    # Much later: the run is stale, so forecast classes are capped; research is unchanged.
    predict.compute_predictions_for_run(db, run, force=True, now=base_now + timedelta(days=3))
    after_stale = classes(_predictions(db))
    assert [a[2] for a in after_research] == [s[2] for s in after_stale]  # research unchanged
    assert any(a[1] != s[1] for a, s in zip(after_research, after_stale, strict=True))


def test_scoring_weights_tag_the_version_and_rescore_the_served_runs(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_pipeline(db)
    default_scores = {p.forecast_id: p.score for p in _predictions(db)}
    assert {p.algorithm_version for p in _predictions(db)} == {"2.2.0"}
    assert predict.outdated_authoritative_runs(db) == []
    monkeypatch.setattr(get_settings(), "scoring_weights_json", '{"wind": 0.9, "tide": 0}')
    tagged = predict.current_model_version()
    assert tagged.startswith("2.2.0+w") and len(tagged) <= 16
    outdated = predict.outdated_authoritative_runs(db)
    assert outdated  # a weights change rescores what users see
    result = predict.compute_pending_predictions(db)
    assert result["rescored_runs"] == len(outdated)
    rescored = _predictions(db)
    assert {p.algorithm_version for p in rescored} == {tagged}
    assert any(p.score != default_scores[p.forecast_id] for p in rescored)
    assert all(p.components["weights_used"].get("tide", 0.0) == 0.0 for p in rescored)
    monkeypatch.setattr(get_settings(), "scoring_weights_json", None)
    predict.compute_pending_predictions(db)
    assert {p.forecast_id: p.score for p in _predictions(db)} == default_scores
    assert {p.algorithm_version for p in _predictions(db)} == {"2.2.0"}


def test_ml_and_the_archive_are_off_by_default(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    assert settings.ml_corrections_enabled is False
    assert settings.prediction_archive_enabled is False
    calls: list[int] = []

    def for_run(_db: Session, run: Any) -> None:
        calls.append(run.id)

    monkeypatch.setattr(hook.RunCorrector, "for_run", staticmethod(for_run))
    out = run_pipeline(db)
    assert out
    assert calls == []  # the ML hook is never asked
    assert db.scalar(select(func.count()).select_from(PredictionArchive)) == 0
    assert all(p.ml_model_version is None for p in _predictions(db))
    # On, but no ACTIVE model: still never asked.
    monkeypatch.setattr(settings, "ml_corrections_enabled", True)
    run = db.get(ForecastRun, _predictions(db)[0].run_id)
    assert run is not None
    assert predict.run_corrector(db, run) is None and calls == []
    # On, with an ACTIVE model version: the hook decides (SYNTHETIC registry row).
    db.add(
        MLModelVersion(
            name="synthetic_hs",
            version=1,
            model_type="ridge",
            target="log_hs_ratio",
            status=MLModelStatus.ACTIVE,
        )
    )
    db.commit()
    assert predict.run_corrector(db, run) is None and calls == [run.id]
