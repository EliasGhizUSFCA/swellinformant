"""Swell direction and period scored over every wave partition (algorithm 2.1.0).

Spot parameters come from the committed spots.json; every sea state is SYNTHETIC, except the
Sunset Beach case, whose partitions are the values of the real blend run of 2026-10-11 (hour
2026-10-12 00Z, audit finding SW-3) typed in by hand.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.geo import window_distance
from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import (
    evaluate,
    score_consistency,
    score_direction,
    score_period,
)

SPOTS: dict[str, dict[str, Any]] = {
    s["slug"]: s for s in json.loads((DEFAULT_DATA_DIR / "spots.json").read_text())
}
FIELDS = (
    "latitude",
    "longitude",
    "swell_window_min",
    "swell_window_max",
    "optimal_swell_direction_min",
    "optimal_swell_direction_max",
    "offshore_wind_direction",
    "min_swell_period_s",
    "ideal_swell_period_s",
    "wave_height_min_ft",
    "wave_height_max_ft",
    "height_factor",
)


def spot(slug: str) -> SpotParams:
    return SpotParams(slug=slug, **{f: float(SPOTS[slug][f]) for f in FIELDS})  # type: ignore[arg-type]


def v200_direction(direction: float, s: SpotParams) -> float:
    """Algorithm 2.0.0's direction fit, for the regression checks inside the window."""
    if window_distance(direction, s.swell_window_min, s.swell_window_max) > 0:
        return 0.0
    d_opt = window_distance(direction, s.optimal_swell_direction_min, s.optimal_swell_direction_max)
    return max(0.0, min(1.0, 1.0 - d_opt / 45.0))


SUNSET = SeaState(
    primary_swell_height_m=0.60,
    primary_swell_period_s=12.5,
    primary_swell_direction_deg=82.0,
    tertiary_swell_height_m=0.44,
    tertiary_swell_period_s=14.6,
    tertiary_swell_direction_deg=310.0,
    wind_speed_kmh=12.0,
    wind_direction_deg=135.0,
)


def test_sunset_beach_is_scored_by_the_swell_that_makes_the_surf() -> None:
    s = spot("sunset-beach")
    r = evaluate(SUNSET, s)
    # 2.0.0 scored the blocked primary swell from 82 deg: direction 0.0.
    assert v200_direction(82.0, s) == 0.0
    assert r.components["swell_direction"] >= 0.8
    parts = {p["kind"]: p for p in r.stored_components()["direction_partitions"]}
    assert set(parts) == {"primary_swell", "tertiary_swell"}
    assert parts["tertiary_swell"]["weight"] > 0.9
    assert sum(p["weight"] for p in parts.values()) == pytest.approx(1.0, abs=0.002)
    # The explanation names the partition that drives the surf.
    assert r.explanation.startswith("0.4 m @ 15 s NW tertiary swell, well aligned")


@pytest.mark.parametrize("slug", ["jeffreys-bay", "pipeline", "ocean-beach-sf", "mundaka"])
def test_a_single_partition_gives_the_v200_direction_and_period(slug: str) -> None:
    s = spot(slug)
    lo, hi = s.swell_window_min, s.swell_window_max
    width = (hi - lo) % 360
    for i in range(0, 21):
        direction = (lo + width * i / 20) % 360
        for period in (8.0, 11.0, 13.5, 17.0):
            sea = SeaState(
                primary_swell_height_m=1.8,
                primary_swell_period_s=period,
                primary_swell_direction_deg=direction,
            )
            r = evaluate(sea, s)
            if r.breaking.mid_ft <= 0:
                continue
            assert r.components["swell_direction"] == round(v200_direction(direction, s), 3)
            assert r.components["swell_period"] == round(score_period(period, s) or 0.0, 3)
            assert r.components["consistency"] == 1.0


