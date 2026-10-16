"""Harmonic tide synthesis from the committed EOT20 constants, checked against real NOAA
CO-OPS predictions (tests/fixtures/coops/h72_*.json: 72 hourly values from 2026-10-14
00:00 UTC, datum MSL).

Measured on 2026-10-15 with data/tides/constants_eot20.json (after removing the mean
offset, which is the difference between CO-OPS's MSL and the model's mean sea level):

    spot        station   r        RMS
    mavericks   9414131   0.9953   4.6 cm
    rincon      9411270   0.9955   4.0 cm
    jaws-peahi  1615680   0.9953   2.0 cm

The tolerances below (r >= 0.99, RMS <= 6 cm) are set from that measurement with a margin;
docs/tides.md has the 14-day validation at every US station.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.services.tides import harmonic_model as hm

REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coops"
CONSTANTS = REPO / "data" / "tides" / "constants_eot20.json"


def _coops(name: str) -> tuple[list[datetime], np.ndarray]:
    rows = json.loads((FIXTURES / name).read_text())["predictions"]
    times = [datetime.strptime(r["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC) for r in rows]
    return times, np.array([float(r["v"]) for r in rows])


@pytest.mark.parametrize(
    ("slug", "station", "max_rms_m"),
    [("mavericks", "9414131", 0.06), ("rincon", "9411270", 0.06), ("jaws-peahi", "1615680", 0.04)],
)
def test_model_tide_matches_coops_predictions(slug: str, station: str, max_rms_m: float) -> None:
    times, coops = _coops(f"h72_{station}.json")
    assert len(times) == 72 and times[0] == datetime(2026, 10, 14, tzinfo=UTC)
    model = hm.predict(hm.load_constants(CONSTANTS)[slug], times)
    offset = float(np.mean(coops) - np.mean(model))
    rms = float(np.sqrt(np.mean((model + offset - coops) ** 2)))
    r = float(np.corrcoef(model, coops)[0, 1])
    assert r >= 0.99, r
    assert rms <= max_rms_m, rms
    # The model's mean sea level and CO-OPS's MSL agree within a few centimetres here.
    assert abs(offset) < 0.05
    # The range is right too (no gain error hidden by the correlation).
    assert np.ptp(model) == pytest.approx(np.ptp(coops), rel=0.06)


def test_no_network_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the harmonic model must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    hm._load_cached.cache_clear()
    constants = hm.load_constants(CONSTANTS)
    start = datetime(2026, 10, 14, tzinfo=UTC)
    times = hm.regular_times(start, start + timedelta(days=2), timedelta(minutes=30))
    heights = hm.predict(constants["nazare"], times)
    assert len(heights) == 97 and np.all(np.isfinite(heights))
    events = hm.predict_events(constants["nazare"], start, start + timedelta(days=2))
    assert 6 <= len(events) <= 9  # semidiurnal: about 4 high/low waters per day


def test_constants_file_is_complete_and_attributed() -> None:
    meta, constants = hm.load_model(CONSTANTS)
    assert meta["source"] == "eot20" and meta["datum"] == "model_msl"
    assert meta["licence"] == "CC BY 4.0"
    assert meta["attribution"] == "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE"
    assert len(constants) == 50 and not meta["unavailable"]
    assert CONSTANTS.stat().st_size < 100_000
    for slug, c in constants.items():
        assert set(c.names) == set(hm.CONSTITUENTS), slug
        assert c.distance_km < 15, slug  # the nearest valid 1/8 degree ocean cell


def test_events_alternate_and_lie_on_the_curve() -> None:
    constants = hm.load_constants(CONSTANTS)["mavericks"]
    start = datetime(2026, 10, 14, tzinfo=UTC)
    events = hm.predict_events(constants, start, start + timedelta(days=7))
    kinds = [k for _, _, k in events]
    assert all(a != b for a, b in zip(kinds, kinds[1:], strict=False))
    times = [t for t, _, _ in events]
    curve = hm.predict(constants, times)
    assert np.max(np.abs(curve - np.array([h for _, h, _ in events]))) < 0.002
    # A high water is higher than the curve a few minutes either side, a low lower.
    for t, h, kind in events:
        around = hm.predict(constants, [t - timedelta(minutes=10), t + timedelta(minutes=10)])
        assert (around <= h + 1e-3).all() if kind == "high" else (around >= h - 1e-3).all()


def test_mean_tide_over_a_month_is_near_zero() -> None:
    constants = hm.load_constants(CONSTANTS)["mundaka"]
    start = datetime(2026, 10, 1, tzinfo=UTC)
    times = hm.regular_times(start, start + timedelta(days=29.5), timedelta(minutes=30))
    heights = hm.predict(constants, times)
    # Short-period constituents average out; SA/SSA keep the seasonal level (< 10 cm).
    assert abs(float(np.mean(heights))) < 0.1
    assert 3.0 < float(np.ptp(heights)) < 5.5  # Bay of Biscay spring range about 4 m


def test_nodal_corrections_match_the_textbook_series() -> None:
    """f and u against the usual series in the node longitude N (Pugh, "Tides, Surges and
    Mean Sea-Level", 1987, chapter 4): an independent check of the Schureman formulas,
    including the sign of u, which the 72-hour comparison above is too short to pin."""
    n_deg = np.array([0.0, 60.0, 90.0, 180.0, 270.0, 300.0])
    n = np.radians(n_deg)
    f, u = hm.nodal_corrections(n_deg)
    series_f = {
        "m2": 1.0004 - 0.0373 * np.cos(n) + 0.0002 * np.cos(2 * n),
        "k1": 1.006 + 0.115 * np.cos(n) - 0.009 * np.cos(2 * n),
        "o1": 1.009 + 0.187 * np.cos(n) - 0.015 * np.cos(2 * n),
        "k2": 1.024 + 0.286 * np.cos(n) + 0.008 * np.cos(2 * n),
    }
    series_u = {
        "m2": -2.14 * np.sin(n),
        "k1": -8.86 * np.sin(n) + 0.68 * np.sin(2 * n),
        "o1": 10.80 * np.sin(n) - 1.34 * np.sin(2 * n),
        "k2": -17.74 * np.sin(n) + 0.68 * np.sin(2 * n),
    }
    # The series are truncated: measured differences are at most 0.0018 (f) and 0.19 deg (u).
    for group, expected in series_f.items():
        assert np.max(np.abs(f[group] - expected)) < 0.003, group
    for group, expected in series_u.items():
        assert np.max(np.abs(u[group] - expected)) < 0.25, group
    assert np.all(f["none"] == 1.0) and np.all(u["none"] == 0.0)


def test_naive_times_are_refused() -> None:
    constants = hm.load_constants(CONSTANTS)["mavericks"]
    with pytest.raises(ValueError, match="timezone-aware"):
        hm.predict(constants, [datetime(2026, 10, 14)])
