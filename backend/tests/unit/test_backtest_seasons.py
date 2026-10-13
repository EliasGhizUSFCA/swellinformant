"""Half-year labels used by the seasonal backtest breakdown (SYNTHETIC times)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.verification.backtest import WINTER_MONTHS_NORTH, season_labels


def test_winter_is_october_to_march_in_the_north_and_flipped_in_the_south() -> None:
    times = pd.Series(pd.date_range("2025-01-15", periods=12, freq="MS", tz="UTC"))
    months = times.dt.month.to_numpy()
    north = season_labels(times, np.full(12, 21.7))
    south = season_labels(times, np.full(12, -33.9))
    for month, n, s in zip(months, north, south, strict=True):
        expected = "winter" if month in WINTER_MONTHS_NORTH else "summer"
        assert n == expected
        assert s == ("summer" if expected == "winter" else "winter")


def test_equator_counts_as_north() -> None:
    times = pd.Series(pd.to_datetime(["2025-12-01", "2025-06-01"], utc=True))
    assert list(season_labels(times, np.array([0.0, 0.0]))) == ["winter", "summer"]
