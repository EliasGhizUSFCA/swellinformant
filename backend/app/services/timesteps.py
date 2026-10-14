"""Forecast time steps: how long each step lasts, where the spacing changes, local days.

Forecast series are not evenly spaced (e.g. hourly to f120, then 3-hourly; ECMWF 3-hourly,
then 6-hourly). A step stands for the interval from its valid time to the next valid time,
so counts of steps are not hours: :func:`weighted_hours` sums durations instead. Pure
functions; times must be timezone-aware and sorted ascending.
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


def segments(times: Sequence[datetime]) -> list[StepSegment]:
    """The series as runs of equal step durations (consecutive equal gaps merged)."""
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


def weighted_hours(
    times: Sequence[datetime],
    mask: Sequence[bool],
    *,
    tz: str | None = None,
    day: date | None = None,
) -> float:
    """Total duration (hours) of the steps where ``mask`` is true.

    With ``tz`` and ``day`` (both or neither), each step's interval ``[t, t + duration)`` is
    clipped to that local day, so a 3-hourly step from 22:00 counts 2 hours towards its day.
    """
    if len(times) != len(mask):
        raise ValueError("times and mask must have the same length")
    if (tz is None) != (day is None):
        raise ValueError("pass tz and day together")
    bounds = local_day_bounds(day, tz) if tz is not None and day is not None else None
    total = 0.0
    for t, hours, on in zip(times, step_hours(times), mask, strict=True):
        if not on:
            continue
        if bounds is None:
            total += hours
            continue
        start, end = max(t, bounds[0]), min(t + timedelta(hours=hours), bounds[1])
        if end > start:
            total += (end - start).total_seconds() / 3600.0
    return total
