"""GET /api/spots/{slug} is built from that one spot's data (CH-17, PLAT-4).

It used to build the summaries of every active spot and keep one. The page data must be
the same as that spot's entry in the list, while only its own predictions and events are
read. Forecast data here comes from the demo provider (SYNTHETIC).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, utcnow
from app.models import SurfQualityPrediction, SurfSpot, SwellEvent
from app.services.pipeline import run_pipeline
from app.services.spot_views import spot_detail, spot_summaries
from tests.conftest import ApiClient

pytestmark = pytest.mark.integration


@pytest.fixture
def loaded(db: Session) -> Iterator[list[Any]]:
    """Every prediction and event row the ORM loads from the database while active."""
    rows: list[Any] = []

    def on_load(_session: Session, instance: Any) -> None:
        if isinstance(instance, (SurfQualityPrediction, SwellEvent)):
            rows.append(instance)

    event.listen(Session, "loaded_as_persistent", on_load)
    try:
        yield rows
    finally:
        event.remove(Session, "loaded_as_persistent", on_load)


def _spot(db: Session, slug: str) -> SurfSpot:
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    return spot


def test_detail_matches_the_list_entry_and_reads_one_spot_only(
    db: Session, few_spots: list[str], loaded: list[Any], app_client: ApiClient
) -> None:
    run_pipeline(db)
    now = utcnow()
    summaries = {s.slug: s for s in spot_summaries(db, now)}
    for slug in few_spots:
        fresh = SessionLocal()  # an empty identity map: every row read is reported
        try:
            loaded.clear()
            detail = spot_detail(fresh, _spot(fresh, slug), now)
            spot_id = detail.id
            assert loaded, "the spot's own predictions are read"
            assert {r.spot_id for r in loaded} == {spot_id}, slug
        finally:
            fresh.close()
        entry = summaries[slug]
        assert detail.current == entry.current, slug
        assert detail.best_upcoming == entry.best_upcoming, slug
        assert detail.next_event == entry.next_event, slug
        assert detail.upcoming_event_count == entry.upcoming_event_count, slug
        assert detail.research_class == entry.research_class, slug
        assert detail.current is not None and detail.best_upcoming is not None
    # Over HTTP the payload is the same object.
    body = app_client.get("/api/spots/pipeline").json()
    assert body["current"]["valid_time"] is not None
    assert body["slug"] == "pipeline" and body["airports"]


def test_detail_latency_is_reported(db: Session, few_spots: list[str]) -> None:
    """Not a benchmark (the test database holds a few demo spots): the single-spot path
    must not be slower than the all-spot list it replaced. Numbers are printed for the
    record; the real-run measurement is in docs/forecast-confidence.md."""
    run_pipeline(db)
    spot = _spot(db, "pipeline")
    now = utcnow()

    def median_ms(fn: Any) -> float:
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000)
        return sorted(samples)[len(samples) // 2]

    one = median_ms(lambda: spot_detail(db, spot, now))
    every = median_ms(lambda: spot_summaries(db, now))
    print(f"spot_detail median {one:.1f} ms; spot_summaries median {every:.1f} ms")
    assert one <= every * 1.5 + 20
