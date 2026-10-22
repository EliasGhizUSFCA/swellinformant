"""Tide stage at given times, from the stored predictions (used by the quality score).

The stage is read from the predicted high and low waters around each time, not from a
rolling minimum and maximum (which put the higher low of a mixed tide at a stage of 0.5 or
more). With L and H the low and the high water on either side of t (rising when L comes
first, falling when H does)

    stage(t) = (h(t) - h_L) / (h_H - h_L)

so every low water has stage 0, every high water stage 1, whichever of a mixed tide's two
lows or highs it is. h(t) is the stored curve (linear between its 30-minute points); where
the curve has a gap the classical cosine shape between the two events is used. The stage is
clipped to [0, 1] (a curve point can lie a few millimetres beyond its event's height).

One station per call: the spot's first tide link (primary, then fallback) with any stored
high or low water around the times, and only the providers in TIDE_PROVIDERS. Times
without a preceding and a following event, or whose surrounding events differ by less than
MIN_RANGE_M (no usable tide: a mixed tide's small wiggle between a high and a slightly lower
"low"), are absent from the result; a high or low water itself keeps its 1 or 0 when the
half cycle on either side of it is large enough.

Scoring asks for the same spot's stages once per model run of a sync, so inside
:func:`shared_tide_reads` each station's links and predictions are read from the database
once (for a window covering every call) and each call takes the rows of its own window
from them: the same rows, in the same order, as its own query would return.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import SpotTideStation, TidePrediction, TideStation

# Below this range between a low and the next high water there is no usable tide stage.
MIN_RANGE_M = 0.15
# One stored prediction: valid time, height, whether it is a high or low water, which, source.
TideRow = Row[datetime, float, bool, str | None, str]
# High and low waters are at most about 13.5 h apart (a diurnal tide's long half cycle).
EVENT_SEARCH = timedelta(hours=14)
# Curve points further apart than this are a gap, not a curve.
MAX_CURVE_GAP = timedelta(minutes=90)


@dataclass(frozen=True)
class TideStage:
    # 0 at the preceding low water, 1 at the next high water (and back to 0 at the next low).
    stage: float
    rising: bool
    # Height difference between the surrounding low and high water (metres).
    range_m: float
    # What produced the predictions, e.g. "noaa_coops" or "eot20".
    source: str


@dataclass(frozen=True)
class Event:
    t: datetime
    height_m: float
    kind: str  # "high" | "low"


def stage_between(
    t: datetime, before: Event, after: Event, height: float | None
) -> tuple[float, bool, float] | None:
    """(stage, rising, range) at ``t`` between two consecutive events, or None when they do
    not form a low/high pair or their range is below MIN_RANGE_M."""
    if before.kind == after.kind or not before.t <= t <= after.t:
        return None
    low, high = (before, after) if before.kind == "low" else (after, before)
    span = high.height_m - low.height_m
    if span < MIN_RANGE_M:
        return None
    rising = before.kind == "low"
    if height is None:
        total = (after.t - before.t).total_seconds()
        x = 0.0 if total <= 0 else (t - before.t).total_seconds() / total
        shape = (1.0 - math.cos(math.pi * x)) / 2.0
        stage = shape if rising else 1.0 - shape
    else:
        stage = (height - low.height_m) / span
    return min(1.0, max(0.0, stage)), rising, span


def curve_height(times: Sequence[datetime], heights: Sequence[float], t: datetime) -> float | None:
    """The curve at ``t`` (linear between neighbouring points), None in a gap or outside."""
    i = bisect_left(times, t)
    if i < len(times) and times[i] == t:
        return heights[i]
    if i == 0 or i == len(times) or times[i] - times[i - 1] > MAX_CURVE_GAP:
        return None
    frac = (t - times[i - 1]) / (times[i] - times[i - 1])
    return heights[i - 1] + (heights[i] - heights[i - 1]) * frac


def stages_from(
    events: Sequence[Event],
    curve: Sequence[tuple[datetime, float]],
    times: Sequence[datetime],
    source: str,
) -> dict[datetime, TideStage]:
    """Pure part of :func:`tide_stages_for` (events and curve sorted by time)."""
    event_times = [e.t for e in events]
    curve_times = [t for t, _ in curve]
    curve_heights = [h for _, h in curve]
    out: dict[datetime, TideStage] = {}
    for t in times:
        i = bisect_right(event_times, t)
        if i == 0 or i == len(events):
            continue
        before, after = events[i - 1], events[i]
        # At a high or low water its own height (a 30-minute curve passes beside it).
        height = before.height_m if t == before.t else curve_height(curve_times, curve_heights, t)
        result = stage_between(t, before, after, height)
        if result is None and t == before.t and i >= 2:
            # At a high or low water whose following half cycle is too small to use, the
            # preceding one still says where it is (0 or 1); rising as from the event on.
            earlier = stage_between(t, events[i - 2], before, before.height_m)
            if earlier is not None:
                result = (earlier[0], before.kind == "low", earlier[2])
        if result is not None:
            stage, rising, span = result
            out[t] = TideStage(stage=stage, rising=rising, range_m=round(span, 3), source=source)
    return out


def _stations(db: Session, spot_id: int) -> list[int]:
    """The ids of the spot's tide stations to try, in order (primary, then fallback)."""
    providers = get_settings().tide_provider_list
    rows = db.execute(
        select(TideStation.id, SpotTideStation.relation, SpotTideStation.distance_km)
        .join(SpotTideStation, SpotTideStation.station_id == TideStation.id)
        .where(SpotTideStation.spot_id == spot_id, TideStation.provider.in_(providers))
    ).all()
    order = {"primary": 0, "fallback": 1}
    rows = sorted(rows, key=lambda r: (order.get(r.relation or "", 2), r.distance_km))
    return [r[0] for r in rows]


