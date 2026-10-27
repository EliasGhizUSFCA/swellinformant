"""Spec 18-20 behaviour of the 2.2.0 quality model: opposing coastlines, offshore and onshore
wind, period and size that are not "more is better", tide handling, missing research, the
per-hour favourable / unfavourable conditions and the separation of research and forecast
confidence.

The spots are the seeded ones (data/surf_spots/spots.json) or SYNTHETIC mirror-image
beaches defined here; every sea state is SYNTHETIC (made up for these tests).

Direction conventions checked here and used everywhere: a direction is the nautical bearing
the wind or swell comes FROM; its vector (where the air goes) is u = -V sin(theta) east,
v = -V cos(theta) north; offshore = from the land to the sea, i.e. from the spot's
``offshore_wind_direction``. Map and chart arrows point where the wind or waves GO, the FROM
bearing + 180 (frontend/lib/directions.ts ``arrowRotationDeg``, tested in
frontend/tests/directions.test.ts), which is the bearing of this vector.
"""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.geo import angular_diff, wind_from_uv
from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality import forecast_confidence as fc
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.outputs import hour_outputs, offshore_class
from app.services.surf_quality.reasons import MAX_REASONS, condition_reasons
from app.services.surf_quality.scoring import (
    NO_TIDE_PREFERENCE,
    TIDE_DISPUTED,
    TIDE_NOT_RESEARCHED,
    TIDE_RESEARCH_LOW,
    evaluate,
)
from app.services.surf_quality.wind import (
    FAVOURABLE_MIN_Q,
    seaward_normal,
    wind_fit,
    wind_relation,
    wind_vector,
)

SPOTS: dict[str, dict[str, Any]] = {
    s["slug"]: s for s in json.loads((DEFAULT_DATA_DIR / "spots.json").read_text())
}


def seeded(slug: str, **kw: Any) -> SpotParams:
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
        height_factor=s["height_factor"],
        tide_preference=s["tide_preference"],
        break_type=s["break_type"],
    )
    return replace(base, **kw)


def mid_window(spot: SpotParams) -> float:
    lo, hi = spot.optimal_swell_direction_min, spot.optimal_swell_direction_max
    return ((lo + hi) / 2) % 360 if lo <= hi else ((lo + hi + 360) / 2) % 360


def hour(spot: SpotParams, wind_kmh: float | None, wind_from: float | None, **kw: Any) -> SeaState:
    values: dict[str, Any] = {
        "primary_swell_height_m": 1.5,
        "primary_swell_period_s": spot.ideal_swell_period_s,
        "primary_swell_direction_deg": mid_window(spot),
        "wind_speed_kmh": wind_kmh,
        "wind_direction_deg": wind_from,
    }
    values.update(kw)
    return SeaState(**values)


# SYNTHETIC mirror-image beaches: identical except that one faces east (ocean to the east,
# offshore wind from the west) and the other west (offshore wind from the east).
EAST_FACING = SpotParams(
    slug="synthetic-east-facing",
    latitude=0.0,
    longitude=0.0,
    swell_window_min=0,
    swell_window_max=180,
    optimal_swell_direction_min=70,
    optimal_swell_direction_max=110,
    offshore_wind_direction=270,
    min_swell_period_s=9,
    ideal_swell_period_s=12,
    wave_height_min_ft=2,
    wave_height_max_ft=6,
    break_type="beach",
)
WEST_FACING = replace(
    EAST_FACING,
    slug="synthetic-west-facing",
    swell_window_min=180,
    swell_window_max=360,
    optimal_swell_direction_min=250,
    optimal_swell_direction_max=290,
    offshore_wind_direction=90,
)


