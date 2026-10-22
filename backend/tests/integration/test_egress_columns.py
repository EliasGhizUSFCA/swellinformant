"""Column-selective reads in the prediction archive, swell detection and matching give the
same results as reading whole rows (Upgrade 4, Neon free-tier egress).

Owned by the egress-map-serving package. The forecasts are the DEMO provider's (SYNTHETIC,
labelled demo in the app) at a fixed clock, plus one simulated swell so that searches match;
the saved searches are made up for the test. Each comparison runs the same stage twice on
the same stored predictions: once with the pre-Upgrade-4 code (whole SurfQualityPrediction
and WaveForecast entities in every step, the peak taken from them; kept below as verbatim
reference copies) and once with the column-selective code, and compares every stored
column.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_engine
from app.models import (
    ForecastRun,
    OpportunityMatch,
    PredictionArchive,
    SavedSearch,
    SurfQualityPrediction,
    SurfSpot,
    SwellEvent,
    WaveForecast,
)
from app.models.enums import QualityLabel, SwellEventStatus
from app.schemas.searches import SearchIn
from app.services import searches
from app.services.forecasts import archive
from app.services.forecasts.ingestion import acquire_all, simulate_swell
from app.services.forecasts.queries import latest_runs_by_spot
from app.services.matching import service as matching
from app.services.matching.evaluate import SearchCriteria, step_meets
from app.services.surf_quality.breaking import dominant_component
from app.services.surf_quality.confidence import confidence_label
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.surf_quality.scoring import label_for_score
from app.services.swell_detection import service as detection
from app.services.swell_detection.clustering import Cluster, Step, cluster_steps, overlaps
from app.services.swell_detection.service import StepForecast, StepPrediction, load_steps
from tests.conftest import search_payload

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
FROZEN = datetime(2026, 10, 10, 12, 30, tzinfo=UTC)  # the stages' wall clock (utcnow)
SWELL_START = NOW + timedelta(days=7)


# --------------------------------------------------------------------------- references
def reference_load_steps(db: Session, spot_id: int, run_id: int, since: datetime) -> list[Step]:
    """load_steps before Upgrade 4: the same steps, but each payload is the whole stored
    prediction and its wave_forecasts row (what detection and matching used to read)."""
    steps = load_steps(db, spot_id, run_id, since)
    whole = {
        p.id: (p, f)
        for p, f in db.execute(
            select(SurfQualityPrediction, WaveForecast)
            .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
            .where(
                SurfQualityPrediction.run_id == run_id,
                SurfQualityPrediction.spot_id == spot_id,
                SurfQualityPrediction.valid_time >= since,
            )
        ).all()
    }
    assert len(whole) == len(steps)
    return [dataclasses.replace(s, payload=whole[s.payload[0].id]) for s in steps]


def _reference_apply_cluster(
    event: SwellEvent, cluster: Cluster, run: ForecastRun, keep_start: bool
) -> None:
    """_apply_cluster before Upgrade 4 (verbatim but for the frozen clock): the peak is the
    whole prediction in the peak step's payload."""
    peak_pred, peak_fc = cluster.peak.payload
    preds = [s.payload[0] for s in cluster.steps]
    if not keep_start or cluster.start < event.start_time:
        event.start_time = cluster.start
    event.end_time = cluster.end
    event.peak_time = peak_pred.valid_time
    event.peak_score = peak_pred.score
    event.avg_score = round(cluster.avg_score, 1)
    event.peak_label = QualityLabel(label_for_score(peak_pred.score))
    event.peak_breaking_height_min_ft = peak_pred.breaking_height_min_ft
    event.peak_breaking_height_max_ft = peak_pred.breaking_height_max_ft
    (
        event.peak_swell_height_m,
        event.peak_swell_period_s,
        event.peak_swell_direction_deg,
    ) = dominant_component(peak_pred, peak_fc)
    event.peak_wind_speed_kmh = peak_fc.wind_speed_kmh
    event.peak_wind_direction_deg = peak_fc.wind_direction_deg
    event.peak_wind_relation = peak_pred.wind_relation
    event.qualifying_hours = int(round(cluster.qualifying_hours))
    event.confidence = int(round(mean(p.confidence for p in preds)))
    event.confidence_label = confidence_label(event.confidence)
    event.latest_run_id = run.id
    event.source_code = run.source.code
    event.is_demo = run.source.is_demo
    event.last_updated_at = detection.utcnow()
    snapshot = {
        "run_key": run.run_key,
        "at": event.last_updated_at.isoformat(),
        "start": event.start_time.isoformat(),
        "end": event.end_time.isoformat(),
        "peak_score": event.peak_score,
        "peak_height_ft": [event.peak_breaking_height_min_ft, event.peak_breaking_height_max_ft],
        "confidence": event.confidence,
        **detection._confidence_snapshot(peak_pred),
        **detection._wind_snapshot(peak_pred, peak_fc),
    }
    event.history = [*(event.history or [])[-19:], snapshot]


