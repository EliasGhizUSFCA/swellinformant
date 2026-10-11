"""Forecast confidence v2 (forecast-v1) end to end: per-source staleness in the blend,
persistence on predictions and the archive, API fields, events, alerts and demo data.

Every forecast value here is SYNTHETIC (``SyntheticProvider`` below, or the demo
provider); the GEFS statistics and research assessments inserted here are SYNTHETIC rows
labelled as such. Nothing is real data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    ForecastAccuracyMetric,
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    Notification,
    PredictionArchive,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
)
from app.models.enums import ForecastRunStatus
from app.models.research import RESEARCH_VARIABLES
from app.services.forecasts import blend, queries
from app.services.forecasts.base import (
    FetchResult,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
)
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.forecasts.registry import ECMWF_IFS_CODE, GEFS_WAVE_CODE, NOAA_GFS_CODE
from app.services.spot_views import map_snapshot, spot_forecast, spot_summaries, upcoming_events
from app.services.surf_quality import forecast_confidence as fc
from app.services.surf_quality.confidence_context import VerificationIndex, nearest_lead_bucket
from app.services.surf_quality.predict import (
    compute_pending_predictions,
    compute_predictions_for_run,
)
from app.services.verification.metrics import LEAD_BUCKETS_H
from tests.conftest import ApiClient
from tests.integration.test_pipeline import make_search, make_user, simulate

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 11, 8, 30, tzinfo=UTC)
GFS_ISSUED = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)  # 8.5 h old at NOW
ECMWF_ISSUED = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)  # 20.5 h old at NOW
CLASSES = {fc.HIGH, fc.MODERATE, fc.LOW}


class SyntheticProvider(ForecastProvider):
    """SYNTHETIC constant sea state for every active spot, 3-hourly to ``hours`` (tests)."""

    def __init__(self, code: str, family: str, hs: float, issued: datetime, partitions: bool):
        self.code, self.family, self.hs, self.issued = code, family, hs, issued
        self.partitions = partitions

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            self.code,
            f"SYNTHETIC {self.code}",
            "test",
            "w",
            "a",
            False,
            "test data",
            "",
            self.family,
        )

    def current_run(self, now: datetime) -> RunInfo:
        return RunInfo(f"{self.code}:{self.issued:%Y%m%d%H}", self.issued, self.issued)

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        out = FetchResult()
        for p in spots:
            recs = []
            for h in range(0, 241, 3):
                v: dict[str, float | None] = {
                    "sig_wave_height_m": self.hs,
                    "wave_period_s": 14.0,
                    "wave_direction_deg": 300.0,
                    "wind_speed_kmh": 8.0,
                    "wind_direction_deg": 130.0,
                }
                if self.partitions:
                    v |= {
                        "primary_swell_height_m": self.hs * 0.9,
                        "primary_swell_period_s": 14.0,
                        "primary_swell_direction_deg": 300.0,
                    }
                recs.append(ForecastRecord(run.issued_at + timedelta(hours=h), v, period_kind="tp"))
            out.records[p.spot_id] = recs
        return out


@pytest.fixture
def live_codes(db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The real direct-ingester source codes, so their per-source staleness applies."""
    codes = [NOAA_GFS_CODE, ECMWF_IFS_CODE]
    monkeypatch.setattr(blend, "configured_source_codes", lambda: codes)
    monkeypatch.setattr(queries, "configured_source_codes", lambda: codes)
    db.execute(delete(SpotResearchAssessment))  # the session seed may have left some
    db.commit()
    return codes