# --------------------------------------------------------------------- opposing coastlines
class TestOpposingCoastlines:
    def test_the_seeded_pair_faces_opposite_ways(self) -> None:
        ob, c9 = seeded("ocean-beach-sf"), seeded("cloud-9")
        assert ob.offshore_wind_direction == 90  # west-facing beach: offshore from the east
        assert c9.offshore_wind_direction == 260  # east-facing reef: offshore from the WSW
        # The seaward normals (land -> sea) point almost opposite ways.
        n_ob, n_c9 = seaward_normal(90), seaward_normal(260)
        assert n_ob[0] * n_c9[0] + n_ob[1] * n_c9[1] < -0.98

    @pytest.mark.parametrize("speed", [8.0, 15.0, 25.0])
    def test_a_west_wind_is_onshore_at_ocean_beach_and_offshore_at_cloud_9(
        self, speed: float
    ) -> None:
        ob, c9 = seeded("ocean-beach-sf"), seeded("cloud-9")
        assert wind_relation(speed, 270, ob.offshore_wind_direction) == "onshore"
        assert wind_relation(speed, 270, c9.offshore_wind_direction) == "offshore"
        at_ob, at_c9 = wind_fit(speed, 270, ob), wind_fit(speed, 270, c9)
        assert at_c9.q > 0.99 and at_c9.favourability == "favourable"  # 10 deg off straight
        assert at_ob.q < at_c9.q and at_ob.favourability != "favourable"
        assert at_ob.v_offshore_kmh == pytest.approx(-speed)  # all onshore
        assert at_c9.v_offshore_kmh is not None and at_c9.v_offshore_kmh > 0.98 * speed

    @pytest.mark.parametrize("speed", [8.0, 15.0, 25.0])
    def test_an_east_wind_is_the_reverse(self, speed: float) -> None:
        ob, c9 = seeded("ocean-beach-sf"), seeded("cloud-9")
        assert wind_relation(speed, 90, ob.offshore_wind_direction) == "offshore"
        assert wind_relation(speed, 90, c9.offshore_wind_direction) == "onshore"
        assert wind_fit(speed, 90, ob).q == 1.0
        assert wind_fit(speed, 90, c9).q < 1.0

    def test_scores_and_reasons_follow_the_coast(self) -> None:
        ob, c9 = seeded("ocean-beach-sf"), seeded("cloud-9")
        west, east = (15.0, 270.0), (15.0, 90.0)
        ob_west, ob_east = evaluate(hour(ob, *west), ob), evaluate(hour(ob, *east), ob)
        c9_west, c9_east = evaluate(hour(c9, *west), c9), evaluate(hour(c9, *east), c9)
        assert ob_east.score > ob_west.score and c9_west.score > c9_east.score
        assert ob_west.wind_relation == c9_east.wind_relation == "onshore"
        assert ob_east.wind_relation == c9_west.wind_relation == "offshore"
        assert "onshore wind 15 km/h" in ob_west.unfavourable
        assert "moderate offshore wind" in c9_west.favourable
        assert "moderate offshore wind" in ob_east.favourable
        assert "onshore wind 15 km/h" in c9_east.unfavourable

    @pytest.mark.parametrize("speed", [6.0, 15.0, 30.0])
    def test_the_synthetic_mirror_pair_scores_exactly_opposite(self, speed: float) -> None:
        """The same wind is offshore at one beach and onshore at its mirror image; every
        other factor is identical, and swapping the wind swaps the results exactly."""
        e_sea = hour(EAST_FACING, speed, 270.0, primary_swell_direction_deg=90.0)
        w_sea = hour(WEST_FACING, speed, 270.0, primary_swell_direction_deg=270.0)
        e, w = evaluate(e_sea, EAST_FACING), evaluate(w_sea, WEST_FACING)
        assert e.wind_relation == "offshore" and w.wind_relation == "onshore"
        for factor in ("swell_direction", "swell_period", "wave_size", "consistency"):
            assert e.components[factor] == w.components[factor], factor
        assert e.components["wind"] > w.components["wind"]
        assert e.score > w.score
        e2 = evaluate(replace(e_sea, wind_direction_deg=90.0), EAST_FACING)
        w2 = evaluate(replace(w_sea, wind_direction_deg=90.0), WEST_FACING)
        assert (e2.score, e2.wind_relation) == (w.score, "onshore")
        assert (w2.score, w2.wind_relation) == (e.score, "offshore")
        assert e2.components == w.components and w2.components == e.components

    @pytest.mark.parametrize("from_deg", [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0])
    def test_vectors_point_where_the_wind_goes(self, from_deg: float) -> None:
        """The scoring vector points TO (from + 180), the bearing map and chart arrows use,
        and geo.wind_from_uv (every provider's u/v conversion) inverts it."""
        u, v = wind_vector(10.0, from_deg)
        to_bearing = math.degrees(math.atan2(u, v)) % 360
        assert angular_diff(to_bearing, (from_deg + 180) % 360) < 1e-9
        speed, back = wind_from_uv(u, v)
        assert speed == pytest.approx(10.0) and back == pytest.approx(from_deg, abs=1e-9)
        # A west wind (from 270) blows towards the east: u > 0.
        assert wind_vector(10.0, 270.0)[0] == pytest.approx(10.0)