def reference_detect_for_spot(
    db: Session,
    spot: SurfSpot,
    run: ForecastRun,
    now: datetime,
    stats: detection.DetectionStats,
) -> list[SwellEvent]:
    """detect_for_spot before Upgrade 4 (verbatim but for the frozen clock and
    reference_load_steps)."""
    settings = get_settings()
    steps = reference_load_steps(db, spot.id, run.id, since=now - timedelta(hours=6))
    threshold = settings.swell_event_min_score
    clusters = cluster_steps(
        steps,
        qualifies=lambda s: s.score >= threshold,
        min_hours=settings.swell_event_min_hours,
        break_daylight_hours=settings.swell_event_break_daylight_hours,
    )
    coverage_start = steps[0].time if steps else now

    existing = list(
        db.scalars(
            select(SwellEvent)
            .where(SwellEvent.spot_id == spot.id, SwellEvent.status == SwellEventStatus.ACTIVE)
            .order_by(SwellEvent.start_time)
            .with_for_update(of=SwellEvent)
        ).all()
    )
    touched: set[object] = set()
    results: list[SwellEvent] = []
    for cluster in clusters:
        hits = [
            e
            for e in existing
            if e.id not in touched
            and overlaps(
                e.start_time, e.end_time, cluster.start, cluster.end, detection.MATCH_TOLERANCE
            )
        ]
        if hits:
            primary = hits[0]
            for extra in hits[1:]:
                extra.status = SwellEventStatus.CANCELLED
                extra.merged_into_id = primary.id
                extra.last_updated_at = detection.utcnow()
                touched.add(extra.id)
                stats.merged += 1
            keep_start = primary.start_time < coverage_start
            _reference_apply_cluster(primary, cluster, run, keep_start)
            primary.version += 1
            touched.add(primary.id)
            stats.updated += 1
            results.append(primary)
        else:
            peak_pred = cluster.peak.payload[0]
            event = SwellEvent(
                spot_id=spot.id,
                status=SwellEventStatus.ACTIVE,
                start_time=cluster.start,
                end_time=cluster.end,
                peak_time=peak_pred.valid_time,
                peak_score=peak_pred.score,
                avg_score=0.0,
                peak_label=QualityLabel(label_for_score(peak_pred.score)),
                peak_breaking_height_min_ft=0,
                peak_breaking_height_max_ft=0,
                qualifying_hours=0,
                confidence=0,
                confidence_label=confidence_label(0),
                source_code=run.source.code,
                first_detected_at=detection.utcnow(),
                version=1,
                history=[],
            )
            _reference_apply_cluster(event, cluster, run, keep_start=False)
            db.add(event)
            stats.created += 1
            results.append(event)
    for e in existing:
        if e.id in touched:
            continue
        e.last_updated_at = detection.utcnow()
        if e.end_time < now:
            e.status = SwellEventStatus.PASSED
            stats.passed += 1
        else:
            e.status = SwellEventStatus.DOWNGRADED
            e.latest_run_id = run.id
            stats.downgraded += 1
    return results