def _predictions(db: Session, station_id: int, lo: datetime, hi: datetime) -> list[TideRow]:
    return list(
        db.execute(
            select(
                TidePrediction.valid_time,
                TidePrediction.height_m,
                TidePrediction.is_event,
                TidePrediction.event_type,
                TidePrediction.source_code,
            )
            .where(
                TidePrediction.station_id == station_id,
                TidePrediction.valid_time >= lo,
                TidePrediction.valid_time <= hi,
            )
            .order_by(TidePrediction.valid_time)
        ).all()
    )


@dataclass
class SharedTideReads:
    """Stations and predictions read once per spot for a whole scoring pass (see the
    module docstring). ``windows``: spot id -> the earliest and latest time any run of the
    pass scores there, so one read per station covers every call."""

    windows: Mapping[int, tuple[datetime, datetime]] = field(default_factory=dict)
    stations: dict[int, list[int]] = field(default_factory=dict)
    # station id -> (lo, hi, rows of lo..hi, ordered by time)
    rows: dict[int, tuple[datetime, datetime, list[TideRow]]] = field(default_factory=dict)

    def station_ids(self, db: Session, spot_id: int) -> list[int]:
        if spot_id not in self.stations:
            self.stations[spot_id] = _stations(db, spot_id)
        return self.stations[spot_id]

    def predictions(
        self, db: Session, spot_id: int, station_id: int, lo: datetime, hi: datetime
    ) -> list[TideRow]:
        held = self.rows.get(station_id)
        if held is None or lo < held[0] or hi > held[1]:
            want = self.windows.get(spot_id)
            start, end = (
                (lo, hi) if want is None else (want[0] - EVENT_SEARCH, want[1] + EVENT_SEARCH)
            )
            if held is not None:
                start, end = min(start, held[0]), max(end, held[1])
            start, end = min(start, lo), max(end, hi)
            held = (start, end, _predictions(db, station_id, start, end))
            self.rows[station_id] = held
        return [r for r in held[2] if lo <= r.valid_time <= hi]


_SHARED_KEY = "tides.stage.shared_reads"


@contextmanager
def shared_tide_reads(
    db: Session, windows: Mapping[int, tuple[datetime, datetime]] | None = None
) -> Iterator[SharedTideReads]:
    """Within the block, :func:`tide_stages_for` on ``db`` reads each spot's stations and
    each station's predictions once (``windows`` as in :class:`SharedTideReads`)."""
    shared = SharedTideReads(windows=dict(windows or {}))
    previous = db.info.get(_SHARED_KEY)
    db.info[_SHARED_KEY] = shared
    try:
        yield shared
    finally:
        if previous is None:
            db.info.pop(_SHARED_KEY, None)
        else:
            db.info[_SHARED_KEY] = previous


def tide_stages_for(
    db: Session, spot_id: int, times: Sequence[datetime]
) -> dict[datetime, TideStage]:
    """The stage at each of ``times`` that a prediction covers; other times are absent."""
    if not times:
        return {}
    lo, hi = min(times) - EVENT_SEARCH, max(times) + EVENT_SEARCH
    shared: SharedTideReads | None = db.info.get(_SHARED_KEY)
    stations = shared.station_ids(db, spot_id) if shared else _stations(db, spot_id)
    for station_id in stations:
        rows = (
            shared.predictions(db, spot_id, station_id, lo, hi)
            if shared
            else _predictions(db, station_id, lo, hi)
        )
        events = [Event(r.valid_time, r.height_m, r.event_type) for r in rows if r.is_event]
        if not events:
            continue
        curve = [(r.valid_time, r.height_m) for r in rows if not r.is_event]
        source = next(r.source_code for r in rows if r.is_event)
        return stages_from(events, curve, times, source)
    return {}