# ------------------------------------------------------------------- offshore and onshore
class TestOffshoreStrength:
    def test_light_offshore_is_favourable_and_strong_offshore_is_not(self) -> None:
        ob = seeded("ocean-beach-sf")
        results = {s: evaluate(hour(ob, s, 90.0), ob) for s in (8.0, 32.0, 45.0, 60.0)}
        assert results[8.0].components["wind"] == 1.0
        assert "light offshore wind" in results[8.0].favourable
        # Strong offshore: still offshore, but scored lower and never listed as favourable.
        for s in (32.0, 45.0, 60.0):
            assert results[s].wind_relation == "offshore"
            assert results[s].details["wind_detail"]["strength"] == "strong"
            assert not any("offshore wind" in r for r in results[s].favourable)
        assert results[8.0].score > results[32.0].score > results[45.0].score
        assert results[45.0].score > results[60.0].score
        assert "strong offshore wind 60 km/h" in results[60.0].unfavourable
        assert results[60.0].details["wind_detail"]["favourability"] == "unfavourable"

    def test_calm_is_favourable(self) -> None:
        ob = seeded("ocean-beach-sf")
        calm = evaluate(hour(ob, 0.0, None), ob)
        assert calm.wind_relation == "glassy" and calm.components["wind"] == 1.0
        assert "glassy, near-calm wind" in calm.favourable

    def test_light_cross_offshore_is_favourable(self) -> None:
        ob = seeded("ocean-beach-sf")
        r = evaluate(hour(ob, 9.0, 30.0), ob)  # 60 deg from straight offshore
        assert r.wind_relation == "cross-offshore"
        assert r.details["wind_detail"]["favourability"] == "favourable"
        assert "light cross-offshore wind" in r.favourable


