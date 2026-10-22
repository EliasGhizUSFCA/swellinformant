"""Database egress of the read paths and of scoring (docs/map-layers.md, egress).

* the public read paths read predictions with their forecasts in one join, each forecast
  column once, and a bounded number of statements whatever the number of spots;
* scoring reads only the columns and JSON keys it uses, and what a pass shares once; its
  results are the same as scoring each run on its own;
* the per-pass prune applies the forecast-run retention.

Forecasts are SYNTHETIC: the demo provider's, or tests.integration.test_retention_bounds'
constant sea state. Tide predictions are the REAL CO-OPS Pillar Point fixture and the EOT20
synthesis of tests.integration.test_tides_api.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.core.database import get_engine
from app.models import (
    ForecastComparison,
    ForecastRun,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
    SwellEvent,
    WaveForecast,
)
from app.models.enums import ConfidenceLabel, QualityLabel, SwellEventStatus
from app.services import spot_views
from app.services.forecasts import blend
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.maintenance import prune_pass
from app.services.pipeline import run_pipeline
from app.services.research import current_assessments
from app.services.research.assess import seed_research
from app.services.seed import DEFAULT_DATA_DIR
from app.services.spot_views import (
    map_snapshot,
    predictions_with_forecasts,
    spot_detail,
    spot_forecast,
    spot_summaries,
    upcoming_events,
)
from app.services.surf_quality import predict
from app.services.surf_quality.confidence_context import (
    _comparisons,
    research_inputs,
    scoring_assessments,
)
from app.services.surf_quality.forecast_confidence import research_input
from app.services.tides.ingest import acquire_tides
from app.services.tides.stage import shared_tide_reads, tide_stages_for
from app.services.timeline import spot_timeline
from tests.integration.test_retention_bounds import (  # noqa: F401 (codes is a fixture)
    NOW,
    SyntheticProvider,
    _ingest,
    codes,
)
from tests.integration.test_tides_api import NOW as TIDE_NOW
from tests.integration.test_tides_api import coops_transport, tide_spots  # noqa: F401

pytestmark = pytest.mark.integration


class Statements:
    """The SQL statements executed inside the block."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def __enter__(self) -> Statements:
        event.listen(get_engine(), "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(get_engine(), "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        self.sql.append(statement)

    def count(self, table: str) -> int:
        return sum(f"FROM {table}" in s or f"JOIN {table}" in s for s in self.sql)

    def reading(self, fragment: str) -> int:
        return sum(fragment in " ".join(s.split()) for s in self.sql)


def count_statements(fn: Callable[[], Any]) -> int:
    with Statements() as seen:
        fn()
    return len(seen.sql)


# --------------------------------------------------------------------------- read paths
def test_predictions_with_their_forecasts_read_each_forecast_once() -> None:
    """``SurfQualityPrediction.forecast`` is lazy="joined": selecting both entities over a
    plain join added a second, eager copy of every forecast row."""
    sql = str(
        predictions_with_forecasts(SurfQualityPrediction.id.in_([1])).compile(
            dialect=postgresql.dialect()
        )
    )
    assert sql.count("JOIN wave_forecasts") == 1 and "LEFT OUTER JOIN" not in sql
    assert sql.count("wave_forecasts.sig_wave_height_m") == 1
    naive = str(
        select(SurfQualityPrediction, WaveForecast)
        .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
        .compile(dialect=postgresql.dialect())
    )
    assert "LEFT OUTER JOIN wave_forecasts AS wave_forecasts_1" in naive  # what it replaces


def test_the_joined_forecast_is_filled_without_another_statement(
    db: Session, few_spots: list[str]
) -> None:
    run_pipeline(db)
    db.expunge_all()
    rows = db.execute(predictions_with_forecasts().limit(5)).all()
    assert len(rows) == 5
    assert count_statements(lambda: [p.forecast.sig_wave_height_m for p, _ in rows]) == 0
    assert all(p.forecast is f for p, f in rows)


@pytest.fixture
def scored(db: Session, few_spots: list[str]) -> Session:
    run_pipeline(db)
    return db


def test_the_public_reads_take_a_fixed_number_of_statements(scored: Session) -> None:
    """No statement per spot, hour or event: the counts with five spots of data equal the
    counts with one (every other spot of the few-spots fixture taken off the list)."""
    db = scored
    now = spot_views.utcnow()
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "pipeline"))
    assert spot is not None

    def counts() -> dict[str, int]:
        db.expunge_all()
        target = db.get(SurfSpot, spot.id)
        assert target is not None
        return {
            "spots": count_statements(lambda: spot_summaries(db, now)),
            "conditions": count_statements(lambda: map_snapshot(db, None, now)),
            "detail": count_statements(lambda: spot_detail(db, target, now)),
            "forecast": count_statements(lambda: spot_forecast(db, target, 10, now)),
            "timeline": count_statements(lambda: spot_timeline(db, target, 10, now)),
        }

    five = counts()
    assert five["spots"] <= 16, five
    db.execute(text("UPDATE surf_spots SET is_active = (slug = 'pipeline')"))
    db.commit()
    assert counts() == five


