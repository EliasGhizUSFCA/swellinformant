"""Forecast time steps: how long each step lasts, where the spacing changes, local days.

Forecast series are not evenly spaced (e.g. hourly to f120, then 3-hourly; ECMWF 3-hourly,
then 6-hourly). A step's *duration* is the gap to the next valid time (:func:`step_hours`,
:func:`forward_hours`); under the nearest-valid-step rule that the charts, the map and the
selected time use, a step stands for the interval halfway back to the previous valid time
and halfway on to the next (:func:`centred_extents`). Counts of steps are not hours:
:func:`overlap_hours` and :func:`covered_hours` measure intervals instead.
Pure functions; times must be timezone-aware and sorted ascending.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Gaps closer than this (hours, about 4 ms) count as equal.
_EQUAL_HOURS = 1e-6


def step_hours(times: Sequence[datetime]) -> list[float]:
    """Duration of each step in hours: the gap to the next time.

    The last step has no next time and gets the median gap of the series. A single time
    gives ``[0.0]`` (its duration is unknown) and an empty series ``[]``.
    """
    if not times:
        return []
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(times, times[1:], strict=False)]
    if not gaps:
        return [0.0]
    return [*gaps, float(statistics.median(gaps))]


@dataclass(frozen=True)
class StepSegment:
    """Consecutive steps of one duration: from the first step's time (``start``) to the end
    of the last step's interval (``end``)."""

    start: datetime
    end: datetime
    step_hours: float


def segments(
    times: Sequence[datetime], durations: Sequence[float] | None = None
) -> list[StepSegment]:
    """The series as runs of equal step durations (consecutive equal durations merged).
    ``durations`` are the steps' hours, :func:`step_hours` by default."""
    if durations is None:
        durations = step_hours(times)
    out: list[StepSegment] = []
    for t, hours in zip(times, durations, strict=True):
        end = t + timedelta(hours=hours)
        if out and abs(out[-1].step_hours - hours) <= _EQUAL_HOURS:
            out[-1] = StepSegment(out[-1].start, end, out[-1].step_hours)
        else:
            out.append(StepSegment(t, end, hours))
    return out


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC instants of local midnight at the start of ``day`` and of the next day in ``tz``.

    The day is 23 or 25 hours long when the clocks change. Where midnight itself is skipped
    (a change at 00:00), the day starts at the first local time that exists.
    """
    zone = ZoneInfo(tz)
    return _local_midnight(day, zone), _local_midnight(day + timedelta(days=1), zone)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    # fold=0: a repeated midnight is its first occurrence; a skipped one maps to the instant
    # the clocks jumped, which is the first instant of that local day.
    return datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)


def forward_hours(times: Sequence[datetime], after: datetime | None = None) -> list[float]:
    """Duration of each step (gap to the next time). The last step lasts until ``after``
    (the series' next time beyond ``times``) when given; otherwise it keeps the spacing of
    the step before it (the end of a 3-hourly-then-6-hourly series lasts 6 h, not the
    median). A single time without ``after`` gives ``[0.0]``."""
    if not times:
        return []
    if after is not None:
        return step_hours([*times, after])[: len(times)]
    hours = step_hours(times)
    if len(hours) >= 2:
        hours[-1] = hours[-2]
    return hours


Interval = tuple[datetime, datetime]


def centred_extents(times: Sequence[datetime], durations: Sequence[float]) -> list[Interval]:
    """The interval each step stands for under the nearest-valid-step rule: from halfway
    back to the previous valid time to halfway on to the next (``durations[i]`` hours after
    ``times[i]``). The first step reaches back by half its own duration; a step of 0 h has
    an empty interval. This is how the spot charts draw a step (frontend
    ``lib/timeline.stepExtents``)."""
    if len(times) != len(durations):
        raise ValueError("times and durations must have the same length")
    out: list[Interval] = []
    for i, (t, hours) in enumerate(zip(times, durations, strict=True)):
        ahead = timedelta(hours=max(hours, 0.0))
        back = (t - times[i - 1]) if i > 0 else ahead
        out.append((t - back / 2, t + ahead / 2))
    return out


def overlap_hours(interval: Interval, windows: Sequence[Interval]) -> float:
    """Hours of ``interval`` inside the (non-overlapping) ``windows``."""
    total = 0.0
    for a, b in windows:
        start, end = max(interval[0], a), min(interval[1], b)
        if end > start:
            total += (end - start).total_seconds() / 3600.0
    return total


def clip(interval: Interval, bounds: Interval) -> Interval:
    """``interval`` limited to ``bounds`` (empty, start == end, when they do not meet)."""
    start, end = max(interval[0], bounds[0]), min(interval[1], bounds[1])
    return (start, end) if end > start else (start, start)


def covered_hours(
    extents: Sequence[Interval],
    mask: Sequence[bool],
    windows: Sequence[Interval],
    bounds: Interval,
) -> float:
    """Hours inside ``bounds`` (e.g. a local day) and ``windows`` (e.g. first to last light)
    covered by the intervals (``centred_extents``) of the steps where ``mask`` is true. The
    intervals do not overlap, so this never exceeds the windows' length inside ``bounds``."""
    if not (len(extents) == len(mask)):
        raise ValueError("extents and mask must have the same length")
    return sum(
        overlap_hours(clip(extent, bounds), windows)
        for extent, on in zip(extents, mask, strict=True)
        if on
    )


def mostly_inside(extent: Interval, windows: Sequence[Interval], share: float = 0.5) -> bool:
    """At least ``share`` of ``extent`` is inside ``windows`` (an empty extent counts when
    its instant is)."""
    length = (extent[1] - extent[0]).total_seconds() / 3600.0
    if length <= 0:
        return any(a <= extent[0] <= b for a, b in windows)
    return overlap_hours(extent, windows) >= share * length
