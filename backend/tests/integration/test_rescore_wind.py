"""Rescoring a real blend run with algorithm 2.1.0 (offshore wind physics, partition-weighted
swell, gated tide) through the prediction job.

The forecast values are REAL: every 3-hourly step with wind of 30 km/h or more of the blend
run of 2026-10-11 (NOAA GFS 18Z + ECMWF IFS 12Z; tests/fixtures/wind_scoring, source and licence in
the file), with the 2.0.0 result of each step from the same re-seeded spot profiles. The tide
stages and sea levels in the last two tests are SYNTHETIC (no tide predictions exist for that
run).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    ForecastRun,
    ForecastSource,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.research.assess import seed_research
from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality import predict
from app.services.surf_quality.predict import (
    compute_predictions_for_run,
    outdated_authoritative_runs,
)
from app.services.surf_quality.scoring import (
    ALGORITHM_VERSION,
    NO_TIDE_PREDICTION,
    min_score_for_label,
)
from app.services.tides.stage import TideStage

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "wind_scoring" / "blend_2026101112_wind30.json"
ISSUED = datetime(2026, 10, 11, 12, tzinfo=UTC)
NOW = datetime(2026, 10, 12, 4, tzinfo=UTC)


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _load_run(db: Session) -> tuple[ForecastRun, dict[int, list[Any]]]:
    """The fixture steps as a blend run; returns the run and forecast id -> 2.0.0 result."""
    data = _fixture()
    spots = {s.slug: s.id for s in db.scalars(select(SurfSpot))}
    source = ForecastSource(
        code="blend",
        name="Swell Travel Agent multi-model blend",
        provider="swell_travel_agent",
        family="blend",
        kind="blend",
        priority=-1,
    )
    db.add(source)
    db.flush()
    rows = [dict(zip(data["columns"], r, strict=True)) for r in data["rows"]]
    run = ForecastRun(
        source_id=source.id,
        run_key=data["run"]["run_key"],
        issued_at=ISSUED,
        status=ForecastRunStatus.SUCCESS,
        completed_at=NOW - timedelta(minutes=10),
        details={"members": [], "spot_ids": sorted({spots[r["slug"]] for r in rows})},
    )
    db.add(run)
    db.flush()
    v200: dict[int, list[Any]] = {}
    for row, old in zip(rows, data["v200"], strict=True):
        values = {k: v for k, v in row.items() if k not in ("slug", "valid_time")}
        f = WaveForecast(
            run_id=run.id,
            spot_id=spots[row["slug"]],
            valid_time=datetime.fromisoformat(row["valid_time"]),
            issued_at=ISSUED,
            missing_fields=[],
            extra={},
            **values,
        )
        db.add(f)
        db.flush()
        v200[f.id] = old
    db.commit()
    return run, v200


def _scored(db: Session, run: ForecastRun) -> list[tuple[SurfQualityPrediction, WaveForecast]]:
    return [
        (p, f)
        for p, f in db.execute(
            select(SurfQualityPrediction, WaveForecast)
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(SurfQualityPrediction.run_id == run.id)
        ).all()
    ]


def test_the_real_strong_wind_steps_meet_the_acceptance_rules(db: Session) -> None:
    run, v200 = _load_run(db)
    assert compute_predictions_for_run(db, run.id, now=NOW) == len(v200) == 502
    scored = _scored(db, run)
    very_good, good = min_score_for_label("very_good"), min_score_for_label("good")
    # 2.0.0 on the same steps: strong offshore and cross-shore wind still rated well.
    old_offshore_30 = [o for o in v200.values() if o[2] == "offshore" and o[0] >= very_good]
    assert len(old_offshore_30) >= 10
    changes: Counter[tuple[str, str]] = Counter()
    for p, f in scored:
        assert p.algorithm_version == ALGORITHM_VERSION == "2.2.0"  # 2.2.0: same scores
        c = p.components
        for key in ("wind_detail", "weights_used", "contributions"):
            assert key in c, key
        assert ("tide_detail" in c) != ("tide_excluded_reason" in c)
        assert sum(c["contributions"].values()) + p.score == pytest.approx(100, abs=0.6)
        detail = c["wind_detail"]
        assert detail["strength"] == "strong"  # every fixture step is 30 km/h or more
        speed, direction = f.wind_speed_kmh, f.wind_direction_deg
        assert speed is not None and direction is not None
        offshore = detail["params"]["offshore_wind_direction"]["value"]
        expected = speed * math.cos(math.radians(direction - offshore))
        # V . n of the forecast wind, also at the sheltered spots (shelter changes the fit).
        assert detail["v_offshore_kmh"] == pytest.approx(expected, abs=0.1)
        assert not (p.wind_relation == "offshore" and speed >= 45 and p.score >= very_good)
        assert not (p.wind_relation == "cross-shore" and speed >= 30 and p.score >= good)
        old, label = v200[f.id], str(getattr(p.label, "value", p.label))
        if old[1] != label:
            changes[(old[1], label)] += 1
    # Reported, not asserted to an exact value: the label changes on these 502 steps.
    print(f"label changes on {len(scored)} strong-wind steps: {sum(changes.values())}")
    print(dict(changes.most_common()))
    assert sum(changes.values()) > 0


def test_rows_scored_by_200_are_rescored(db: Session) -> None:
    run, _ = _load_run(db)
    compute_predictions_for_run(db, run.id, now=NOW)
    assert outdated_authoritative_runs(db, NOW) == []
    db.execute(
        update(SurfQualityPrediction)
        .where(SurfQualityPrediction.run_id == run.id)
        .values(algorithm_version="2.0.0")
    )
    db.commit()
    assert outdated_authoritative_runs(db, NOW) == [run.id]
    out = predict.compute_pending_predictions(db, NOW)
    assert out["rescored_runs"] == 1
    assert outdated_authoritative_runs(db, NOW) == []


def test_the_tide_is_scored_only_where_the_preference_is_established(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SYNTHETIC tide stages for every step: Dungeons (tide research class high, decision
    kept, low_to_mid) scores its tide; Burleigh Heads (class low) does not."""
    db.execute(delete(SpotResearchAssessment))  # the db fixture truncates research tables
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    run, _ = _load_run(db)

    def stages(db: Session, spot_id: int, times: list[datetime]) -> dict[datetime, TideStage]:
        return {
            t: TideStage(stage=0.0, rising=True, range_m=1.2, source="synthetic") for t in times
        }

    monkeypatch.setattr(predict, "tide_stages_for", stages)
    compute_predictions_for_run(db, run.id, now=NOW)
    slug_of = {s.id: s.slug for s in db.scalars(select(SurfSpot))}
    seen = set()
    for p, _f in _scored(db, run):
        slug = slug_of[p.spot_id]
        c = p.components
        if slug == "dungeons":
            assert c["tide_detail"] == {
                "stage": 0.0,
                "rising": True,
                "range_m": 1.2,
                "source": "synthetic",
            }
            assert c["tide"] == pytest.approx(0.52)  # 1 - 1.6 x |0 - 0.3|
            assert "weights_used" in c and "tide" in c["weights_used"]
            seen.add(slug)
        elif slug == "burleigh-heads":
            assert "tide" not in c
            assert c["tide_excluded_reason"].startswith("Tide preference not well established")
            assert "tide not scored" in p.explanation
            seen.add(slug)
    assert seen == {"dungeons", "burleigh-heads"}