def test_the_spot_list_reads_whole_rows_only_for_the_chosen_hours(scored: Session) -> None:
    db = scored
    now = spot_views.utcnow()
    with Statements() as seen:
        spots = spot_summaries(db, now)
    whole = [s for s in seen.sql if "surf_quality_predictions.components" in s]
    assert len(whole) == 1 and "surf_quality_predictions.id IN" in whole[0]
    chosen = {c.valid_time for s in spots for c in (s.current, s.best_upcoming) if c is not None}
    assert chosen  # the demo data gives every spot a current and a best hour


@pytest.mark.usefixtures("codes")
def test_events_of_a_blend_run_read_its_attribution_once(db: Session) -> None:
    """A blend run's attribution links need its members' sources (one statement): a list of
    events of the same run reads them once, not once per event."""
    _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=6)))
    _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=6)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    blend_run = db.scalars(select(ForecastRun).where(ForecastRun.run_key.like("blend:%"))).one()
    spots = list(db.scalars(select(SurfSpot).where(SurfSpot.is_active).limit(3)))
    for i, spot in enumerate(spots):
        db.add(
            SwellEvent(  # SYNTHETIC events, one per spot
                spot_id=spot.id,
                status=SwellEventStatus.ACTIVE,
                start_time=NOW + timedelta(days=6 + i),
                end_time=NOW + timedelta(days=6 + i, hours=12),
                peak_time=NOW + timedelta(days=6 + i, hours=6),
                peak_score=70,
                avg_score=65.0,
                peak_label=QualityLabel.GOOD,
                peak_breaking_height_min_ft=4.0,
                peak_breaking_height_max_ft=6.0,
                qualifying_hours=12,
                confidence=60,
                confidence_label=ConfidenceLabel.MODERATE,
                latest_run_id=blend_run.id,
                source_code="blend",
            )
        )
    db.commit()
    db.expunge_all()
    with Statements() as seen:
        events = upcoming_events(db, NOW, limit=10)
    assert len(events) == 3
    members = "FROM forecast_sources WHERE forecast_sources.code IN"
    assert seen.reading(members) == 1  # the blend's member sources, read once
    db.expunge_all()
    with Statements() as listed:
        summaries = spot_summaries(db, NOW)
    assert sum(s.next_event is not None for s in summaries) == 3
    assert listed.reading(members) == 1


# --------------------------------------------------------------------------- scoring
def _prediction_rows(db: Session) -> dict[tuple[int, int, datetime], dict[str, Any]]:
    skip = {"id", "forecast_id"}
    out = {}
    for p in db.scalars(select(SurfQualityPrediction)):
        values = {c.key: getattr(p, c.key) for c in SurfQualityPrediction.__table__.columns}
        out[(p.run_id, p.spot_id, p.valid_time)] = {
            k: v for k, v in values.items() if k not in skip
        }
    return out


def _unscore(db: Session) -> None:
    db.execute(delete(SurfQualityPrediction))
    db.execute(update(ForecastRun).values(predictions_computed_at=None))
    db.commit()


@pytest.fixture
def tides_and_research(db: Session, tide_spots: list[str]) -> Iterator[None]:  # noqa: F811
    acquire_tides(db, TIDE_NOW, client=coops_transport([]), backoff_seconds=0)
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    yield