def reference_archive_rows(db: Session, run_id: int, spot_ids: list[int]) -> list[dict[str, Any]]:
    """The rows archive_authoritative built before Upgrade 4: every (prediction, forecast)
    of the run read whole, filtered in Python (3-hourly times in the session's time zone,
    UTC here and on Neon; -6..240 h lead)."""
    run = latest_runs_by_spot(db, NOW)[spot_ids[0]]
    assert run.id == run_id
    use_digest = get_settings().archive_confidence_digest
    rows = []
    for pred, fc in db.execute(
        select(SurfQualityPrediction, WaveForecast)
        .join(WaveForecast, WaveForecast.id == SurfQualityPrediction.forecast_id)
        .where(SurfQualityPrediction.run_id == run_id, SurfQualityPrediction.spot_id.in_(spot_ids))
    ):
        lead = (pred.valid_time - run.issued_at).total_seconds() / 3600.0
        if pred.valid_time.hour % 3 or not -6 <= lead <= 240:
            continue
        rows.append(
            {
                "spot_id": pred.spot_id,
                "source_code": run.source.code,
                "run_key": run.run_key,
                "issued_at": run.issued_at,
                "valid_time": pred.valid_time,
                "lead_hours": round(lead, 2),
                **{k: getattr(fc, k) for k in archive.OFFSHORE_FIELDS},
                "breaking_height_mid_ft": pred.breaking_height_mid_ft,
                "breaking_p10_ft": pred.breaking_p10_ft,
                "breaking_p90_ft": pred.breaking_p90_ft,
                "uncertainty_kind": pred.uncertainty_kind,
                "score": pred.score,
                "confidence": pred.confidence,
                "method": pred.breaking_height_method,
                "algorithm_version": pred.algorithm_version,
                "ml_model_version": pred.ml_model_version,
                "forecast_confidence_class": pred.forecast_confidence_class,
                "local_confidence_class": pred.local_confidence_class,
                "research_class": archive._research_class(pred.confidence_inputs),
                "confidence_method": pred.confidence_method,
                "confidence_inputs": (
                    archive.confidence_digest(pred.confidence_inputs)
                    if use_digest
                    else pred.confidence_inputs
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------- dataset
@pytest.fixture
def dataset(db: Session, few_spots: list[str]) -> list[SavedSearch]:
    """Demo forecasts for five spots, a simulated swell at Jeffreys Bay 7 days ahead,
    predictions (and with them the archive), and saved searches that exercise every rule
    matching judges a step on (height, quality, period, wind speed and direction class,
    swell direction, consistency)."""
    from app.models import NotificationPreference, User, UserProfile

    acquire_all(db, now=NOW)
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "jeffreys-bay"))
    assert spot is not None
    outcome = simulate_swell(db, spot, start=SWELL_START, duration_hours=60, now=NOW)
    assert outcome.status == "success"
    assert compute_pending_predictions(db, NOW)["predictions"] > 0
    user = User(
        email="egress-columns@example.com",
        full_name="Egress columns (test)",
        password_hash="test-only-not-a-hash",
        password_changed_at=NOW,
        email_verified_at=NOW,
    )
    user.profile = UserProfile()
    user.notification_preferences = NotificationPreference(unsubscribe_token="t" * 24)
    db.add(user)
    db.commit()
    variants = [
        {"name": "Any J-Bay swell", "min_quality": "fair", "wave_min": 1, "wave_max": 30},
        {
            "name": "Strict",
            "wave_min": 3,
            "wave_max": 25,
            "min_period_s": 10,
            "max_wind_kmh": 40,
            "wind_requirement": "not_onshore",
            "swell_direction_min": 150,
            "swell_direction_max": 260,
            "min_consistency": 40,
        },
        {
            "name": "Everywhere offshore",
            "destination_mode": "all",
            "destinations": {},
            "min_quality": "fair",
            "wave_min": 1,
            "wave_max": 30,
            "wind_requirement": "offshore",
            "min_consistency": 10,
        },
    ]
    return [
        searches.create_search(db, user, SearchIn.model_validate(search_payload(**v)))
        for v in variants
    ]


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (detection, matching):
        monkeypatch.setattr(module, "utcnow", lambda: FROZEN)


def _natural(e: SwellEvent) -> tuple[Any, ...]:
    """An event's identity across the two runs (database ids differ between them)."""
    return (e.spot_id, e.start_time, e.end_time, str(e.status), e.version)


def _events(db: Session) -> list[dict[str, Any]]:
    db.expire_all()
    rows = list(db.scalars(select(SwellEvent)))
    by_id = {e.id: e for e in rows}
    out = []
    for e in rows:
        values = {c.key: getattr(e, c.key) for c in SwellEvent.__table__.columns}
        values.pop("id")
        merged = values.pop("merged_into_id")
        values["merged_into"] = None if merged is None else _natural(by_id[merged])
        out.append(values)
    return sorted(out, key=lambda v: repr([v[k] for k in sorted(v)]))


def _matches(db: Session) -> list[dict[str, Any]]:
    db.expire_all()
    events = {e.id: _natural(e) for e in db.scalars(select(SwellEvent))}
    out = []
    for m in db.scalars(select(OpportunityMatch)):
        values = {c.key: getattr(m, c.key) for c in OpportunityMatch.__table__.columns}
        for volatile in ("id", "created_at", "updated_at"):
            values.pop(volatile)
        values["swell_event_id"] = events[m.swell_event_id]
        out.append(values)
    return sorted(out, key=lambda v: repr([v[k] for k in sorted(v)]))


def _detect_and_match(
    db: Session,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Detection twice (create, then update in place an hour later) and matching once."""
    first = detection.detect_events(db, NOW).as_dict()
    again = detection.detect_events(db, NOW + timedelta(hours=1)).as_dict()
    matched = matching.match_all(db, NOW + timedelta(hours=1)).__dict__
    return (
        _events(db),
        _matches(db),
        {
            **first,
            **{f"again_{k}": v for k, v in again.items()},
            **{f"match_{k}": v for k, v in matched.items()},
        },
    )


def _reset(db: Session) -> None:
    db.execute(delete(OpportunityMatch))
    db.execute(delete(SwellEvent))
    db.execute(update(SavedSearch).values(last_evaluated_at=None))
    db.commit()


# --------------------------------------------------------------------------- tests
def test_detection_and_matching_are_unchanged_by_column_selection(
    db: Session,
    dataset: list[SavedSearch],
    frozen_clock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as m:
        m.setattr(detection, "detect_for_spot", reference_detect_for_spot)
        m.setattr(detection, "load_steps", reference_load_steps)
        m.setattr(matching, "load_steps", reference_load_steps)
        ref_events, ref_matches, ref_stats = _detect_and_match(db)
    _reset(db)
    events, matches, stats = _detect_and_match(db)

    # The dataset exercises what is compared: events at several spots, the simulated swell
    # matched by more than one search, events updated in place by the second detection.
    assert len({e["spot_id"] for e in ref_events}) >= 2 and ref_stats["again_updated"] > 0
    assert len({m["search_id"] for m in ref_matches}) >= 2
    assert any(
        e["history"] and e["history"][-1].get("forecast_confidence_class") for e in ref_events
    )
    assert stats == ref_stats
    assert events == ref_events
    assert matches == ref_matches


def test_the_archive_is_unchanged_by_column_selection(db: Session, dataset: list[Any]) -> None:
    # The old filter read the hour in the session's time zone; the new one reads it in UTC.
    assert db.execute(text("SHOW TimeZone")).scalar() in ("UTC", "Etc/UTC", "GMT")
    columns = [
        c.key for c in PredictionArchive.__table__.columns if c.key not in ("id", "archived_at")
    ]
    stored = sorted(
        (tuple(getattr(r, c) for c in columns) for r in db.scalars(select(PredictionArchive))),
        key=lambda t: (t[columns.index("run_key")], t[0], t[columns.index("valid_time")]),
    )
    assert stored, "the dataset archives rows"
    expected: list[tuple[Any, ...]] = []
    by_run: dict[int, list[int]] = {}
    for spot_id, run in latest_runs_by_spot(db, NOW).items():
        by_run.setdefault(run.id, []).append(spot_id)
    for run_id, spot_ids in by_run.items():
        rows = reference_archive_rows(db, run_id, spot_ids)
        expected += [tuple(r[c] for c in columns) for r in rows]
    expected.sort(key=lambda t: (t[columns.index("run_key")], t[0], t[columns.index("valid_time")]))
    assert stored == expected


@pytest.fixture
def statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def capture(*args: Any) -> None:
        seen.append(str(args[2]))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    yield seen
    event.remove(engine, "before_cursor_execute", capture)


HEAVY = (
    "surf_quality_predictions.components,",
    "surf_quality_predictions.explanation",
    "surf_quality_predictions.confidence_inputs",
    "wave_forecasts.extra",
    "wave_forecasts_1.",  # the prediction's joined ``forecast`` relationship
)


def test_load_steps_reads_only_the_step_columns(
    db: Session, dataset: list[Any], statements: list[str]
) -> None:
    spot_id, run = next(iter(latest_runs_by_spot(db, NOW).items()))
    steps = load_steps(db, spot_id, run.id, NOW - timedelta(hours=6))
    assert steps
    reads = [s for s in statements if "FROM surf_quality_predictions" in s]
    assert reads and not any(h in s for s in reads for h in HEAVY), reads
    # Only the consistency share of components (a jsonb subscript), not the whole document.
    assert any("surf_quality_predictions.components[" in s for s in reads)

    pred, fc = steps[0].payload
    assert isinstance(pred, StepPrediction) and isinstance(fc, StepForecast)
    # An attribute that was not selected fails loudly instead of loading anything.
    for name in ("explanation", "confidence_inputs", "forecast"):
        with pytest.raises(AttributeError):
            getattr(pred, name)
    with pytest.raises(AttributeError):
        fc.extra  # noqa: B018
    # components carries only the consistency share, which matching's rule reads.
    whole = db.get(SurfQualityPrediction, pred.id)
    assert whole is not None
    consistency = (whole.components or {}).get("consistency")
    assert pred.components == ({} if consistency is None else {"consistency": consistency})
    criteria = SearchCriteria(wave_min_ft=0, wave_max_ft=100, min_score=0, min_consistency=1)
    assert step_meets(pred, fc, criteria) == step_meets(whole, whole.forecast, criteria)


def test_the_archive_reads_only_the_archived_columns(
    db: Session, dataset: list[Any], statements: list[str]
) -> None:
    db.execute(delete(PredictionArchive))
    db.commit()
    for run in {r.id: r for r in latest_runs_by_spot(db, NOW).values()}.values():
        run.details = {k: v for k, v in run.details.items() if k != "archived_spot_ids"}
    db.commit()
    statements.clear()
    assert archive.archive_authoritative(db, NOW)["archived"] > 0
    reads = [s for s in statements if "FROM surf_quality_predictions" in s]
    assert reads and not any(h in s for s in reads for h in HEAVY[:2] + HEAVY[3:]), reads
    # The 3-hourly grid and the lead window are applied by the database.
    assert any("% " in s and "timezone(" in s for s in reads)