class TestOnshorePenalties:
    SPEEDS = (6.0, 8.6, 10.0, 15.0, 25.0, 40.0)

    def test_onshore_wind_costs_more_the_stronger_it_is(self) -> None:
        ob = seeded("ocean-beach-sf")
        fits = [wind_fit(s, 270.0, ob) for s in self.SPEEDS]
        scores = [evaluate(hour(ob, s, 270.0), ob).score for s in self.SPEEDS]
        assert [f.q for f in fits] == sorted((f.q for f in fits), reverse=True)
        assert len(set(f.q for f in fits)) == len(fits)
        assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]
        assert [f.favourability for f in fits] == [
            "workable",  # light onshore: Q >= 0.75, capped (2.2.0)
            "workable",  # 8.6 km/h: still Q >= 0.75, capped
            "workable",
            "unfavourable",
            "unfavourable",
            "unfavourable",
        ]
        assert fits[0].q >= FAVOURABLE_MIN_Q and fits[1].q >= FAVOURABLE_MIN_Q
        assert fits[0].onshore_capped and fits[1].onshore_capped
        assert not fits[2].onshore_capped  # below 0.75 anyway

    def test_light_onshore_is_listed_as_unfavourable(self) -> None:
        ob = seeded("ocean-beach-sf")
        light = evaluate(hour(ob, 6.0, 270.0), ob)
        assert "light onshore wind 6 km/h" in light.unfavourable
        assert not any("onshore" in r for r in light.favourable)
        moderate = evaluate(hour(ob, 25.0, 270.0), ob)
        assert "onshore wind 25 km/h" in moderate.unfavourable

    def test_the_cap_covers_cross_onshore_and_never_changes_q_or_the_score(self) -> None:
        ob = seeded("ocean-beach-sf")
        f = wind_fit(10.0, 300.0, ob)  # 150 deg from straight offshore (90): onshore
        g = wind_fit(12.0, 335.0, ob)  # 115 deg from offshore: cross-onshore
        assert wind_relation(12.0, 335.0, 90.0) == "cross-onshore"
        assert g.q >= FAVOURABLE_MIN_Q and g.favourability == "workable" and g.onshore_capped
        assert f.favourability != "favourable"
        # The score depends on Q only: a capped wind scores like an uncapped one of equal Q.
        r = evaluate(hour(ob, 12.0, 335.0), ob)
        assert r.components["wind"] == round(g.q, 3)
        assert r.components["wind_gate"] == 1.0


# ---------------------------------------------------------------- period and size
class TestPeriodAndSize:
    def test_a_longer_period_is_not_always_better(self) -> None:
        spot = EAST_FACING  # ideal 12 s, working range 2-6 ft (SYNTHETIC beach)
        scores = {}
        for period in (12.0, 16.0, 20.0):
            r = evaluate(
                hour(
                    spot,
                    6.0,
                    270.0,
                    primary_swell_height_m=1.2,
                    primary_swell_period_s=period,
                    primary_swell_direction_deg=90.0,
                ),
                spot,
            )
            assert r.components["swell_period"] == 1.0  # no bonus beyond the ideal period
            scores[period] = r
        # The longer period makes the surf bigger than this break handles.
        assert scores[12.0].score > scores[16.0].score > scores[20.0].score
        assert scores[20.0].breaking.mid_ft > spot.wave_height_max_ft
        assert any(r.startswith("too big for this break") for r in scores[20.0].unfavourable)

    def test_too_short_a_period_is_named_for_the_break_type(self) -> None:
        reef = seeded("cloud-9")  # min period 9 s
        r = evaluate(hour(reef, 6.0, 260.0, primary_swell_period_s=6.0), reef)
        assert r.components["swell_period"] < 0.4
        assert "period too short for this reef (6 s)" in r.unfavourable

    def test_big_waves_are_not_universally_better(self) -> None:
        spot = EAST_FACING
        scores = [
            evaluate(
                hour(spot, 6.0, 270.0, primary_swell_height_m=h, primary_swell_direction_deg=90.0),
                spot,
            ).score
            for h in (0.6, 0.9, 1.2, 1.6, 2.0, 3.0)
        ]
        peak = scores.index(max(scores))
        assert 0 < peak < len(scores) - 1  # rises, then falls
        assert scores[-1] == 0  # far beyond 1.5 x the working maximum
        big = evaluate(
            hour(spot, 6.0, 270.0, primary_swell_height_m=3.0, primary_swell_direction_deg=90.0),
            spot,
        )
        assert any(r.startswith("too big for this break") for r in big.unfavourable)
        small = evaluate(
            hour(spot, 6.0, 270.0, primary_swell_height_m=0.2, primary_swell_direction_deg=90.0),
            spot,
        )
        assert any(r.startswith("too small") for r in small.unfavourable)

    def test_a_blocked_swell_is_named(self) -> None:
        ob = seeded("ocean-beach-sf")  # swell window 190-320
        r = evaluate(hour(ob, 6.0, 90.0, primary_swell_direction_deg=100.0), ob)
        assert any("blocked" in p for p in r.unfavourable)