def test_bulk_only_sea_state_is_unchanged() -> None:
    s = spot("jeffreys-bay")
    sea = SeaState(sig_wave_height_m=2.0, wave_period_s=13.0, wave_direction_deg=215.0)
    r = evaluate(sea, s)
    assert r.components["swell_direction"] == round(v200_direction(215.0, s), 3)
    assert r.components["swell_period"] == round(score_period(13.0, s) or 0.0, 3)
    assert "consistency" not in r.components  # no partition information, as in 2.0.0


def test_a_tiny_blocked_partition_barely_changes_the_direction_fit() -> None:
    s = spot("jeffreys-bay")
    base = SeaState(
        primary_swell_height_m=2.0, primary_swell_period_s=15.0, primary_swell_direction_deg=215.0
    )
    with_blocked = SeaState(
        primary_swell_height_m=2.0,
        primary_swell_period_s=15.0,
        primary_swell_direction_deg=215.0,
        secondary_swell_height_m=0.3,
        secondary_swell_period_s=9.0,
        secondary_swell_direction_deg=60.0,  # far outside the window
    )
    a = evaluate(base, s).components["swell_direction"]
    b = evaluate(with_blocked, s).components["swell_direction"]
    assert abs(a - b) < 0.02


def test_direction_and_period_follow_the_breaking_energy_flux() -> None:
    """Two swells: the weights are Hb_i^2.5 / sum(Hb_j^2.5) of their breaking heights."""
    s = spot("jeffreys-bay")
    sea = SeaState(
        primary_swell_height_m=2.0,
        primary_swell_period_s=10.0,
        primary_swell_direction_deg=245.0,  # inside the window, 15 deg off the optimum
        secondary_swell_height_m=1.5,
        secondary_swell_period_s=16.0,
        secondary_swell_direction_deg=215.0,  # optimal
    )
    r = evaluate(sea, s)
    flux = {p.name: p.breaking_m**2.5 for p in r.breaking.flux}
    w2 = flux["secondary_swell"] / sum(flux.values())
    d1, d2 = score_direction(245.0, s), score_direction(215.0, s)
    p1, p2 = score_period(10.0, s), score_period(16.0, s)
    assert d1 is not None and d2 is not None and p1 is not None and p2 is not None
    assert r.components["swell_direction"] == pytest.approx((1 - w2) * d1 + w2 * d2, abs=1e-3)
    assert r.components["swell_period"] == pytest.approx((1 - w2) * p1 + w2 * p2, abs=1e-3)


def test_consistency_counts_the_tertiary_swell_and_ignores_wind_sea_leaving_the_break() -> None:
    s = spot("jeffreys-bay")  # offshore from 290 deg
    swells = {
        "primary_swell_height_m": 1.2,
        "primary_swell_period_s": 14.0,
        "primary_swell_direction_deg": 215.0,
        "tertiary_swell_height_m": 1.0,
        "tertiary_swell_period_s": 13.0,
        "tertiary_swell_direction_deg": 225.0,
    }
    onshore_sea = SeaState(
        **swells, wind_wave_height_m=1.0, wind_wave_period_s=5.0, wind_wave_direction_deg=215.0
    )
    offshore_sea = SeaState(
        **swells, wind_wave_height_m=1.0, wind_wave_period_s=5.0, wind_wave_direction_deg=290.0
    )
    on = evaluate(onshore_sea, s)
    off = evaluate(offshore_sea, s)
    # Wind sea travelling toward the break costs consistency; wind sea from the offshore
    # direction, which travels out to sea, hardly reaches the break (only the edge of its
    # 30 deg spread does) and costs almost nothing. 2.0.0 gave both 1.2^2 / (1.2^2 + 1^2).
    assert on.components["consistency"] < 0.9
    assert off.components["consistency"] > 0.95
    assert on.components["consistency"] < off.components["consistency"]
    # Without a breaking breakdown the offshore energy share now includes the tertiary swell.
    no_flux = score_consistency(onshore_sea)
    assert no_flux == pytest.approx((1.2**2 + 1.0**2) / (1.2**2 + 1.0**2 + 1.0**2))
