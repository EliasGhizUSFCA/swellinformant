"""GET /api/spots/{slug}/timeline: same run and values as /forecast, native steps, local
days, caching headers (Cache-Control, ETag, 304) and the explicit "unavailable" states."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource, SurfQualityPrediction, SurfSpot
from app.models.enums import ForecastRunStatus
from app.schemas.timeline import SpotTimelineOut
from app.services import timesteps
from app.services.pipeline import run_pipeline
from app.services.tides.api import NO_TIDE_SOURCE
from app.services.timeline import NO_FORECAST
from tests.conftest import ApiClient
from tests.timeline_fixtures import build_rows, ecmwf_shaped_times, jbay_doc

pytestmark = pytest.mark.integration


def _timeline(client: ApiClient, slug: str, days: int = 10) -> SpotTimelineOut:
    resp = client.get(f"/api/spots/{slug}/timeline", params={"days": days})
    assert resp.status_code == 200, resp.text
    return SpotTimelineOut.model_validate(resp.json())


def _store_run(db: Session, slug: str, times: list[datetime] | None, shift: timedelta) -> int:
    """Store the J-Bay fixture rows (shifted by whole days so they lie around now, or
    re-timed to ``times``: synthetic) as a run of the configured demo source."""
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    assert spot is not None
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == "demo"))
    if source is None:
        source = ForecastSource(code="demo", name="Demo", provider="demo", is_demo=True)
        db.add(source)
        db.flush()
    rows = build_rows([t + shift for t in times] if times else None)
    if times is None:
        for p, f in rows:
            p.valid_time += shift
            f.valid_time = p.valid_time
    now = utcnow()
    run = ForecastRun(
        source_id=source.id,
        run_key=f"fixture:{jbay_doc()['run']['run_key']}",
        issued_at=now - timedelta(hours=6),
        completed_at=now,
        status=ForecastRunStatus.SUCCESS,
        details={"spot_ids": [spot.id]},
        predictions_computed_at=now,
    )
    db.add(run)
    db.flush()
    for p, f in rows:
        f.run_id, f.spot_id, f.issued_at = run.id, spot.id, run.issued_at
        db.add(f)
        db.flush()
        p.run_id, p.spot_id, p.forecast_id = run.id, spot.id, f.id
        db.add(p)
    db.commit()
    return run.id


def _days_shift(first: datetime) -> timedelta:
    """Whole days that move the fixture's first step to just before now (time of day and
    daylight flags stay true to the stored rows)."""
    target = utcnow() - timedelta(hours=2)
    return timedelta(days=(target - first).days)


def test_timeline_without_a_forecast_says_so(app_client: ApiClient) -> None:
    resp = app_client.get("/api/spots/jeffreys-bay/timeline", params={"days": 3})
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.headers["etag"].startswith('W/"')
    body = SpotTimelineOut.model_validate(resp.json())
    assert body.spot_slug == "jeffreys-bay" and body.timezone == "Africa/Johannesburg"
    assert body.available is False and body.message == NO_FORECAST
    assert body.steps == [] and body.days == [] and body.provenance is None
    assert body.steps_meta.segments == [] and body.steps_meta.interpolation == "none"
    assert body.units.model_dump() == {
        "breaking": "ft",
        "offshore": "m",
        "wind": "km/h",
        "tide": "m",
    }
    # The embedded blocks state their own status; nothing is made up.
    assert body.tide.status == "unavailable" and body.tide.reason == NO_TIDE_SOURCE
    assert body.tide.curve == []
    assert body.observations.status == "unavailable" and body.observations.items == []
    assert body.generated_at.tzinfo is not None


def test_timeline_validates_days_and_slug(app_client: ApiClient) -> None:
    assert app_client.get("/api/spots/jeffreys-bay/timeline?days=0").status_code == 422
    assert app_client.get("/api/spots/jeffreys-bay/timeline?days=17").status_code == 422
    missing = app_client.get("/api/spots/no-such-spot/timeline")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_timeline_uses_the_forecast_run_and_its_values(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    run_pipeline(db)
    fc = app_client.get("/api/spots/pipeline/forecast?days=5").json()
    tl = _timeline(app_client, "pipeline", days=5)
    assert tl.available and tl.provenance is not None
    assert tl.provenance.run.run_key == fc["run"]["run_key"]
    assert tl.provenance.source.code == fc["source"]["code"] == "demo"
    assert [m.code for m in tl.provenance.members] == ["demo"]
    assert tl.provenance.algorithm_version == fc["algorithm_version"]
    assert tl.provenance.breaking_method == fc["breaking_height_method"]
    # Same steps, same score and label at every valid time.
    assert [s.t for s in tl.steps] == [
        datetime.fromisoformat(p["valid_time"]) for p in fc["points"]
    ]
    for step, point in zip(tl.steps, fc["points"], strict=True):
        assert (step.quality.score, step.quality.label) == (point["score"], point["label"])
        assert step.breaking.central_ft == point["breaking_height_mid_ft"]
        assert (step.breaking.typical_ft, step.breaking.sets_ft) == (
            point["breaking_height_min_ft"],
            point["breaking_height_max_ft"],
        )
        assert step.quality.forecast_confidence_class == point["forecast_confidence_class"]
    # The steps and their segments are the stored valid times (demo data is hourly).
    run = db.scalar(select(ForecastRun).where(ForecastRun.run_key == fc["run"]["run_key"]))
    assert run is not None
    stored = db.scalars(
        select(SurfQualityPrediction.valid_time)
        .where(
            SurfQualityPrediction.run_id == run.id,
            SurfQualityPrediction.valid_time >= tl.steps[0].t,
        )
        .join(SurfSpot, SurfSpot.id == SurfQualityPrediction.spot_id)
        .where(SurfSpot.slug == "pipeline")
        .order_by(SurfQualityPrediction.valid_time)
    ).all()
    assert [s.t for s in tl.steps] == list(stored[: len(tl.steps)])
    assert {seg.step_hours for seg in tl.steps_meta.segments} == {1.0}
    assert tl.steps_meta.segments[0].start == tl.steps[0].t
    # Every step lies inside one of the spot-local days.
    assert tl.days[0].start_utc <= tl.steps[0].t < tl.days[0].end_utc
    assert tl.steps[-1].t < tl.days[-1].end_utc


def test_timeline_cache_headers_and_conditional_requests(
    app_client: ApiClient, db: Session, few_spots: list[str]
) -> None:
    run_pipeline(db)
    first = app_client.get("/api/spots/pipeline/timeline")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "public, max-age=300"
    etag = first.headers["etag"]
    assert etag.startswith('W/"') and etag.endswith('"')
    again = app_client.get("/api/spots/pipeline/timeline")
    assert again.headers["etag"] == etag  # stable across requests (generated_at differs)
    for header in (etag, etag.removeprefix("W/"), f'"other", {etag}', "*"):
        cached = app_client.get("/api/spots/pipeline/timeline", headers={"If-None-Match": header})
        assert cached.status_code == 304, header
        assert cached.content == b""
        assert cached.headers["etag"] == etag
        assert cached.headers["cache-control"] == "public, max-age=300"
    changed = app_client.get("/api/spots/pipeline/timeline", headers={"If-None-Match": '"old"'})
    assert changed.status_code == 200 and changed.json()["available"] is True
    # Another window is another representation.
    assert app_client.get("/api/spots/pipeline/timeline?days=3").headers["etag"] != etag


def test_timeline_of_a_stored_3_hourly_run(app_client: ApiClient, db: Session) -> None:
    rows = jbay_doc()["rows"]
    first = datetime.fromisoformat(rows[0]["prediction"]["valid_time"])
    shift = _days_shift(first)
    _store_run(db, "jeffreys-bay", None, shift)
    tl = _timeline(app_client, "jeffreys-bay")
    assert tl.available and tl.timezone == "Africa/Johannesburg"
    stored = [datetime.fromisoformat(r["prediction"]["valid_time"]) + shift for r in rows]
    window_start = utcnow() - timedelta(hours=3)
    expected = [t for t in stored if t >= window_start]
    assert [s.t for s in tl.steps] == expected
    assert all(s.step_hours == 3.0 for s in tl.steps)
    assert [(g.start, g.end, g.step_hours) for g in tl.steps_meta.segments] == [
        (expected[0], expected[-1] + timedelta(hours=3), 3.0)
    ]
    # Local midnights in SAST (UTC+2): 22:00 UTC.
    assert all(
        d.start_utc.hour == 22 and d.end_utc - d.start_utc == timedelta(hours=24) for d in tl.days
    )
    # Night is from last light to the next first light, from sun.py, not from step flags.
    for day in tl.days:
        assert day.first_light is not None and day.last_light is not None
        assert day.start_utc < day.first_light < day.last_light < day.end_utc
    # Partitions as stored, the driver flagged; no tide source configured.
    assert all(sum(p.drives_surf for p in s.partitions) == 1 for s in tl.steps)
    assert tl.tide.status == "unavailable" and all(s.tide is None for s in tl.steps)
    assert tl.provenance is not None and tl.provenance.run.stale_after_hours is not None


def test_timeline_of_a_mixed_cadence_run(app_client: ApiClient, db: Session) -> None:
    start = utcnow().replace(minute=0, second=0, microsecond=0)  # inside the window
    times = ecmwf_shaped_times(start, 24, 16)  # synthetic valid times, real values
    _store_run(db, "jeffreys-bay", times, timedelta(0))
    tl = _timeline(app_client, "jeffreys-bay")
    segs = [(g.step_hours, g.start) for g in tl.steps_meta.segments]
    assert segs == [(3.0, times[0]), (6.0, times[23])]
    assert tl.steps_meta.segments[-1].end == tl.steps[-1].t + timedelta(hours=6)
    six = [s for s in tl.steps if s.t >= times[23]]
    assert six and all(s.step_hours == 6.0 for s in six)
    assert timesteps.segments([s.t for s in tl.steps])[0].step_hours == 3.0


def test_timeline_last_step_lasts_until_the_next_stored_time(
    app_client: ApiClient, db: Session
) -> None:
    """With days=1 the window ends inside the run; the last step's length is the gap to the
    run's next stored time, not a guess."""
    start = utcnow().replace(minute=0, second=0, microsecond=0)
    times = ecmwf_shaped_times(start, 9, 16)  # synthetic: 6-hourly from about +24 h
    _store_run(db, "jeffreys-bay", times, timedelta(0))
    tl = _timeline(app_client, "jeffreys-bay", days=2)
    assert tl.steps[-1].t < times[-1]
    assert tl.steps[-1].step_hours == 6.0