# --------------------------------------------------------------------- tide and research
class TestTideAndMissingResearch:
    SPOT = replace(EAST_FACING, tide_preference="mid", tide_research_class="moderate")

    def sea(self, stage: float | None) -> SeaState:
        return hour(self.SPOT, 6.0, 270.0, primary_swell_direction_deg=90.0, tide_stage=stage)

    def test_a_researched_tide_is_scored_and_described(self) -> None:
        good, bad = evaluate(self.sea(0.5), self.SPOT), evaluate(self.sea(0.0), self.SPOT)
        assert good.components["tide"] == 1.0 and bad.components["tide"] < 0.4
        assert good.score > bad.score
        assert "unfavourable tide stage" in bad.unfavourable
        assert "tide_detail" in good.details and "tide_excluded_reason" not in good.details

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            ({"tide_research_class": "low"}, TIDE_RESEARCH_LOW),
            ({"tide_research_class": None}, TIDE_NOT_RESEARCHED),  # never assessed
            ({"tide_decision": "disputed"}, TIDE_DISPUTED),
            ({"tide_preference": None}, NO_TIDE_PREFERENCE),
        ],
    )
    def test_without_established_research_the_tide_is_not_scored_or_implied(
        self, change: dict[str, Any], reason: str
    ) -> None:
        spot = replace(self.SPOT, **change)
        at_low = evaluate(replace(self.sea(0.0)), spot)
        at_mid = evaluate(replace(self.sea(0.5)), spot)
        assert "tide" not in at_low.components
        assert at_low.details["tide_excluded_reason"] == reason
        assert at_low.score == at_mid.score  # the stage changes nothing
        for r in (at_low, at_mid):
            assert not any("tide" in p for p in r.favourable + r.unfavourable)


# ------------------------------------------------------------------- reasons and outputs
class TestReasons:
    def test_lists_are_short_distinct_and_ordered_by_points(self) -> None:
        fits = {
            "swell_direction": 1.0,
            "swell_period": 1.0,
            "wave_size": 1.0,
            "wind": 1.0,
            "tide": 1.0,
            "consistency": 1.0,
        }
        used = {
            "swell_direction": 0.2,
            "swell_period": 0.2,
            "wave_size": 0.2,
            "wind": 0.3,
            "tide": 0.05,
            "consistency": 0.05,
        }
        good, bad = condition_reasons(
            fits,
            used_weights=used,
            points_lost={},
            swell_kind="swell",
            swell_period_s=14.0,
            swell_blocked=False,
            break_type="point",
            face_min_ft=4.0,
            face_max_ft=5.0,
            face_mid_ft=4.5,
            size_min_ft=3.0,
            size_max_ft=10.0,
            wind_relation="offshore",
            wind_strength="light",
            wind_favourability="favourable",
            wind_speed_kmh=8.0,
        )
        assert bad == []
        assert len(good) == MAX_REASONS == 3 and len(set(good)) == 3
        assert good[0] == "light offshore wind"  # the heaviest factor first
        assert "favourable tide stage" not in good  # 5 points: below the top three

    def test_nothing_is_said_about_a_factor_that_was_not_scored(self) -> None:
        good, bad = condition_reasons(
            {"wave_size": 0.9},
            used_weights={"wave_size": 1.0},
            points_lost={},
            swell_kind="swell",
            swell_period_s=None,
            swell_blocked=False,
            break_type=None,
            face_min_ft=3.0,
            face_max_ft=4.0,
            face_mid_ft=3.5,
            size_min_ft=2.0,
            size_max_ft=8.0,
            wind_relation="unknown",
            wind_strength=None,
            wind_favourability=None,
            wind_speed_kmh=None,
        )
        assert good == ["good size for this break (3–4 ft)"] and bad == []

    def test_offshore_class(self) -> None:
        assert offshore_class("offshore", "light") == "light offshore"
        assert offshore_class("onshore", "strong") == "strong onshore"
        assert offshore_class("cross-shore", None) == "cross-shore"
        assert offshore_class("glassy", "glassy") == "glassy"
        assert offshore_class("unknown", None) is None and offshore_class(None, None) is None

    def test_hour_outputs_read_the_stored_prediction(self) -> None:
        ob = seeded("ocean-beach-sf")
        r = evaluate(hour(ob, 8.0, 90.0), ob)
        row = SimpleNamespace(
            components=r.stored_components(),
            wind_relation=r.wind_relation,
            algorithm_version=r.model_version,
        )
        out = hour_outputs(row)
        assert out == {
            "favourable": r.favourable,
            "unfavourable": r.unfavourable,
            "offshore_class": "light offshore",
            "model_version": "2.2.0",
        }
        old = SimpleNamespace(
            components={"wind": 0.9}, wind_relation="offshore", algorithm_version="2.0.0"
        )
        assert hour_outputs(old)["favourable"] == [] and hour_outputs(old)["offshore_class"]


