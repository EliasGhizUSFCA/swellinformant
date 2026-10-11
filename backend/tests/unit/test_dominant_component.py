"""The event headline uses the wave component that drives the surf, not "primary swell"."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.swell_detection.service import dominant_component


def _pred(partitions: list[dict[str, Any]] | None) -> Any:
    return SimpleNamespace(
        components={"breaking": {"partitions": partitions}} if partitions else {}
    )


FC: Any = SimpleNamespace(sig_wave_height_m=2.03, wave_period_s=8.6, wave_direction_deg=120.0)


def test_wind_sea_dominates_a_small_primary_swell() -> None:
    # Live case (Burleigh Heads, 2026-10-20 00Z): 0.16 m swell under a 2.0 m wind sea.
    parts = [
        {
            "name": "primary_swell",
            "height_m": 0.155,
            "period_s": 11.06,
            "direction_deg": 150,
            "breaking_m": 0.21,
        },
        {
            "name": "wind_sea",
            "height_m": 2.018,
            "period_s": 8.4,
            "direction_deg": 115,
            "breaking_m": 1.62,
        },
    ]
    assert dominant_component(_pred(parts), FC) == (2.018, 8.4, 115)


def test_long_period_swell_wins_over_a_taller_short_wind_sea_when_it_breaks_bigger() -> None:
    parts = [
        {
            "name": "primary_swell",
            "height_m": 1.2,
            "period_s": 16.0,
            "direction_deg": 200,
            "breaking_m": 1.9,
        },
        {
            "name": "wind_sea",
            "height_m": 1.4,
            "period_s": 6.0,
            "direction_deg": 300,
            "breaking_m": 0.0,
        },  # blocked by the swell window
    ]
    assert dominant_component(_pred(parts), FC) == (1.2, 16.0, 200)


def test_falls_back_to_bulk_sea_state_without_a_breakdown() -> None:
    assert dominant_component(_pred(None), FC) == (2.03, 8.6, 120.0)