@pytest.mark.usefixtures("tides_and_research", "codes")
def test_a_pass_scores_exactly_as_each_run_on_its_own(db: Session) -> None:
    """Two member runs and their blend, scored once by compute_pending_predictions (the
    pass's shared reads: spots, research, decisions, tide predictions) and once run by run
    with compute_predictions_for_run (every input read for that run alone)."""
    tide_now = TIDE_NOW + timedelta(hours=12)  # inside the tide fixture's days
    for code, family, hs, prio in (("t_gfs", "ncep_gfs", 2.1, 0), ("t_ecmwf", "ecmwf_ifs", 2.6, 1)):
        provider = SyntheticProvider(code, family, hs, tide_now - timedelta(hours=6))
        out = acquire_from_provider(db, provider, now=tide_now, priority=prio)
        assert out.status in ("success", "partial"), out
    assert blend.build_blend(db, tide_now).status == "success"
    runs = list(db.scalars(select(ForecastRun.id).order_by(ForecastRun.issued_at)))
    assert len(runs) == 3
    out = predict.compute_pending_predictions(db, tide_now)
    assert out["runs"] == 3 and out["predictions"] > 0
    shared = _prediction_rows(db)
    assert any(
        (v["components"] or {}).get("tide_detail", {})
        and v["components"]["tide_detail"].get("source") in ("noaa_coops", "eot20")
        for v in shared.values()
    ), "the real tide predictions were used"
    _unscore(db)
    for run_id in runs:
        predict.compute_predictions_for_run(db, run_id, now=tide_now)
    assert _prediction_rows(db) == shared


@pytest.mark.usefixtures("tides_and_research")
def test_shared_tide_reads_give_the_same_stages_with_one_read_per_station(db: Session) -> None:
    mavericks = db.scalar(select(SurfSpot).where(SurfSpot.slug == "mavericks"))
    assert mavericks is not None
    start = TIDE_NOW + timedelta(hours=12)
    windows = [
        [start + timedelta(hours=h) for h in range(0, 72)],  # hourly, 3 days
        [start + timedelta(hours=h) for h in range(0, 240, 3)],  # 3-hourly, 10 days
        [start + timedelta(hours=h, minutes=30) for h in range(12, 30)],  # inside, off-hour
    ]
    alone = [tide_stages_for(db, mavericks.id, times) for times in windows]
    assert all(alone)
    with Statements() as seen, shared_tide_reads(db):
        together = [tide_stages_for(db, mavericks.id, times) for times in windows]
    assert together == alone
    # One stations read; predictions read for the first window and once more when the
    # second reaches past it (the union of both); the third is inside.
    assert seen.count("tide_stations") == 1
    assert seen.count("tide_predictions") == 2
    with (
        Statements() as primed,
        shared_tide_reads(db, {mavericks.id: (start, start + timedelta(hours=237))}),
    ):
        assert [tide_stages_for(db, mavericks.id, times) for times in windows] == alone
    assert primed.count("tide_predictions") == 1  # the pass's window, read once


def test_scoring_reads_the_assessments_without_the_unused_evidence(db: Session) -> None:
    seed_research(db, DEFAULT_DATA_DIR)
    db.commit()
    full = current_assessments(db)
    trimmed = scoring_assessments(db)
    assert full.keys() == trimmed.keys() and full
    assert research_inputs(trimmed) == {
        spot_id: entry
        for spot_id, row in full.items()
        if (entry := research_input(row)) is not None
    }
    assert predict.tide_research_classes(db) == predict.tide_research_classes(db, trimmed)
    for spot_id, row in full.items():
        kept = trimmed[spot_id].per_variable or {}
        for name, entry in (row.per_variable or {}).items():
            if name == "wind" or not isinstance(entry, dict):
                assert kept[name] == entry  # the wind dispute ranks the wind evidence
            else:
                assert kept[name] == {k: v for k, v in entry.items() if k != "evidence"}
    size = func.sum(func.length(SpotResearchAssessment.per_variable.cast(postgresql.TEXT)))
    assert db.scalar(select(size)) > 0


