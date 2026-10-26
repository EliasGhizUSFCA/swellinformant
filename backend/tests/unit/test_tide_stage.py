"""Tide stage from predicted high and low waters (real CO-OPS events, tests/fixtures/coops)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.tides import coops
from app.services.tides.stage import MIN_RANGE_M, Event, stage_between, stages_from

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coops"


def _events(name: str) -> list[Event]:
    points = coops.parse_predictions(json.loads((FIXTURES / name).read_text()), "MLLW")
    return [Event(p.t, p.height_m, str(p.event)) for p in points if p.event]


def test_every_low_water_is_0_and_every_high_water_is_1_at_pillar_point() -> None:
    events = _events("hilo14_9414131.json")  # mixed semidiurnal: two unequal lows a day
    stages = stages_from(events, [], [e.t for e in events[1:-1]], "noaa_coops")
    assert len(stages) == len(events) - 2
    for e in events[1:-1]:
        assert stages[e.t].stage == (0.0 if e.kind == "low" else 1.0)
        assert stages[e.t].source == "noaa_coops"
    # The higher low water (0.87 m on 2026-10-14 13:44 UTC) is a low: stage 0, not ~0.5.
    higher_low = next(e for e in events if e.t == datetime(2026, 10, 14, 13, 44, tzinfo=UTC))
    assert higher_low.kind == "low" and higher_low.height_m == pytest.approx(0.87)
    assert stages[higher_low.t].stage == 0.0


def test_rising_and_falling_between_events() -> None:
    events = _events("hilo14_9414131.json")
    curve = [
        (p.t, p.height_m)
        for p in coops.cosine_curve(
            [
                coops.TidePoint(e.t, e.height_m, "MLLW", "high" if e.kind == "high" else "low")
                for e in events
            ],
            timedelta(minutes=30),
            events[0].t.replace(minute=0),
        )
    ]
    times = [t for t, _ in curve if events[0].t < t < events[-1].t]
    stages = stages_from(events, curve, times, "noaa_coops")
    # Every time has a stage except inside a half cycle smaller than MIN_RANGE_M (the small
    # wiggles of this mixed tide: 3 of 53 half cycles in these 14 days).
    small = [
        (a, b)
        for a, b in zip(events, events[1:], strict=False)
        if abs(a.height_m - b.height_m) < MIN_RANGE_M
    ]
    assert len(small) == 3
    missing = {t for t in times if t not in stages}
    assert missing == {t for t in times for a, b in small if a.t < t < b.t}
    for t, st in stages.items():
        following = next(e for e in events if e.t > t)
        assert st.rising == (following.kind == "high")
        assert 0.0 <= st.stage <= 1.0
    # Monotonic within each rising or falling half cycle.
    for a, b in zip(events, events[1:], strict=False):
        inside = [stages[t].stage for t in times if a.t < t < b.t and t in stages]
        diffs = [y - x for x, y in zip(inside, inside[1:], strict=False)]
        assert all(d >= -1e-9 for d in diffs) if a.kind == "low" else all(d <= 1e-9 for d in diffs)


def test_without_a_curve_the_cosine_shape_between_events_is_used() -> None:
    low = Event(datetime(2026, 10, 14, 0, tzinfo=UTC), 0.2, "low")
    high = Event(datetime(2026, 10, 14, 6, tzinfo=UTC), 1.4, "high")
    mid = datetime(2026, 10, 14, 3, tzinfo=UTC)
    result = stage_between(mid, low, high, None)
    assert result is not None
    stage, rising, span = result
    assert stage == pytest.approx(0.5) and rising and span == pytest.approx(1.2)
    # With a height, the stage is the fraction of the range.
    assert stage_between(mid, low, high, 0.5) == pytest.approx((0.25, True, 1.2))
    # Falling: H then L.
    falling = stage_between(mid, Event(low.t, 1.4, "high"), Event(high.t, 0.2, "low"), 1.1)
    assert falling == pytest.approx((0.75, False, 1.2))


def test_a_range_below_15_cm_gives_no_stage() -> None:
    events = _events("hilo_1612668.json")  # Haleiwa: small, mixed tide
    lo = next(
        (a, b)
        for a, b in zip(events, events[1:], strict=False)
        if abs(a.height_m - b.height_m) < MIN_RANGE_M
    )
    middle = lo[0].t + (lo[1].t - lo[0].t) / 2
    big = next(
        (a, b)
        for a, b in zip(events, events[1:], strict=False)
        if abs(a.height_m - b.height_m) > 0.3
    )
    other = big[0].t + (big[1].t - big[0].t) / 2
    stages = stages_from(events, [], [middle, other], "noaa_coops")
    assert middle not in stages and other in stages


def test_times_outside_the_events_are_absent() -> None:
    events = _events("hilo_1612668.json")
    before = events[0].t - timedelta(hours=1)
    after = events[-1].t + timedelta(hours=1)
    assert stages_from(events, [], [before, after], "noaa_coops") == {}
