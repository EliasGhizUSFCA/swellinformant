"""Group forecast timesteps into swell events (pure functions, no database).

Rules (documented in docs/SURF_SCORING.md):

* Only daylight steps count; night steps neither qualify nor break an event, so a swell
  that is good on consecutive mornings forms ONE multi-day event.
* A daylight step qualifies when its predicate holds (default: score ≥ threshold).
* An event ends once ``break_daylight_hours`` consecutive daylight hours fail to qualify
  (e.g. a full day of onshore wind splits two swells into two events).
* An event needs at least ``min_hours`` qualifying hours to be kept.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class Step:
    time: datetime
    is_daylight: bool
    score: int
    duration: timedelta = timedelta(hours=1)
    payload: Any = None


@dataclass
class Cluster:
    steps: list[Step] = field(default_factory=list)

    @property
    def start(self) -> datetime:
        return self.steps[0].time

    @property
    def end(self) -> datetime:
        last = self.steps[-1]
        return last.time + last.duration

    @property
    def qualifying_hours(self) -> float:
        return sum(s.duration.total_seconds() for s in self.steps) / 3600.0

    @property
    def peak(self) -> Step:
        return max(self.steps, key=lambda s: (s.score, -s.time.timestamp()))

    @property
    def avg_score(self) -> float:
        return sum(s.score for s in self.steps) / len(self.steps)

    @property
    def score_sum(self) -> float:
        return sum(s.score for s in self.steps)


def cluster_steps(
    steps: Sequence[Step],
    *,
    qualifies: Callable[[Step], bool],
    min_hours: float = 3,
    break_daylight_hours: float = 9,
) -> list[Cluster]:
    ordered = sorted(steps, key=lambda s: s.time)
    clusters: list[Cluster] = []
    current: Cluster | None = None
    failing_daylight = 0.0
    for step in ordered:
        if not step.is_daylight:
            continue
        if qualifies(step):
            if current is None:
                current = Cluster()
            current.steps.append(step)
            failing_daylight = 0.0
        elif current is not None:
            failing_daylight += step.duration.total_seconds() / 3600.0
            if failing_daylight >= break_daylight_hours:
                clusters.append(current)
                current = None
                failing_daylight = 0.0
    if current is not None:
        clusters.append(current)
    return [c for c in clusters if c.qualifying_hours >= min_hours]


def best_cluster(clusters: Sequence[Cluster]) -> Cluster | None:
    """The window with the most surf "value" (sum of scores), ties → earliest."""
    if not clusters:
        return None
    return max(clusters, key=lambda c: (c.score_sum, -c.start.timestamp()))


def overlaps(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
    tolerance: timedelta = timedelta(0),
) -> bool:
    return a_start <= b_end + tolerance and b_start <= a_end + tolerance