# ------------------------------------------------------------------ confidence separation
def _prediction(forecast_cls: str, research_cls: str | None) -> SimpleNamespace:
    """A stored prediction's confidence columns (SYNTHETIC values)."""
    local = fc.lower(forecast_cls, research_cls) if research_cls else fc.LOW
    return SimpleNamespace(
        forecast_confidence_class=forecast_cls,
        local_confidence_class=local,
        lead_hours=30.0,
        confidence_inputs={
            "method": fc.METHOD,
            "class": forecast_cls,
            "reasons": ["A 1-day forecast."],
            "local": {"class": local, "research_class": research_cls, "reason": None},
        },
    )


class TestConfidenceSeparation:
    def test_the_forecast_class_takes_no_research_input(self) -> None:
        assert "research" not in " ".join(inspect.signature(fc.classify_forecast).parameters)
        assert not any("research" in f for f in fc.ForecastInputs.__dataclass_fields__)

    @pytest.mark.parametrize("forecast_cls", [fc.HIGH, fc.MODERATE, fc.LOW])
    def test_changing_research_confidence_never_changes_forecast_confidence(
        self, forecast_cls: str
    ) -> None:
        outs = [fc.api_fields(_prediction(forecast_cls, r)) for r in (fc.HIGH, fc.LOW, None)]
        assert {o["research_confidence"] for o in outs} == {fc.HIGH, fc.LOW, None}
        for o in outs:
            assert o["forecast_confidence"] == {
                "level": forecast_cls,
                "kind": "heuristic",
                "method": fc.METHOD,
            }
            assert o["forecast_confidence_class"] == forecast_cls
            assert o["forecast_confidence_reasons"] == outs[0]["forecast_confidence_reasons"]

    @pytest.mark.parametrize("research_cls", [fc.HIGH, fc.MODERATE, fc.LOW, None])
    def test_changing_forecast_confidence_never_changes_research_confidence(
        self, research_cls: str | None
    ) -> None:
        fresh = [fc.api_fields(_prediction(f, research_cls)) for f in (fc.HIGH, fc.LOW)]
        stale = fc.api_fields(_prediction(fc.HIGH, research_cls), stale=True)
        assert stale["forecast_confidence"]["level"] == fc.MODERATE  # read-time stale cap
        assert [o["forecast_confidence"]["level"] for o in fresh] == [fc.HIGH, fc.LOW]
        assert {o["research_confidence"] for o in [*fresh, stale]} == {research_cls}

    def test_rows_scored_before_forecast_v1_have_no_forecast_confidence(self) -> None:
        old = SimpleNamespace(
            forecast_confidence_class=None,
            local_confidence_class=None,
            lead_hours=None,
            confidence_inputs=None,
        )
        out = fc.api_fields(old)
        assert out["forecast_confidence"] is None and out["research_confidence"] is None