def _ingest(db: Session, ecmwf_hs: float = 2.1) -> None:
    for prio, p in enumerate(
        (
            SyntheticProvider(NOAA_GFS_CODE, "ncep_gfs", 2.0, GFS_ISSUED, True),
            SyntheticProvider(ECMWF_IFS_CODE, "ecmwf_ifs", ecmwf_hs, ECMWF_ISSUED, False),
        )
    ):
        out = acquire_from_provider(db, p, now=NOW, priority=prio)
        assert out.status == "success", out


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def _synthetic_gefs(db: Session, rel_spread: float = 0.08) -> None:
    """SYNTHETIC GEFS-Wave statistics (mean 2.0 m) at every active spot, 6-hourly."""
    source = ForecastSource(
        code=GEFS_WAVE_CODE,
        name="SYNTHETIC GEFS",
        provider="test",
        family="ncep_gefs",
        kind="ensemble",
    )
    db.add(source)
    db.flush()
    issued = GFS_ISSUED - timedelta(hours=6)
    run = ForecastRun(
        source_id=source.id,
        run_key="gefs-wave:synthetic",
        issued_at=issued,
        status=ForecastRunStatus.SUCCESS,
        details={},
        predictions_computed_at=NOW,
    )
    db.add(run)
    db.flush()
    for spot in db.scalars(select(SurfSpot).where(SurfSpot.is_active)):
        for h in range(0, 247, 6):
            db.add(
                ForecastEnsembleStat(
                    run_id=run.id,
                    spot_id=spot.id,
                    valid_time=issued + timedelta(hours=h),
                    lead_hours=float(h),
                    members=31,
                    hs_mean_m=2.0,
                    hs_spread_m=2.0 * rel_spread,
                    extra={"synthetic": True},
                )
            )
    db.commit()


def _synthetic_research(db: Session, slug: str, cls: str) -> SpotResearchAssessment:
    """A SYNTHETIC current research assessment with every variable at ``cls``."""
    spot = _spot(db, slug)
    for row in db.scalars(
        select(SpotResearchAssessment).where(
            SpotResearchAssessment.spot_id == spot.id, SpotResearchAssessment.is_current
        )
    ):
        row.is_current = False
    db.flush()
    row = SpotResearchAssessment(
        spot_id=spot.id,
        methodology_version="synthetic-test",
        overall_class=cls,
        per_variable={v: {"class": cls, "summary": "SYNTHETIC"} for v in RESEARCH_VARIABLES},
        gaps=[],
        summary="SYNTHETIC assessment for tests.",
        evidence_fingerprint="0" * 64,
        is_current=True,
    )
    db.add(row)
    db.commit()
    return row


def _blend_run(db: Session) -> ForecastRun:
    run = db.scalar(
        select(ForecastRun)
        .join(ForecastSource)
        .where(
            ForecastSource.code == blend.BLEND_CODE, ForecastRun.status == ForecastRunStatus.SUCCESS
        )
    )
    assert run is not None
    return run


def _preds(db: Session, run_id: int, slug: str) -> list[SurfQualityPrediction]:
    return list(
        db.scalars(
            select(SurfQualityPrediction)
            .where(
                SurfQualityPrediction.run_id == run_id,
                SurfQualityPrediction.spot_id == _spot(db, slug).id,
            )
            .order_by(SurfQualityPrediction.valid_time)
        ).all()
    )