@pytest.mark.usefixtures("codes")
def test_blend_comparisons_read_only_the_member_families(db: Session) -> None:
    _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=6)))
    _ingest(db, SyntheticProvider("t_ecmwf", "ecmwf_ifs", 2.6, NOW - timedelta(hours=6)), 1)
    assert blend.build_blend(db, NOW).status == "success"
    blend_run = db.scalars(select(ForecastRun).where(ForecastRun.run_key.like("blend:%"))).one()
    rows = list(
        db.scalars(
            select(ForecastComparison).where(ForecastComparison.blend_run_id == blend_run.id)
        )
    )
    assert rows
    # SYNTHETIC edge cases: a member without a role, a JSON-null role, a non-object entry,
    # falsy and non-string families, a non-string role, a family listed twice.
    rows[0].sources = {
        **rows[0].sources,
        "t_norole": {"family": "f_norole", "hs": 1.0},
        "t_nullrole": {"family": "f_null", "role": None},
        "t_scalar": 3,
        "t_empty": {"family": ""},
        "t_zero": {"family": 0, "role": "member"},
        "t_number": {"family": 7},
        "t_listrole": {"family": "f_list", "role": ["member"]},
        "t_reference": {"family": "f_reference", "role": "reference"},
        "t_twice": {"family": "f_norole"},
    }
    rows[1].sources = {}
    db.commit()
    got = _comparisons(db, blend_run)

    def expected(row: ForecastComparison) -> tuple[str, ...]:
        order = [str(m["family"]) for m in blend_run.details.get("members", [])]
        return tuple(
            sorted(
                {
                    str(v.get("family"))
                    for v in (row.sources or {}).values()
                    if isinstance(v, dict)
                    and v.get("role", "member") == "member"
                    and v.get("family")
                },
                key=lambda f: order.index(f) if f in order else len(order),
            )
        )

    for row in rows:
        hour = got[(row.spot_id, row.valid_time)]
        assert hour.families == expected(row)
        assert hour.disagreement.hs_log_ratio == row.hs_log_ratio_max
        assert hour.disagreement.flags == tuple(str(f) for f in row.flags or ())
    assert "f_norole" in got[(rows[0].spot_id, rows[0].valid_time)].families
    assert "f_null" not in got[(rows[0].spot_id, rows[0].valid_time)].families
    assert "7" in got[(rows[0].spot_id, rows[0].valid_time)].families
    assert "f_list" not in got[(rows[0].spot_id, rows[0].valid_time)].families
    assert "f_reference" not in got[(rows[0].spot_id, rows[0].valid_time)].families
    assert got[(rows[1].spot_id, rows[1].valid_time)].families == ()


def test_scoring_forecasts_carry_only_the_extra_keys_scoring_reads(
    db: Session, few_spots: list[str]
) -> None:
    run_pipeline(db)
    row = db.scalars(select(WaveForecast).limit(1)).one()
    row.extra = {"gust_kind": "max_1h", "interpolated": True, "families": ["a"]}
    other = db.scalars(select(WaveForecast).where(WaveForecast.id != row.id).limit(1)).one()
    other.extra = {"ml_corrections": None}
    db.commit()
    by_id = {f.id: f for f in predict.scoring_forecasts(db, row.run_id)}
    assert by_id[row.id].extra == {"gust_kind": "max_1h"}
    got = by_id.get(other.id) or next(
        f for f in predict.scoring_forecasts(db, other.run_id) if f.id == other.id
    )
    assert got.extra == {} and got.extra.get("ml_corrections") is None
    whole = db.get(WaveForecast, row.id)
    assert whole is not None
    for name in predict._SCORING_COLUMNS:
        assert getattr(by_id[row.id], name) == getattr(whole, name), name


# --------------------------------------------------------------------------- retention
@pytest.mark.usefixtures("codes")
def test_the_prune_pass_applies_the_forecast_run_retention(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "retention_forecast_days", 1)
    old = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=60)))
    _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.1, NOW - timedelta(hours=36)))
    _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.2, NOW - timedelta(hours=12)))
    db.execute(
        update(ForecastRun)
        .where(ForecastRun.id == old.id)
        .values(started_at=NOW - timedelta(days=2))
    )
    db.commit()
    out = prune_pass(db, NOW)
    assert out["forecast_runs"] == 1  # the two newest usable runs are always kept
    assert db.get(ForecastRun, old.id) is None
    assert db.scalar(select(func.count()).select_from(ForecastRun)) == 2
    assert prune_pass(db, NOW)["forecast_runs"] == 0
