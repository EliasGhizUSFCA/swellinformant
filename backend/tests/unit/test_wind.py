"""Offshore wind physics and the wind fit (surf_quality.wind, algorithm 2.1.0).

The spot directions come from the committed data/surf_spots/spots.json; every wind value is
SYNTHETIC test input.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality import wind
from app.services.surf_quality.models import SpotParams
from app.services.surf_quality.wind import (
    ShelterSector,
    WindProfile,
    q_wind,
    wind_components,
    wind_favourability,
    wind_fit,
    wind_relation,
    wind_strength,
)

SPOTS: dict[str, dict[str, Any]] = {
    s["slug"]: s for s in json.loads((DEFAULT_DATA_DIR / "spots.json").read_text())
}


def params(slug: str, **kw: Any) -> SpotParams:
    s = SPOTS[slug]
    base = SpotParams(
        slug=slug,
        latitude=s["latitude"],
        longitude=s["longitude"],
        swell_window_min=s["swell_window_min"],
        swell_window_max=s["swell_window_max"],
        optimal_swell_direction_min=s["optimal_swell_direction_min"],
        optimal_swell_direction_max=s["optimal_swell_direction_max"],
        offshore_wind_direction=s["offshore_wind_direction"],
        min_swell_period_s=s["min_swell_period_s"],
        ideal_swell_period_s=s["ideal_swell_period_s"],
        wave_height_min_ft=s["wave_height_min_ft"],
        wave_height_max_ft=s["wave_height_max_ft"],
    )
    return replace(base, **kw)


# A west-facing beach: offshore wind blows FROM the east.
WEST_FACING = replace(params("ocean-beach-sf"), offshore_wind_direction=90.0)


def v_dot_n(speed: float, from_deg: float, offshore_deg: float) -> float:
    """V . n from first principles: u = -V sin t, v = -V cos t; n at bearing offshore + 180."""
    t = math.radians(from_deg)
    u, v = -speed * math.sin(t), -speed * math.cos(t)
    phi = math.radians(offshore_deg + 180.0)
    return u * math.sin(phi) + v * math.cos(phi)


class TestSignConvention:
    def test_west_facing_beach_table(self) -> None:
        """Spec 9: on a west-facing beach E is offshore, W onshore, N and S cross-shore."""
        assert wind_components(20, 90, 90).v_offshore_kmh == pytest.approx(20, abs=1e-9)
        assert wind_components(20, 270, 90).v_offshore_kmh == pytest.approx(-20, abs=1e-9)
        for cross in (0.0, 180.0, 360.0):
            c = wind_components(20, cross, 90)
            assert abs(c.v_offshore_kmh) < 1e-9
            assert c.v_cross_kmh == pytest.approx(20, abs=1e-9)
        assert wind_relation(20, 90, 90) == "offshore"
        assert wind_relation(20, 270, 90) == "onshore"
        assert wind_relation(20, 0, 90) == wind_relation(20, 180, 90) == "cross-shore"

    def test_diagonal_signs(self) -> None:
        # NE and SE have an offshore component, SW and NW an onshore one.
        for d in (45, 135):
            assert wind_components(10, d, 90).v_offshore_kmh == pytest.approx(10 / math.sqrt(2))
        for d in (225, 315):
            assert wind_components(10, d, 90).v_offshore_kmh == pytest.approx(-10 / math.sqrt(2))

    @pytest.mark.parametrize("from_deg", [d * 5.0 for d in range(72)])
    @pytest.mark.parametrize("offshore", [0.0, 90.0, 147.5, 290.0])
    def test_components_match_explicit_vectors(self, from_deg: float, offshore: float) -> None:
        c = wind_components(17.0, from_deg, offshore)
        assert c.v_offshore_kmh == pytest.approx(v_dot_n(17.0, from_deg, offshore), abs=1e-9)
        assert math.hypot(c.v_offshore_kmh, c.v_cross_kmh) == pytest.approx(17.0, abs=1e-9)
        # The relation follows cos(psi) = V . n / V.
        psi = math.degrees(math.acos(max(-1.0, min(1.0, c.v_offshore_kmh / 17.0))))
        assert wind.offshore_angle_deg(17.0, from_deg, offshore) == pytest.approx(psi, abs=1e-6)

    @pytest.mark.parametrize(
        ("slug", "offshore"), [("ocean-beach-sf", 90), ("la-graviere-hossegor", 95)]
    )
    def test_spec_table_at_west_facing_spots(self, slug: str, offshore: float) -> None:
        spot = params(slug)
        assert spot.offshore_wind_direction == offshore
        assert wind_relation(15, 90, offshore) == "offshore"
        assert wind_relation(15, 270, offshore) == "onshore"
        assert wind_relation(15, 0, offshore) == "cross-shore"
        assert wind_relation(15, 180, offshore) == "cross-shore"

    @pytest.mark.parametrize("slug", sorted(SPOTS))
    def test_every_spot_is_offshore_at_its_offshore_direction(self, slug: str) -> None:
        off = float(SPOTS[slug]["offshore_wind_direction"])
        assert wind_components(25, off, off).v_offshore_kmh == pytest.approx(25, abs=1e-9)
        assert wind_components(25, (off + 180) % 360, off).v_offshore_kmh == pytest.approx(
            -25, abs=1e-9
        )
        assert wind_relation(25, off, off) == "offshore"
        assert wind_relation(25, (off + 180) % 360, off) == "onshore"


class TestSpotOrientation:
    """Researched offshore directions, not "east is offshore" and not the swell normal."""

    def test_chicama_offshore_is_not_beta_plus_180(self) -> None:
        spot = params("chicama")
        assert spot.offshore_wind_direction == 150
        beta = spot.physical_profile().shore_normal_deg
        assert (beta + 180) % 360 == pytest.approx(30)
        assert wind_relation(15, 150, 150) == "offshore"
        assert wind_relation(15, 30, 150) == "cross-onshore"
        assert wind_components(15, 30, 150).v_offshore_kmh < 0

    @pytest.mark.parametrize(
        ("slug", "from_deg", "relation"),
        [
            ("jeffreys-bay", 290, "offshore"),
            ("jeffreys-bay", 35, "cross-shore"),
            ("snapper-rocks-superbank", 200, "offshore"),
            ("snapper-rocks-superbank", 135, "cross-offshore"),
            ("kirra", 225, "offshore"),
            ("kirra", 90, "cross-onshore"),  # east is not offshore at Kirra
            ("uluwatu", 135, "offshore"),
            ("uluwatu", 300, "onshore"),
            ("teahupoo", 30, "offshore"),
            ("teahupoo", 200, "onshore"),
        ],
    )
    def test_named_cases(self, slug: str, from_deg: float, relation: str) -> None:
        assert wind_relation(15, from_deg, params(slug).offshore_wind_direction) == relation

    def test_kirra_east_wind_scores_below_its_offshore(self) -> None:
        kirra = params("kirra")
        assert q_wind(20, 90, kirra) < 0.5 < q_wind(20, 225, kirra)


SPEEDS = [x * 0.5 for x in range(0, 161)]  # 0..80 km/h


class TestSpeedRegimes:
    def test_calm_and_light_offshore(self) -> None:
        assert q_wind(0, None, WEST_FACING) == 1.0
        assert q_wind(0, 270, WEST_FACING) == 1.0
        assert q_wind(10, 90, WEST_FACING) >= 0.95
        assert q_wind(20, 90, WEST_FACING) >= 0.95

    def test_strong_offshore_is_not_maximal(self) -> None:
        assert q_wind(35, 90, WEST_FACING) < q_wind(15, 90, WEST_FACING)
        assert q_wind(50, 90, WEST_FACING) <= 0.5
        # No floor: stronger is always worse.
        assert q_wind(100, 90, WEST_FACING) < q_wind(60, 90, WEST_FACING)
        assert q_wind(150, 90, WEST_FACING) < q_wind(100, 90, WEST_FACING)

    @pytest.mark.parametrize("from_deg", [d * 10.0 for d in range(36)])
    def test_non_increasing_in_speed(self, from_deg: float) -> None:
        qs = [q_wind(v, from_deg, WEST_FACING) for v in SPEEDS]
        assert all(b <= a + 1e-12 for a, b in zip(qs, qs[1:], strict=False))

    @pytest.mark.parametrize("speed", [15.0, 30.0])
    def test_ordering_by_direction(self, speed: float) -> None:
        # psi = 180 onshore, 120 cross-onshore, 90 cross, 60 cross-offshore, 0 offshore.
        q = {psi: q_wind(speed, (90 + psi) % 360, WEST_FACING) for psi in (180, 120, 90, 60, 0)}
        assert q[180] < q[120] < q[90] < q[60] <= q[0]

    @pytest.mark.parametrize("slug", ["ocean-beach-sf", "kirra", "uluwatu"])
    def test_continuous_in_speed_and_direction(self, slug: str) -> None:
        """No jumps: at most 0.02 per 1 deg. Per 0.5 km/h the bound is the steepest slope of
        the chop curve, max d/dV 1/(1 + (V/v_half)^p) = x^(p-1) (p+1)^2 / (4 p v_half) with
        x^p = (p-1)/(p+1): 0.067 per km/h for p = 3 and v_half = 12.5 km/h, so 0.034 per
        0.5 km/h onshore (2.0.0 also changed by up to 0.033 per 0.5 km/h). The audit's 0.02
        per 0.5 km/h would need v_half >= 21 km/h, against the agreed 12.5 km/h default."""
        spot = params(slug)
        p, v_half = wind.DEFAULT_PARAMS.p, wind.DEFAULT_PARAMS.v_half_kmh
        x = ((p - 1) / (p + 1)) ** (1 / p)
        max_slope = x ** (p - 1) * (p + 1) ** 2 / (4 * p * v_half)
        assert max_slope == pytest.approx(0.0672, abs=1e-4)
        for d in range(0, 360, 5):
            qs = [q_wind(v, d, spot) for v in SPEEDS]
            assert max(abs(b - a) for a, b in zip(qs, qs[1:], strict=False)) <= 0.5 * max_slope
        for v in (5.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0, 60.0, 80.0):
            qs = [q_wind(v, float(d), spot) for d in range(0, 361)]
            assert max(abs(b - a) for a, b in zip(qs, qs[1:], strict=False)) <= 0.02

    def test_unknown_direction_scores_as_cross_shore(self) -> None:
        fit = wind_fit(20, None, WEST_FACING)
        assert fit.q == pytest.approx(q_wind(20, 0, WEST_FACING))
        assert fit.v_offshore_kmh is None and fit.v_cross_kmh is None

    def test_gust_only_lowers_the_fit(self) -> None:
        base = q_wind(20, 0, WEST_FACING)
        assert q_wind(20, 0, WEST_FACING, gust_kmh=None) == base
        assert q_wind(20, 0, WEST_FACING, gust_kmh=18.0) == base  # gust below the mean
        gusts = [q_wind(20, 0, WEST_FACING, gust_kmh=g) for g in (20, 25, 30, 40, 60)]
        assert gusts[0] == pytest.approx(base)
        assert all(b < a for a, b in zip(gusts, gusts[1:], strict=False))
        # The reported components stay those of the mean wind.
        gusty = wind_fit(20, 45, WEST_FACING, gust_kmh=60)
        assert gusty.gust_used is True
        assert gusty.v_offshore_kmh == pytest.approx(v_dot_n(20, 45, 90))
        assert math.hypot(gusty.v_offshore_kmh, gusty.v_cross_kmh or 0.0) == pytest.approx(20)


class TestLabels:
    def test_beaufort_strength_boundaries(self) -> None:
        assert wind_strength(0) == wind_strength(5.0) == "glassy"
        assert wind_strength(5.01) == wind_strength(11.99) == "light"
        assert wind_strength(12.0) == wind_strength(28.99) == "moderate"
        assert wind_strength(29.0) == wind_strength(120) == "strong"

    def test_favourability_boundaries(self) -> None:
        assert wind_favourability(1.0) == wind_favourability(0.75) == "favourable"
        assert wind_favourability(0.7499) == wind_favourability(0.4) == "workable"
        assert wind_favourability(0.3999) == wind_favourability(0.0) == "unfavourable"

    def test_light_onshore_breeze_is_light_and_favourable(self) -> None:
        """The Ocean Beach 2026-10-13 09Z case: 5.8 km/h onshore next to an Exceptional score."""
        ob = params("ocean-beach-sf")
        fit = wind_fit(5.8, 271.0, ob)
        assert wind_relation(5.8, 271.0, ob.offshore_wind_direction) == "onshore"
        assert fit.strength == "light"
        assert fit.favourability == "favourable"

    def test_glassy_and_unknown(self) -> None:
        assert wind_relation(0.0, None, 90) == "glassy"  # calm: no direction
        assert wind_relation(5.0, 270, 90) == "glassy"
        assert wind_relation(5.1, 270, 90) == "onshore"
        assert wind_relation(None, 90, 90) == "unknown"
        assert wind_relation(12.0, None, 90) == "unknown"

    def test_sectors_are_symmetric_about_cross_shore(self) -> None:
        names = ["offshore", "cross-offshore", "cross-shore", "cross-onshore", "onshore"]
        for psi in [x * 0.5 for x in range(0, 361)]:
            rel = wind_relation(20, (90 + psi) % 360, 90)
            mirror = wind_relation(20, (90 + 180 - psi) % 360, 90)
            if psi not in (45.0, 75.0, 105.0, 135.0):  # the inclusive bounds themselves
                assert names.index(rel) == len(names) - 1 - names.index(mirror), psi
        # Winds just onshore of perpendicular are cross-shore only up to 105 deg.
        assert wind_relation(20, 90 + 104, 90) == "cross-shore"
        assert wind_relation(20, 90 + 106, 90) == "cross-onshore"
        assert wind_relation(20, 90 + 76, 90) == "cross-shore"
        assert wind_relation(20, 90 + 74, 90) == "cross-offshore"


class TestSpotWindProfile:
    def test_shelter_halves_the_wind_from_its_sector(self) -> None:
        """A 0.5 shelter factor doubles the N cross-shore speed needed for the same Q."""
        sheltered = replace(
            WEST_FACING,
            wind_profile=WindProfile(
                shelter=(ShelterSector(337.5, 22.5, 0.5, ("synthetic-source",)),),
                data_quality="research",
            ),
        )
        for v in (10.0, 15.0, 20.0, 30.0):
            assert q_wind(2 * v, 0.0, sheltered) == pytest.approx(q_wind(v, 0.0, WEST_FACING))
        # The opposite cross-shore wind (S) is not sheltered.
        assert q_wind(20, 180, sheltered) == pytest.approx(q_wind(20, 180, WEST_FACING))
        fit = wind_fit(20, 0.0, sheltered)
        assert fit.shelter_factor == pytest.approx(0.5)
        assert fit.params["shelter_factor"]["sources"] == ["synthetic-source"]
        # Shelter changes the fit, not the reported components of the forecast wind.
        assert fit.q == pytest.approx(q_wind(10, 0.0, WEST_FACING))
        assert fit.v_offshore_kmh == pytest.approx(0.0, abs=1e-9)
        assert fit.v_cross_kmh == pytest.approx(20.0)
        nw = wind_fit(20, 315.0, sheltered)  # 22.5 deg outside the sector: a little shelter
        assert 0.0 < nw.shelter_factor < 0.01
        assert nw.v_offshore_kmh == pytest.approx(v_dot_n(20, 315.0, 90.0))
        # Shelter fades out outside the sector over about 15 deg: continuous in direction
        # (no step at the sector edge; at most 0.006 per 0.1 deg).
        qs = [q_wind(30, d / 10, sheltered) for d in range(0, 3601)]
        assert max(abs(b - a) for a, b in zip(qs, qs[1:], strict=False)) <= 0.006

    def test_no_profile_equals_the_default(self) -> None:
        empty = replace(WEST_FACING, wind_profile=WindProfile())
        for d in (0, 90, 200, 270):
            assert q_wind(25, d, empty) == q_wind(25, d, WEST_FACING)

    def test_researched_tolerance_replaces_the_default(self) -> None:
        tolerant = replace(
            WEST_FACING,
            wind_profile=WindProfile(
                offshore_tolerance_kmh=39.0, sources=("synthetic-source",), data_quality="research"
            ),
        )
        assert q_wind(38, 90, tolerant) == pytest.approx(1.0)
        assert q_wind(38, 90, WEST_FACING) < 0.8
        tol = wind_fit(38, 90, tolerant).params["offshore_tolerance_kmh"]
        assert tol == {"value": 39.0, "provenance": "research", "sources": ["synthetic-source"]}
        # A number taken from the Beaufort scale for a source's word ("strong offshore") is
        # labelled beaufort, not research; a heuristic profile's number is heuristic.
        worded = replace(
            tolerant,
            wind_profile=replace(tolerant.wind_profile, tolerance_provenance="beaufort"),
        )
        tol = wind_fit(38, 90, worded).params["offshore_tolerance_kmh"]
        assert tol == {"value": 39.0, "provenance": "beaufort", "sources": ["synthetic-source"]}
        owner = replace(
            WEST_FACING, wind_profile=WindProfile(offshore_tolerance_kmh=33.0)
        )  # data_quality "heuristic default"
        assert wind_fit(38, 90, owner).params["offshore_tolerance_kmh"]["provenance"] == (
            "heuristic"
        )

    def test_extra_offshore_direction_takes_the_best_fit(self) -> None:
        two_faces = replace(
            WEST_FACING,
            wind_profile=WindProfile(
                extra_offshore_deg=(180.0,), sources=("s",), data_quality="research"
            ),
        )
        assert q_wind(20, 180, two_faces) == pytest.approx(1.0)
        assert wind_fit(20, 180, two_faces).params["offshore_wind_direction"]["value"] == 180.0
        # The relation keeps the profile's main offshore direction.
        assert wind_relation(20, 180, two_faces.offshore_wind_direction) == "cross-shore"

    def test_detail_labels_every_parameter(self) -> None:
        fit = wind_fit(20, 0.0, WEST_FACING)
        d = fit.as_dict()
        assert set(d) == {
            "q",
            "v_offshore_kmh",
            "v_cross_kmh",
            "strength",
            "favourability",
            "gust_used",
            "params",
        }
        assert d["gust_used"] is False
        assert {p["provenance"] for p in d["params"].values()} <= {
            "research",
            "beaufort",
            "heuristic",
        }
        for name, value, provenance in wind.documented_defaults():
            assert provenance == "heuristic"
            if name != "gust_weight":
                assert d["params"][name]["value"] == value
        assert d["params"]["glassy_max_kmh"] == {"value": 5.0, "provenance": "beaufort"}
        assert d["params"]["offshore_wind_direction"] == {"value": 90.0, "provenance": "research"}
