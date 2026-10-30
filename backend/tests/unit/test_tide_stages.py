"""predict.tide_stages (plain Python) gives exactly what its pandas predecessor gave.

The pandas code below is the 2.1.0 implementation verbatim. The series are SYNTHETIC
(seeded random tide-like curves with gaps, irregular spacing and shuffled order), made up
for this comparison only.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.services.surf_quality.predict import MIN_TIDAL_RANGE_M, demo_tide_stages, tide_stages

pd = pytest.importorskip("pandas")

T0 = datetime(2026, 10, 16, tzinfo=UTC)


def pandas_tide_stages(times: list[datetime], levels: list[float | None]) -> list[float | None]:
    """The pandas implementation tide_stages replaced (algorithm 2.1.0)."""
    if not times or all(v is None for v in levels):
        return [None] * len(times)
    series = pd.Series(levels, index=pd.DatetimeIndex(times), dtype="float64").sort_index()
    window = pd.Timedelta(hours=25)
    lo = series.rolling(window, center=True, min_periods=6).min()
    hi = series.rolling(window, center=True, min_periods=6).max()
    span = hi - lo
    stage = (series - lo) / span
    stage[(span < MIN_TIDAL_RANGE_M) | series.isna()] = float("nan")
    return [
        None if pd.isna(v) else round(float(v), 3) for v in stage.reindex(pd.DatetimeIndex(times))
    ]


def synthetic_series(rng: random.Random) -> tuple[list[datetime], list[float | None]]:
    step_minutes = rng.choice([30, 60, 60, 180, 360])
    n = rng.randint(0, 120)
    amplitude = rng.choice([0.04, 0.06, 0.3, 1.2, 2.5])
    times: list[datetime] = []
    t = T0
    for _ in range(n):
        # Mostly regular steps, sometimes a gap of several steps.
        t += timedelta(minutes=step_minutes * rng.choice([1, 1, 1, 1, 2, 5]))
        times.append(t)
    levels: list[float | None] = []
    for t in times:
        hours = (t - T0).total_seconds() / 3600.0
        value = amplitude * math.sin(2 * math.pi * hours / 12.42) + rng.uniform(-0.02, 0.02)
        levels.append(None if rng.random() < 0.15 else round(value, rng.choice([2, 3, 6])))
    if rng.random() < 0.3:
        pairs = list(zip(times, levels, strict=True))
        rng.shuffle(pairs)
        times = [p[0] for p in pairs]
        levels = [p[1] for p in pairs]
    return times, levels


@pytest.mark.parametrize("seed", range(400))
def test_matches_the_pandas_implementation(seed: int) -> None:
    times, levels = synthetic_series(random.Random(seed))
    assert tide_stages(times, levels) == pandas_tide_stages(times, levels)


def test_window_edges_match_pandas() -> None:
    """Half-hourly levels put neighbours exactly 12.5 h away: the window is closed on the
    right and open on the left, as pandas' centred time window is."""
    times = [T0 + timedelta(minutes=30 * i) for i in range(80)]
    levels: list[float | None] = [round(1.5 * math.sin(i / 7.0), 3) for i in range(80)]
    assert tide_stages(times, levels) == pandas_tide_stages(times, levels)
    # The right edge counts: a low exactly 12.5 h after a time sets its stage.
    edge = [0.5] * 26
    edge[25] = -1.0
    out = tide_stages(times[:26], edge)
    assert out == pandas_tide_stages(times[:26], edge)
    assert out[0] == 1.0  # (0.5 - (-1.0)) / 1.5


def test_degenerate_inputs() -> None:
    assert tide_stages([], []) == []
    times = [T0 + timedelta(hours=i) for i in range(5)]
    assert tide_stages(times, [None] * 5) == [None] * 5
    # Fewer than six known levels in every window.
    assert tide_stages(times, [0.0, 1.0, 0.0, 1.0, 0.0]) == [None] * 5
    # A NaN counts as missing, as in pandas.
    times = [T0 + timedelta(hours=i) for i in range(12)]
    levels: list[float | None] = [float("nan")] + [math.sin(i) for i in range(1, 12)]
    assert tide_stages(times, levels) == pandas_tide_stages(times, levels)
    assert tide_stages(times, levels)[0] is None


def test_demo_tide_stages_unchanged_shape() -> None:
    times = [T0 + timedelta(hours=i) for i in range(48)]
    levels: list[float | None] = [round(math.sin(2 * math.pi * i / 12.42), 3) for i in range(48)]
    stages = demo_tide_stages(times, levels)
    expected = pandas_tide_stages(times, levels)
    assert {t: s.stage for t, s in stages.items()} == {
        t: v for t, v in zip(times, expected, strict=True) if v is not None
    }
    assert all(s.source == "demo" for s in stages.values())