def test_model_sea_level_is_never_a_tide_stage_for_a_real_run(db: Session) -> None:
    """A real run whose rows carry a (SYNTHETIC) sea level gets no tide stage from it: tide
    stages come from tide predictions only. A demo run uses its synthetic sea level,
    labelled "demo"."""
    db.execute(delete(SpotResearchAssessment))
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    run, _ = _load_run(db)
    dungeons = db.scalar(select(SurfSpot.id).where(SurfSpot.slug == "dungeons"))
    rows = db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == run.id, WaveForecast.spot_id == dungeons)
        .order_by(WaveForecast.valid_time)
    ).all()
    assert len(rows) >= 3
    for i, f in enumerate(rows):
        f.sea_level_m = 1.2 * math.sin(i)  # SYNTHETIC
    db.commit()
    compute_predictions_for_run(db, run.id, now=NOW)
    real = [p for p, f in _scored(db, run) if f.spot_id == dungeons]
    assert real and all(p.components["tide_stage"] is None for p in real)
    assert all(p.components["tide_excluded_reason"] == NO_TIDE_PREDICTION for p in real)
    db.execute(update(ForecastSource).values(is_demo=True))
    db.commit()
    compute_predictions_for_run(db, run.id, force=True, now=NOW)
    db.expire_all()
    demo = [p for p, f in _scored(db, run) if f.spot_id == dungeons]
    staged = [p for p in demo if p.components["tide_stage"] is not None]
    assert staged
    assert all(p.components["tide_detail"]["source"] == "demo" for p in staged)
