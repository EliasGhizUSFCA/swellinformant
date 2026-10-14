"""Forecast step durations, spacing segments and DST-aware local days (services/timesteps)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.timesteps import (
    StepSegment,
    local_day_bounds,
    segments,
    step_hours,
    weighted_hours,
)

T0 = datetime(2026, 10, 24, 0, tzinfo=UTC)


def hours(*offsets: float) -> list[datetime]:
    return [T0 + timedelta(hours=h) for h in offsets]


# A mixed series: hourly to +3 h, 3-hourly to +12 h, then 6-hourly to +24 h.
MIXED = hours(0, 1, 2, 3, 6, 9, 12, 18, 24)


def test_step_hours_are_the_gap_to_the_next_time_and_the_median_gap_last() -> None:
    assert step_hours(MIXED) == [1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 6.0, 6.0, 3.0]
    assert step_hours(hours(0, 3, 6)) == [3.0, 3.0, 3.0]
    assert step_hours(hours(0)) == [0.0]
    assert step_hours([]) == []


def test_segments_merge_consecutive_equal_steps() -> None:
    assert segments(MIXED) == [
        StepSegment(T0, T0 + timedelta(hours=3), 1.0),
        StepSegment(T0 + timedelta(hours=3), T0 + timedelta(hours=12), 3.0),
        StepSegment(T0 + timedelta(hours=12), T0 + timedelta(hours=24), 6.0),
        # The last time has no next one: it lasts the median gap (3 h), its own segment.
        StepSegment(T0 + timedelta(hours=24), T0 + timedelta(hours=27), 3.0),
    ]
    # Segments are contiguous: each ends where the next starts.
    segs = segments(MIXED)
    assert all(a.end == b.start for a, b in zip(segs, segs[1:], strict=False))
    assert segments(hours(0, 3, 6, 9)) == [StepSegment(T0, T0 + timedelta(hours=12), 3.0)]
    assert segments([]) == []


def test_lisbon_fall_back_day_is_25_hours() -> None:
    # 2026-10-25: Europe/Lisbon leaves summer time (UTC+1 -> UTC+0) at 02:00 local.
    start, end = local_day_bounds(date(2026, 10, 25), "Europe/Lisbon")
    assert start == datetime(2026, 10, 24, 23, tzinfo=UTC)
    assert end == datetime(2026, 10, 26, 0, tzinfo=UTC)
    assert end - start == timedelta(hours=25)
    # An ordinary day either side is 24 h.
    a, b = local_day_bounds(date(2026, 10, 24), "Europe/Lisbon")
    assert b - a == timedelta(hours=24) and b == start


def test_new_york_spring_forward_day_is_23_hours() -> None:
    # 2026-03-08: America/New_York starts daylight time (UTC-5 -> UTC-4) at 02:00 local.
    start, end = local_day_bounds(date(2026, 3, 8), "America/New_York")
    assert start == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


def test_a_skipped_local_midnight_starts_the_day_at_the_clock_change() -> None:
    # America/Santiago moves its clocks from 00:00 to 01:00 (UTC-4 -> UTC-3) on 2026-09-06:
    # the local day begins at 01:00 = 04:00 UTC and is 23 h long.
    start, end = local_day_bounds(date(2026, 9, 6), "America/Santiago")
    assert start == datetime(2026, 9, 6, 4, tzinfo=UTC)
    assert end - start == timedelta(hours=23)


def test_weighted_hours_sums_step_durations() -> None:
    mask = [True, False, True, True, False, True, False, True, True]
    # 1 + 1 + 3 + 3 + 6 + 3 (the last step: median gap) = 17
    assert weighted_hours(MIXED, mask) == pytest.approx(17.0)
    assert weighted_hours(MIXED, [False] * len(MIXED)) == 0.0
    # Two good 3-hourly steps are 6 hours; two good hourly steps are 2.
    assert weighted_hours(hours(0, 3, 6, 9), [False, True, True, False]) == 6.0
    assert weighted_hours(hours(0, 1, 2, 3), [False, True, True, False]) == 2.0


def test_weighted_hours_are_clipped_to_the_local_day() -> None:
    # 3-hourly steps across the Lisbon fall-back day (local midnight = 23:00Z on the 24th).
    times = [datetime(2026, 10, 24, 21, tzinfo=UTC) + timedelta(hours=3 * i) for i in range(10)]
    everything = [True] * len(times)
    day = date(2026, 10, 25)
    # The 21Z step covers 21-24Z, of which 23-24Z is on the local 25th (1 h); the steps from
    # 00Z to 21Z on the 25th add 8 x 3 h; the last one (00Z on the 26th) is the next day.
    assert weighted_hours(times, everything, tz="Europe/Lisbon", day=day) == pytest.approx(25.0)
    # Lisbon is UTC+1 on the 24th, whose local day ends at 23Z: 2 h of the first step.
    previous = weighted_hours(times, everything, tz="Europe/Lisbon", day=date(2026, 10, 24))
    assert previous == pytest.approx(2.0)
    # Only the step that straddles local midnight: 1 of its 3 hours is on the 25th.
    only_first = [True] + [False] * (len(times) - 1)
    assert weighted_hours(times, only_first, tz="Europe/Lisbon", day=day) == pytest.approx(1.0)


def test_weighted_hours_checks_its_arguments() -> None:
    with pytest.raises(ValueError, match="same length"):
        weighted_hours(MIXED, [True])
    with pytest.raises(ValueError, match="together"):
        weighted_hours(MIXED, [True] * len(MIXED), tz="Europe/Lisbon")
