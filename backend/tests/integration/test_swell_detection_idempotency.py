"""Swell detection over unchanged data keeps every event (Upgrade 4 integration).

The forecasts are the DEMO provider's (SYNTHETIC, labelled demo in the app) with the wall
clock frozen at CLOCK. On that date the demo's natural swells give at least one spot two
events within detection's 12 h matching tolerance (checked below): the case on which the
greedy cluster-to-event matching cancelled the second event and created it again on every
pass (one cancelled row per sync, seen live and in test_map_fields on some dates).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import SurfSpot, SwellEvent
from app.models.enums import QualityLabel, SwellEventStatus
from app.services.forecasts.queries import latest_runs_by_spot
from app.services.pipeline import run_pipeline
from app.services.surf_quality.confidence import confidence_label
from app.services.swell_detection import service as detection
from app.services.swell_detection.clustering import Cluster, cluster_steps
from tests.clock import freeze_utcnow

pytestmark = pytest.mark.integration

CLOCK = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> datetime:
    freeze_utcnow(monkeypatch, CLOCK)
    return CLOCK


def _events(db: Session) -> dict[int, dict[str, Any]]:
    db.expire_all()
    return {
        e.id: {
            "spot_id": e.spot_id,
            "status": e.status,
            "start": e.start_time,
            "end": e.end_time,
            "peak": (e.peak_time, e.peak_score),
            "merged_into_id": e.merged_into_id,
            "version": e.version,
        }
        for e in db.scalars(select(SwellEvent))
    }


def _close_pairs(events: dict[int, dict[str, Any]]) -> int:
    """Pairs of active events at one spot within the matching tolerance of each other."""
    active = sorted(
        (e["spot_id"], e["start"], e["end"])
        for e in events.values()
        if e["status"] == SwellEventStatus.ACTIVE
    )
    return sum(
        1
        for (s1, _, end1), (s2, start2, _) in zip(active, active[1:], strict=False)
        if s1 == s2 and start2 - end1 <= detection.MATCH_TOLERANCE
    )


def _clusters(db: Session, spot_id: int, now: datetime) -> list[Cluster]:
    """The clusters detection finds for the spot (same calls as detect_for_spot)."""
    settings = get_settings()
    run = latest_runs_by_spot(db, now)[spot_id]
    steps = detection.load_steps(db, spot_id, run.id, since=now - timedelta(hours=6))
    return cluster_steps(
        steps,
        qualifies=lambda s: s.score >= settings.swell_event_min_score,
        min_hours=settings.swell_event_min_hours,
        break_daylight_hours=settings.swell_event_break_daylight_hours,
    )


def test_detecting_twice_over_unchanged_data_keeps_every_event(
    db: Session, few_spots: list[str], frozen: datetime
) -> None:
    run_pipeline(db, now=frozen)
    before = _events(db)
    assert _close_pairs(before) >= 1  # the dataset exercises the ambiguous case

    first = detection.detect_events(db, frozen).as_dict()
    second = detection.detect_events(db, frozen).as_dict()
    after = _events(db)

    for stats in (first, second):
        assert stats["created"] == stats["merged"] == stats["errors"] == 0
        assert stats["updated"] == len(before)
    assert after.keys() == before.keys()  # no event created, none cancelled
    for event_id, was in before.items():
        now = after[event_id]
        assert now["version"] == was["version"] + 2  # re-detected in place, twice
        assert {k: v for k, v in now.items() if k != "version"} == {
            k: v for k, v in was.items() if k != "version"
        }


def test_an_event_from_before_the_run_split_by_it_never_overlaps_a_new_one(
    db: Session, few_spots: list[str], frozen: datetime
) -> None:
    """An event that began before the run's coverage keeps its start only for the first
    cluster: continued by a later, larger cluster it starts with that cluster, so the event
    created for the earlier cluster does not overlap it (the exclusion constraint)."""
    run_pipeline(db, now=frozen)
    candidates = [
        (spot_id, clusters)
        for spot_id in sorted({e["spot_id"] for e in _events(db).values()})
        if len(clusters := _clusters(db, spot_id, frozen)) >= 2
        and max(range(len(clusters)), key=lambda i: clusters[i].end - clusters[i].start) > 0
    ]
    assert candidates  # a spot whose largest cluster is not its first
    spot_id, clusters = candidates[0]
    largest = max(clusters, key=lambda c: c.end - c.start)
    db.execute(delete(SwellEvent).where(SwellEvent.spot_id == spot_id))
    # One made-up event from a day before the run up to the end of the largest cluster: it
    # overlaps every cluster up to that one, the largest most.
    spanning = SwellEvent(
        spot_id=spot_id,
        status=SwellEventStatus.ACTIVE,
        start_time=frozen - timedelta(days=1),
        end_time=largest.end,
        peak_time=largest.start,
        peak_score=80,
        avg_score=80.0,
        peak_label=QualityLabel.GOOD,
        peak_breaking_height_min_ft=3,
        peak_breaking_height_max_ft=4,
        qualifying_hours=6,
        confidence=50,
        confidence_label=confidence_label(50),
        source_code="demo",
        first_detected_at=frozen - timedelta(days=1),
        version=1,
        history=[],
    )
    db.add(spanning)
    db.commit()

    spot = db.get(SurfSpot, spot_id)
    assert spot is not None
    stats = detection.DetectionStats()
    detection.detect_for_spot(db, spot, latest_runs_by_spot(db, frozen)[spot_id], frozen, stats)
    db.commit()  # the deferred exclusion constraint is checked here

    db.expire_all()
    kept = db.get(SwellEvent, spanning.id)
    assert kept is not None and kept.status == SwellEventStatus.ACTIVE
    assert (kept.start_time, kept.end_time) == (largest.start, largest.end)
    assert stats.created == len(clusters) - 1 and stats.merged == 0
    overlapping = db.scalar(
        text(
            "SELECT count(*) FROM swell_events a JOIN swell_events b ON a.id < b.id "
            "AND a.spot_id = b.spot_id AND a.status = 'active' AND b.status = 'active' "
            "AND tstzrange(a.start_time, a.end_time, '[]') && "
            "tstzrange(b.start_time, b.end_time, '[]')"
        )
    )
    assert overlapping == 0
    spot_events = select(func.count()).where(SwellEvent.spot_id == spot_id)
    assert db.scalar(spot_events) == len(clusters)