# --------------------------------------------------------------------------- staleness
def test_ecmwf_stays_in_the_blend_at_the_hour_it_used_to_drop_out(
    db: Session, live_codes: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """08:30 UTC: the newest ECMWF run (12Z the day before) is 20.5 h old. The 00Z cycle is
    published ~08:00 and fetched at the next 3-hourly acquisition, so every day ECMWF used
    to drop out of the blend here under the single 18 h rule."""
    _ingest(db)
    with monkeypatch.context() as m:  # the old rule: one FORECAST_STALE_HOURS for all
        m.setattr(queries, "stale_after_hours", lambda code, settings=None: 18.0)
        members, _ = blend.select_members(db, NOW)
        assert [x.code for x in members] == [NOAA_GFS_CODE]
        assert blend.build_blend(db, NOW).status == "skipped"
    members, _ = blend.select_members(db, NOW)
    assert [x.code for x in members] == [NOAA_GFS_CODE, ECMWF_IFS_CODE]
    out = blend.build_blend(db, NOW)
    assert out.status == "success"
    run = db.get(ForecastRun, out.run_id)
    assert run is not None and not queries.is_stale(run, NOW)
    assert (
        queries.latest_runs_by_spot(db, NOW, require_predictions=False)[_spot(db, "pipeline").id].id
        == run.id
    )
    # 26 h after its cycle, ECMWF is stale and leaves the blend.
    later = ECMWF_ISSUED + timedelta(hours=26)
    assert [x.code for x in blend.select_members(db, later)[0]] == [NOAA_GFS_CODE]
    status = queries.forecast_status(db, NOW)
    limits = {s["code"]: s["stale_after_hours"] for s in status["sources"]}
    assert limits == {NOAA_GFS_CODE: 16.0, ECMWF_IFS_CODE: 25.0}
    assert not any(s["stale"] for s in status["sources"])


# --------------------------------------------------------------------------- persistence
@pytest.fixture
def scored(db: Session, live_codes: list[str]) -> dict[str, Any]:
    """GFS + ECMWF blend with SYNTHETIC GEFS spread, scored at NOW with SYNTHETIC research:
    pipeline High, uluwatu Low, jeffreys-bay none."""
    _ingest(db)
    _synthetic_gefs(db)
    assessments = {
        "pipeline": _synthetic_research(db, "pipeline", fc.HIGH),
        "uluwatu": _synthetic_research(db, "uluwatu", fc.LOW),
    }
    assert blend.build_blend(db, NOW).status == "success"
    research_before = [
        (a.id, a.overall_class, a.evidence_fingerprint, a.is_current)
        for a in db.scalars(select(SpotResearchAssessment).order_by(SpotResearchAssessment.id))
    ]
    out = compute_pending_predictions(db, NOW)
    assert out["predictions"] > 0 and out["archived"] > 0
    return {"run": _blend_run(db), "assessments": assessments, "research_before": research_before}


def test_every_prediction_stores_both_classes_lead_and_issue_time(
    db: Session, scored: dict[str, Any]
) -> None:
    preds = db.scalars(select(SurfQualityPrediction)).all()
    assert preds
    for p in preds:
        run = db.get(ForecastRun, p.run_id)
        assert run is not None
        assert p.forecast_confidence_class in CLASSES and p.local_confidence_class in CLASSES
        assert fc.rank(p.local_confidence_class) <= fc.rank(p.forecast_confidence_class)
        assert p.confidence_method == "forecast-v1"
        assert p.issued_at == run.issued_at
        assert p.lead_hours == pytest.approx((p.valid_time - run.issued_at).total_seconds() / 3600)
        inputs = p.confidence_inputs or {}
        assert inputs["class"] == p.forecast_confidence_class
        assert inputs["index"] == p.confidence  # the legacy index is kept and recorded
        assert isinstance(inputs["rules"], list) and inputs["reasons"]
        assert inputs["local"]["class"] == p.local_confidence_class
    run = scored["run"]
    pipeline = _preds(db, run.id, "pipeline")
    jbay = _preds(db, run.id, "jeffreys-bay")
    assert {p.research_assessment_id for p in pipeline} == {scored["assessments"]["pipeline"].id}
    assert {p.research_assessment_id for p in jbay} == {None}


def test_requirement_1_research_and_forecast_confidence_are_stored_separately(
    db: Session, scored: dict[str, Any]
) -> None:
    run = scored["run"]
    by_spot = {s: _preds(db, run.id, s) for s in ("pipeline", "uluwatu", "jeffreys-bay")}
    # Same synthetic forecast everywhere: forecast confidence does not depend on research.
    classes = {s: [p.forecast_confidence_class for p in ps] for s, ps in by_spot.items()}
    assert classes["pipeline"] == classes["uluwatu"] == classes["jeffreys-bay"]
    # Local confidence is the documented min(forecast, research basis), nothing else.
    for p in by_spot["pipeline"]:
        assert p.local_confidence_class == p.forecast_confidence_class
    for p in [*by_spot["uluwatu"], *by_spot["jeffreys-bay"]]:
        assert p.local_confidence_class == fc.LOW
    # Scoring never touched the research assessments.
    after = [
        (a.id, a.overall_class, a.evidence_fingerprint, a.is_current)
        for a in db.scalars(select(SpotResearchAssessment).order_by(SpotResearchAssessment.id))
    ]
    assert after == scored["research_before"]
    # Archive: both classes and the research class used, in separate columns.
    rows = {
        (r.spot_id, r.valid_time): r
        for r in db.scalars(
            select(PredictionArchive).where(PredictionArchive.run_key == run.run_key)
        )
    }
    assert rows
    for slug, research in (("pipeline", fc.HIGH), ("uluwatu", fc.LOW), ("jeffreys-bay", None)):
        spot_rows = [r for (sid, _), r in rows.items() if sid == _spot(db, slug).id]
        assert spot_rows and {r.research_class for r in spot_rows} == {research}
        for r in spot_rows:
            assert r.confidence_method == "forecast-v1" and r.confidence_inputs
            assert r.forecast_confidence_class == r.confidence_inputs["class"]
            assert r.local_confidence_class == r.confidence_inputs["local"]["class"]
    # API: separate fields.
    summaries = {s.slug: s for s in spot_summaries(db, NOW)}
    assert summaries["pipeline"].research_class == fc.HIGH
    assert summaries["uluwatu"].research_class == fc.LOW
    assert summaries["jeffreys-bay"].research_class is None
    current = {s: summaries[s].current for s in ("pipeline", "uluwatu", "jeffreys-bay")}
    assert all(c is not None for c in current.values())
    assert len({c.forecast_confidence_class for c in current.values() if c}) == 1
    # New research changes local confidence only; the stored forecast class is unchanged.
    _synthetic_research(db, "jeffreys-bay", fc.HIGH)
    compute_predictions_for_run(db, run.id, force=True, now=NOW)
    db.expire_all()
    rescored = _preds(db, run.id, "jeffreys-bay")
    assert [p.forecast_confidence_class for p in rescored] == classes["jeffreys-bay"]
    assert [p.local_confidence_class for p in rescored] == classes["jeffreys-bay"]


def test_requirement_2_and_3_classes_by_lead_with_synthetic_ensemble(
    db: Session, scored: dict[str, Any]
) -> None:
    run = scored["run"]
    pipeline = {round(p.lead_hours or 0): p for p in _preds(db, run.id, "pipeline")}
    uluwatu = {round(p.lead_hours or 0): p for p in _preds(db, run.id, "uluwatu")}
    # (3) short lead + small spread + agreeing independent models: High forecast confidence,
    # while the poorly researched spot keeps Low local confidence with the research reason.
    short = uluwatu[24]
    assert short.forecast_confidence_class == fc.HIGH
    assert short.local_confidence_class == fc.LOW
    assert fc.stored_reasons(short.confidence_inputs)[-1] == fc.NO_RESEARCH_REASON
    assert "Ensemble members agree closely." in fc.stored_reasons(short.confidence_inputs)
    # 3-7 days: High only because the spread is small and two independent families agree.
    assert pipeline[120].forecast_confidence_class == fc.HIGH
    # (2) the well-researched spot at a 9-day lead: Moderate at best.
    nine = pipeline[216]
    assert nine.forecast_confidence_class in (fc.MODERATE, fc.LOW)
    assert "A 9-day forecast — forecasts this far out often change." in fc.stored_reasons(
        nine.confidence_inputs
    )
    # The blend's lead counts from its oldest member (ECMWF 12Z). Beyond ECMWF's last step
    # (240 h) only GFS (+ GEFS, not independent) remains, and it is over 10 days out.
    tail = pipeline[max(pipeline)]
    assert max(pipeline) > 240
    assert (tail.confidence_inputs or {})["independent_families"] == 1
    assert tail.forecast_confidence_class == fc.LOW
    assert "cap:moderate:single_source_over_3d" in (tail.confidence_inputs or {})["rules"]


def test_strong_independent_disagreement_is_low_or_moderate(
    db: Session, live_codes: list[str]
) -> None:
    _ingest(db, ecmwf_hs=4.6)  # SYNTHETIC: ECMWF 2.3x GFS
    _synthetic_research(db, "pipeline", fc.HIGH)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    preds = _preds(db, _blend_run(db).id, "pipeline")
    overlap = [p for p in preds if (p.lead_hours or 0) <= 240]  # ECMWF's last step
    assert overlap and all(p.forecast_confidence_class in (fc.LOW, fc.MODERATE) for p in overlap)
    assert all(
        "GFS and ECMWF disagree strongly on swell size." in fc.stored_reasons(p.confidence_inputs)
        and (p.confidence_inputs or {})["families"] == ["ncep_gfs", "ecmwf_ifs"]
        for p in overlap
    )


def test_requirement_4_spot_without_research_evidence_never_high_local_or_high_research(
    db: Session, live_codes: list[str]
) -> None:
    from app.services.research import current_assessments
    from app.services.research.assess import assess_all

    assess_all(db)  # no evidence file in the test data: every spot is assessed without any
    db.commit()
    assessments = current_assessments(db)
    assert assessments
    for row in assessments.values():
        assert row.overall_class != fc.HIGH
        assert all(v.get("class") != fc.HIGH for v in (row.per_variable or {}).values())
    _ingest(db)
    _synthetic_gefs(db)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    preds = db.scalars(select(SurfQualityPrediction)).all()
    assert any(p.forecast_confidence_class == fc.HIGH for p in preds)  # the forecast can be
    assert all(p.local_confidence_class != fc.HIGH for p in preds)  # the break cannot
    assert all(p.research_assessment_id == assessments[p.spot_id].id for p in preds)
    archived = db.scalars(select(PredictionArchive)).all()
    assert archived and {r.research_class for r in archived} <= {fc.LOW, fc.MODERATE}
    assert all(r.local_confidence_class != fc.HIGH for r in archived)


def _synthetic_metric(
    db: Session, source: str, scope: str, key: str, bucket: int, si: float, ss: float
) -> None:
    """A SYNTHETIC forecast_accuracy_metrics row (not a real verification result)."""
    db.add(
        ForecastAccuracyMetric(
            scope=scope,
            scope_key=key,
            source_code=source,
            model_version="raw",
            variable="hs",
            lead_bucket_h=bucket,
            period_start=NOW - timedelta(days=90),
            period_end=NOW - timedelta(days=1),
            n=500,
            rmse=0.5,
            scatter_index=si,
            skill_vs_persistence=ss,
            extra={"synthetic": True},
        )
    )


def test_verification_caps_poor_scores_and_reports_missing_local_checks(
    db: Session, live_codes: list[str]
) -> None:
    for bucket in LEAD_BUCKETS_H:
        _synthetic_metric(db, "gfswave_bulletin", "global", "all", bucket, si=0.62, ss=0.4)
        _synthetic_metric(db, "ecmwf_ifs_wave", "global", "all", bucket, si=0.15, ss=0.8)
    _synthetic_metric(db, "ecmwf_ifs_wave", "station", "synthetic:buoy", 24, si=0.12, ss=0.9)
    db.commit()
    index = VerificationIndex.load(db, {"gfswave_bulletin", "gfswave_grid", "ecmwf_ifs_wave"})
    near = index.lookup(("ncep_gfs", "ecmwf_ifs"), ["synthetic:buoy"], 20.0)
    assert [(v.source_code, v.scope, v.near_spot, v.poor) for v in near] == [
        ("gfswave_bulletin", "global", False, True),
        ("ecmwf_ifs_wave", "station", True, False),
    ]
    assert nearest_lead_bucket(90.0) == 72 and nearest_lead_bucket(100.0) == 120

    _ingest(db)
    _synthetic_gefs(db)
    assert blend.build_blend(db, NOW).status == "success"
    compute_pending_predictions(db, NOW)
    preds = _preds(db, _blend_run(db).id, "pipeline")
    assert preds and all(p.forecast_confidence_class != fc.HIGH for p in preds)
    for p in preds:
        reasons = fc.stored_reasons(p.confidence_inputs)
        assert "Recent checks against buoys show large errors." in reasons
        assert "Not yet checked against buoys near this spot." in reasons
        inputs = p.confidence_inputs or {}
        sources = {v["source"] for v in inputs["verification"]}
        # one row per family behind the hour (GFS only beyond ECMWF's last step)
        expected = {"ncep_gfs": "gfswave_bulletin", "ecmwf_ifs": "ecmwf_ifs_wave"}
        assert sources == {expected[f] for f in inputs["families"]}


def test_api_outputs(db: Session, scored: dict[str, Any], app_client: ApiClient) -> None:
    spot = _spot(db, "uluwatu")
    out = spot_forecast(db, spot, now=NOW)
    assert out.available and not out.stale
    for point in out.points:
        assert point.forecast_confidence_class in CLASSES
        assert point.local_confidence_class == fc.LOW
        assert point.lead_hours is not None and point.confidence_reasons
        assert point.confidence_reasons[-1] == fc.NO_RESEARCH_REASON  # forecast reasons first
    for day in out.daily:
        best = next(p for p in out.points if p.valid_time == day.best_time)
        assert day.forecast_confidence_class == best.forecast_confidence_class
        assert day.local_confidence_class == best.local_confidence_class
    snap = map_snapshot(db, NOW + timedelta(hours=24), now=NOW)
    cond = next(s.conditions for s in snap.spots if s.slug == "uluwatu")
    assert cond is not None and cond.forecast_confidence_class == fc.HIGH
    assert cond.local_confidence_class == fc.LOW and cond.lead_hours is not None
    # Over HTTP (real "now" is long after these synthetic runs: stale, so capped).
    body = app_client.client.get("/api/spots/uluwatu/forecast").json()
    point = body["points"][0] if body["points"] else None
    if point is not None:
        assert point["forecast_confidence_class"] in ("moderate", "low")
        assert point["confidence_reasons"][0] == fc.STALE_REASON
    # Read-time staleness: 30 h later the GFS member is stale, so nothing shows High.
    late = spot_forecast(db, spot, now=NOW + timedelta(hours=30))
    assert late.stale and late.points
    assert all(p.forecast_confidence_class != fc.HIGH for p in late.points)
    assert late.points[0].confidence_reasons[0] == fc.STALE_REASON


# --------------------------------------------------------------------------- demo, events, alerts
def test_demo_data_is_low_in_predictions_events_and_alerts(
    db: Session, few_spots: list[str], app_client: ApiClient
) -> None:
    user = make_user(db)
    make_search(db, user)
    from app.services.pipeline import run_pipeline

    run_pipeline(db)
    simulate(db)
    preds = db.scalars(select(SurfQualityPrediction)).all()
    assert preds and {p.forecast_confidence_class for p in preds} == {fc.LOW}
    assert {p.local_confidence_class for p in preds} == {fc.LOW}
    assert all(
        fc.stored_reasons(p.confidence_inputs)[0] == "Demo data, not a real forecast."
        for p in preds
    )
    events = upcoming_events(db)
    assert events
    for e in events:
        assert e.forecast_confidence_class == fc.LOW and e.local_confidence_class == fc.LOW
        assert e.confidence_reasons[0] == "Demo data, not a real forecast."
    body = app_client.client.get("/api/events").json()
    assert body and all(e["forecast_confidence_class"] == "low" for e in body)
    assert body[0]["confidence_reasons"][0] == "Demo data, not a real forecast."
    spots = app_client.client.get("/api/spots").json()
    current = [s["current"] for s in spots if s["current"]]
    assert current and {c["forecast_confidence_class"] for c in current} == {"low"}
    assert all(c["lead_hours"] is not None and c["confidence_reasons"] for c in current)
    forecast = app_client.client.get("/api/spots/jeffreys-bay/forecast").json()
    assert {d["forecast_confidence_class"] for d in forecast["daily"]} == {"low"}
    sent = db.scalars(select(Notification)).all()
    assert sent
    snap = sent[0].snapshot
    assert snap["forecast_confidence_class"] == fc.LOW
    text = sent[0].body_text or ""
    assert "Forecast confidence: Low — about " in text and " days away" in text
    assert "Confidence at this break: Low — " in text
