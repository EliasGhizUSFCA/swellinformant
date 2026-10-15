"""The spot timeline assembly (app/services/timeline.py), without a database.

Real rows come from tests/fixtures/timeline/jbay_blend_3h.json; the cadence and time-zone
cases re-time them (synthetic valid times, see tests/timeline_fixtures.py).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.schemas.tides import (
    TIDE_PREFERENCE_NOT_ESTABLISHED,
    ObservationOut,
    ObservationsBlockOut,
    TideBlockOut,
    TideEventOut,
    TidePointOut,
    TidePreferenceOut,
)
from app.services import timesteps
from app.services.surf_quality.breaking import dominant_component
from app.services.surf_quality.forecast_confidence import STALE_REASON
from app.services.surf_quality.sun import TWILIGHT_MARGIN, sun_times
from app.services.tides.api import NO_OBSERVATIONS, NO_TIDE_SOURCE
from app.services.tides.stage import TideStage
from app.services.timeline import (
    NO_FUTURE_DATA,
    assemble_timeline,
    driving_partition,
    timeline_etag,
)
from tests.timeline_fixtures import (
    Rows,
    build_rows,
    ecmwf_shaped_times,
    jbay_spot,
    regular_times,
    spot,
)

NOW = datetime(2026, 10, 11, 18, tzinfo=UTC)
PREFERENCE = TidePreferenceOut(
    value=None, research_class=None, decision=None, text=TIDE_PREFERENCE_NOT_ESTABLISHED
)
NO_TIDE = TideBlockOut(status="unavailable", reason=NO_TIDE_SOURCE, preference=PREFERENCE)
NO_OBS = ObservationsBlockOut(status="unavailable", reason=NO_OBSERVATIONS)


def assemble(  # type: ignore[no-untyped-def]
    rows: Rows,
    site=None,
    *,
    stale: bool = False,
    tide: TideBlockOut = NO_TIDE,
    stages=None,
    after: datetime | None = None,
    before: datetime | None = None,
):
    return assemble_timeline(
        site or jbay_spot(),
        rows,
        after=after,
        before=before,
        stale=stale,
        provenance=None,
        tide=tide,
        observations=NO_OBS,
        stages=stages or {},
        now=NOW,
    )


# --- steps and segments --------------------------------------------------------------


def test_steps_are_the_stored_valid_times_with_their_durations() -> None:
    rows = build_rows()
    tl = assemble(rows)
    assert tl.available and len(tl.steps) == len(rows) == 40
    assert [s.t for s in tl.steps] == [p.valid_time for p, _ in rows]
    assert {s.step_hours for s in tl.steps} == {3.0}
    assert [seg.model_dump() for seg in tl.steps_meta.segments] == [
        {
            "start": rows[0][0].valid_time,
            "end": rows[-1][0].valid_time + timedelta(hours=3),
            "step_hours": 3.0,
        }
    ]
    assert tl.steps_meta.interpolation == "none"
    assert tl.steps_meta.policy == "nearest-valid-step"
    # Nothing between the stored times: no 04:00 step in 3-hourly data.
    assert all(s.t.hour % 3 == 0 for s in tl.steps)
    assert not any(s.interpolated for s in tl.steps)


def test_ecmwf_shaped_series_gives_a_3_hour_then_a_6_hour_segment() -> None:
    times = ecmwf_shaped_times(datetime(2026, 10, 12, 0, tzinfo=UTC), 24, 16)  # synthetic
    tl = assemble(build_rows(times))
    segs = tl.steps_meta.segments
    assert [(s.start, s.end, s.step_hours) for s in segs] == [
        (times[0], times[23], 3.0),
        (times[23], times[-1] + timedelta(hours=6), 6.0),
    ]
    # The step before the 6-hour gap lasts 6 hours: a step stands for the time to the next.
    assert [s.step_hours for s in tl.steps[22:25]] == [3.0, 6.0, 6.0]
    # The last step keeps its stretch's spacing (the series' median gap would say 3 h).
    assert tl.steps[-1].step_hours == 6.0
    assert timesteps.step_hours(times)[-1] == 3.0


def test_last_step_lasts_until_the_next_stored_time_beyond_the_window() -> None:
    times = regular_times(datetime(2026, 10, 12, 0, tzinfo=UTC), 3, 10)  # synthetic
    tl = assemble(build_rows(times), after=times[-1] + timedelta(hours=6))
    assert tl.steps[-1].step_hours == 6.0
    assert tl.steps_meta.segments[-1].end == times[-1] + timedelta(hours=6)
    # The same stretches as the frozen helper finds on the series including the next time
    # (whose own entry, the extra point, is dropped).
    whole = timesteps.segments([*times, times[-1] + timedelta(hours=6)])[:-1]
    assert [(s.start, s.end, s.step_hours) for s in tl.steps_meta.segments] == [
        (s.start, s.end, s.step_hours) for s in whole
    ]


def test_a_lone_last_step_keeps_the_runs_spacing_before_it() -> None:
    """A window that holds only the run's last stored time: the step lasts as long as the
    gap from the stored time before it, never 0 h (which would draw nothing)."""
    times = regular_times(datetime(2026, 10, 12, 0, tzinfo=UTC), 6, 1)  # synthetic
    tl = assemble(build_rows(times), before=times[0] - timedelta(hours=6))
    assert [s.step_hours for s in tl.steps] == [6.0]
    assert tl.steps_meta.segments[0].end == times[0] + timedelta(hours=6)
    # Without any other stored time there is nothing to measure: 0 h, not a made-up length.
    assert assemble(build_rows(times)).steps[0].step_hours == 0.0


def test_no_rows_is_unavailable_and_says_why() -> None:
    tl = assemble([])
    assert not tl.available and tl.message == NO_FUTURE_DATA
    assert tl.steps == [] and tl.days == [] and tl.steps_meta.segments == []


# --- quality, breaking and the components -------------------------------------------


def test_quality_and_breaking_are_the_stored_prediction() -> None:
    rows = build_rows()
    tl = assemble(rows)
    for step, (p, _) in zip(tl.steps, rows, strict=True):
        assert (step.quality.score, step.quality.label) == (p.score, p.label.value)
        b = step.breaking
        assert b.central_ft == p.breaking_height_mid_ft
        assert (b.typical_ft, b.sets_ft) == (p.breaking_height_min_ft, p.breaking_height_max_ft)
        assert (b.p10_ft, b.p90_ft) == (p.breaking_p10_ft, p.breaking_p90_ft)


def test_quality_does_not_depend_on_wave_height_alone() -> None:
    """The strip shows the stored score: changing the breaking heights changes nothing."""
    rows = build_rows()
    before = [s.quality for s in assemble(rows).steps]
    for p, _ in rows:
        p.breaking_height_mid_ft = (p.breaking_height_mid_ft or 0) * 3
        p.breaking_height_max_ft *= 3
    assert [s.quality for s in assemble(rows).steps] == before


def test_forecast_reasons_exclude_the_research_sentence_and_stale_comes_first() -> None:
    rows = build_rows()
    p = rows[0][0]
    local = p.confidence_inputs["local"]["reason"]  # type: ignore[index]
    tl = assemble(rows)
    assert tl.steps[0].quality.forecast_confidence_reasons == p.confidence_inputs["reasons"]  # type: ignore[index]
    assert local not in tl.steps[0].quality.forecast_confidence_reasons
    stale = assemble(rows, stale=True).steps[0].quality
    assert stale.forecast_confidence_reasons[0] == STALE_REASON
    assert stale.forecast_confidence_class in {"moderate", "low"}


def test_why_lists_only_stored_fits_and_gates() -> None:
    rows = build_rows()
    p = rows[0][0]
    why = assemble(rows).steps[0].why
    comp = p.components
    assert why.fits == {
        k: comp[k]
        for k in ("swell_direction", "swell_period", "wave_size", "wind", "tide", "consistency")
        if comp.get(k) is not None
    }
    assert why.gates == {
        "size": comp["size_gate"],
        "period": comp["period_gate"],
        "wind": comp["wind_gate"],
    }
    assert why.contributions is None  # algorithm 2.0.0 rows do not store them
    assert why.tide_included is False and why.explanation == p.explanation
    comp["contributions"] = {"wind": 4.0, "swell_period": 12.5}
    comp["tide"] = 0.8
    why = assemble(rows).steps[0].why
    assert why.contributions == {"wind": 4.0, "swell_period": 12.5}
    assert why.tide_included is True and why.fits["tide"] == 0.8


# --- partitions -----------------------------------------------------------------------


def test_partitions_list_only_stored_systems_and_flag_the_driver() -> None:
    rows = build_rows()
    tl = assemble(rows)
    drivers = 0
    for step, (p, f) in zip(tl.steps, rows, strict=True):
        kinds = [q.kind for q in step.partitions]
        expected = [
            k
            for k, h in (
                ("primary_swell", f.primary_swell_height_m),
                ("secondary_swell", f.secondary_swell_height_m),
                ("tertiary_swell", f.tertiary_swell_height_m),
                ("wind_sea", f.wind_wave_height_m),
            )
            if h is not None and h > 0
        ]
        assert kinds == expected
        driving = [q for q in step.partitions if q.drives_surf]
        assert len(driving) <= 1
        if driving:
            drivers += 1
            q = driving[0]
            h, t, d = dominant_component(p, f)
            assert q.kind == driving_partition(p)
            # The breakdown stores directions to the degree, the forecast row to 0.1 deg.
            assert (q.height_m, q.period_s) == pytest.approx((h, t), abs=0.01)
            assert q.dir_from_deg == pytest.approx(d, abs=0.5)
    assert drivers == len(rows)
    # In this run the wind sea, not the primary swell, drives the surf at some steps.
    assert {q.kind for s in tl.steps for q in s.partitions if q.drives_surf} >= {"primary_swell"}


def test_missing_partitions_are_left_out_not_zero_filled() -> None:
    rows = build_rows()[:2]
    f = rows[0][1]
    f.secondary_swell_height_m = None
    f.tertiary_swell_height_m = None
    f.wind_wave_height_m = None
    step = assemble(rows).steps[0]
    assert [q.kind for q in step.partitions] == ["primary_swell"]


# --- wind -----------------------------------------------------------------------------


def test_wind_without_wind_detail_keeps_strength_and_favourability_unknown() -> None:
    rows = build_rows()
    p, f = rows[0]
    w = assemble(rows).steps[0].wind
    assert w is not None
    assert w.speed_kmh == pytest.approx(f.wind_speed_kmh, abs=0.01)
    assert w.dir_from_deg == pytest.approx(f.wind_direction_deg, abs=0.1)
    assert w.relation == p.wind_relation and w.fit == p.components["wind"]
    assert w.strength is None and w.favourability is None
    assert w.offshore_component_kmh is None and w.gust_kind is None


def test_wind_detail_and_gust_flags_are_carried() -> None:
    rows = build_rows()
    p, f = rows[0]
    p.components["wind_detail"] = {
        "q": 0.9,
        "v_offshore_kmh": 5.5,
        "v_cross_kmh": 1.2,
        "strength": "light",
        "favourability": "favourable",
    }
    f.extra = {"gust_kind": "gfs_instantaneous"}
    f.wind_speed_kmh, f.wind_gust_kmh = 20.0, 15.0
    w = assemble(rows).steps[0].wind
    assert w is not None
    assert (w.fit, w.strength, w.favourability) == (0.9, "light", "favourable")
    assert (w.offshore_component_kmh, w.cross_component_kmh) == (5.5, 1.2)
    assert w.gust_kind == "gfs_instantaneous" and w.gust_below_mean is True
    f.wind_gust_kmh = None
    w = assemble(rows).steps[0].wind
    assert w is not None and w.gust_kmh is None and w.gust_below_mean is False


def test_step_without_wind_data_has_no_wind() -> None:
    rows = build_rows()[:3]
    rows[1][1].wind_speed_kmh = None
    rows[1][1].wind_direction_deg = None
    tl = assemble(rows)
    assert tl.steps[1].wind is None and tl.steps[0].wind is not None


# --- local days, daylight and good hours ---------------------------------------------


def test_days_are_local_midnights_with_sun_times_from_sun_py() -> None:
    rows = build_rows()
    site = jbay_spot()
    tl = assemble(rows, site)
    first_local = rows[0][0].valid_time + timedelta(hours=2)  # SAST, no DST
    assert tl.days[0].date == first_local.date()
    for d in tl.days:
        assert d.end_utc - d.start_utc == timedelta(hours=24)
        assert (d.start_utc, d.end_utc) == timesteps.local_day_bounds(d.date, site.timezone)
        rise, set_, state = sun_times(site.latitude, site.longitude, d.date)
        assert state == d.polar_state == "normal"
        assert d.sunrise is not None and rise is not None and set_ is not None
        assert abs((d.sunrise - rise).total_seconds()) < 60
        assert abs((d.sunset - set_).total_seconds()) < 60  # type: ignore[operator]
        assert d.first_light == d.sunrise - TWILIGHT_MARGIN
        assert d.last_light == d.sunset + TWILIGHT_MARGIN  # type: ignore[operator]
    # The days cover every step.
    assert tl.days[0].start_utc <= tl.steps[0].t and tl.steps[-1].t < tl.days[-1].end_utc


def test_day_best_and_good_hours_are_duration_weighted() -> None:
    rows = build_rows()
    site = jbay_spot()
    tl = assemble(rows, site)
    for d in tl.days:
        inside = [s for s in tl.steps if d.start_utc <= s.t < d.end_utc and s.is_daylight]
        if not inside:
            assert d.best_time is None and d.good_hours == 0
            continue
        top = max(inside, key=lambda s: s.quality.score)
        assert (d.best_time, d.best_score, d.best_label) == (
            top.t,
            top.quality.score,
            top.quality.label,
        )
        good = [s for s in tl.steps if s.is_daylight and s.quality.score >= 57]
        hours = sum(
            (min(s.t + timedelta(hours=3), d.end_utc) - max(s.t, d.start_utc)).total_seconds()
            / 3600
            for s in good
            if s.t < d.end_utc and s.t + timedelta(hours=3) > d.start_utc
        )
        assert d.good_hours == pytest.approx(hours)
        # Hours, not a count of 3-hour steps.
        assert d.good_hours % 3 == 0


def test_lisbon_autumn_change_day_lasts_25_hours() -> None:
    site = spot("ericeira", "Europe/Lisbon", 38.98, -9.42)
    times = regular_times(datetime(2026, 10, 24, 0, tzinfo=UTC), 3, 24)  # synthetic
    tl = assemble(build_rows(times), site)
    day = next(d for d in tl.days if d.date == date(2026, 10, 25))
    assert day.start_utc == datetime(2026, 10, 24, 23, tzinfo=UTC)  # 00:00 WEST
    assert day.end_utc == datetime(2026, 10, 26, 0, tzinfo=UTC)  # 00:00 WET
    assert day.end_utc - day.start_utc == timedelta(hours=25)


def test_auckland_spring_change_day_lasts_23_hours() -> None:
    site = spot("raglan", "Pacific/Auckland", -37.80, 174.87)
    times = regular_times(datetime(2026, 9, 26, 0, tzinfo=UTC), 3, 24)  # synthetic
    tl = assemble(build_rows(times), site)
    day = next(d for d in tl.days if d.date == date(2026, 9, 27))
    assert day.end_utc - day.start_utc == timedelta(hours=23)


@pytest.mark.parametrize(
    ("start", "state"),
    [
        (datetime(2026, 12, 20, 0, tzinfo=UTC), "polar_night"),
        (datetime(2026, 6, 20, 0, tzinfo=UTC), "polar_day"),
    ],
)
def test_polar_days_have_no_sunrise(start: datetime, state: str) -> None:
    site = spot("tromso-test", "Europe/Oslo", 69.65, 18.96)  # synthetic high-latitude spot
    tl = assemble(build_rows(regular_times(start, 3, 16)), site)
    middle = tl.days[1]
    assert middle.polar_state == state
    assert middle.sunrise is None and middle.sunset is None
    assert middle.first_light is None and middle.last_light is None


def test_sunrise_falls_in_the_civil_day_across_the_date_line() -> None:
    """Tonga keeps UTC+13 at 175 W, a day ahead of its solar date: the sunrise shown for a
    local date is the one that happens on that local date."""
    site = spot("haatafu-test", "Pacific/Tongatapu", -21.06, -175.33)
    tl = assemble(build_rows(regular_times(datetime(2026, 10, 12, 0, tzinfo=UTC), 3, 24)), site)
    for d in tl.days:
        if d.sunrise is None:
            continue
        assert d.start_utc <= d.sunrise < d.end_utc
        rise, _, _ = sun_times(site.latitude, site.longitude, d.date - timedelta(days=1))
        assert rise is not None and abs((d.sunrise - rise).total_seconds()) < 60


# --- tide -------------------------------------------------------------------------------


def test_tide_unavailable_gives_no_step_heights_and_keeps_the_reason() -> None:
    tl = assemble(build_rows())
    assert tl.tide.status == "unavailable" and tl.tide.reason == NO_TIDE_SOURCE
    assert tl.tide.curve == [] and all(s.tide is None for s in tl.steps)
    assert tl.tide.preference.text == TIDE_PREFERENCE_NOT_ESTABLISHED


def test_step_tide_is_read_from_the_curve() -> None:
    rows = build_rows()[:4]
    t0 = rows[0][0].valid_time
    # Synthetic tide curve (hourly) with one high water after the second step.
    curve = [
        TidePointOut(t=t0 + timedelta(hours=h), height_m=0.2 + 0.1 * h, interpolated=False)
        for h in range(0, 8)
    ] + [
        TidePointOut(t=t0 + timedelta(hours=h), height_m=1.0 - 0.1 * (h - 8), interpolated=False)
        for h in range(8, 12)
    ]
    events = [TideEventOut(t=t0 + timedelta(hours=8), kind="high", height_m=1.0)]
    tide = TideBlockOut(
        status="demo", datum="MSL", curve=curve, events=events, preference=PREFERENCE
    )
    stage = TideStage(stage=0.4, rising=True, range_m=0.8, source="demo")
    tl = assemble(rows, tide=tide, stages={rows[0][0].valid_time: stage})
    first, second, third = (s.tide for s in tl.steps[:3])
    assert first is not None and first.height_m == pytest.approx(0.2) and first.rising is True
    assert first.stage == 0.4
    assert second is not None and second.height_m == pytest.approx(0.5) and second.stage is None
    assert third is not None and third.height_m == pytest.approx(0.8) and third.rising is True
    fourth = tl.steps[3].tide
    assert fourth is not None and fourth.height_m == pytest.approx(0.9) and fourth.rising is None
    # Never from model sea level, even when the model has one.
    rows[0][1].sea_level_m = 5.0
    assert assemble(rows, tide=tide).steps[0].tide.height_m == pytest.approx(0.2)  # type: ignore[union-attr]


def test_step_outside_the_tide_curve_has_no_tide() -> None:
    rows = build_rows()[:3]
    t0 = rows[0][0].valid_time
    tide = TideBlockOut(
        status="available",
        datum="MLLW",
        curve=[TidePointOut(t=t0, height_m=0.5, interpolated=False)],
        preference=PREFERENCE,
    )
    tl = assemble(rows, tide=tide)
    assert tl.steps[0].tide is not None and tl.steps[1].tide is None


# --- ETag -----------------------------------------------------------------------------


def test_etag_ignores_generation_time_but_follows_the_data() -> None:
    rows = build_rows()
    a = assemble(rows)
    b = a.model_copy(update={"generated_at": NOW + timedelta(minutes=4)})
    assert timeline_etag(a, 10) == timeline_etag(b, 10)
    assert timeline_etag(a, 10).startswith('W/"')
    assert timeline_etag(a, 10) != timeline_etag(a, 5)
    assert timeline_etag(a, 10) != timeline_etag(assemble(rows, stale=True), 10)
    assert timeline_etag(a, 10) != timeline_etag(assemble(rows[1:]), 10)
    # A re-score of the same run (same run key and algorithm version, e.g. after new
    # research evidence) changes the ratings, so the validator changes too.
    rescored = build_rows()
    rescored[5][0].score -= 7
    assert timeline_etag(a, 10) != timeline_etag(assemble(rescored), 10)
    obs = a.model_copy(
        update={
            "observations": ObservationsBlockOut(
                status="available",
                items=[
                    ObservationOut(
                        t=NOW,
                        kind="instrument",
                        station_code="x",
                        station_name="x",
                        provider="ndbc",
                        relation="offshore_reference",
                        distance_km=10,
                        hs_m=1.0,
                        hs_kind="spectral_hm0",
                        tp_s=10,
                        dir_from_deg=200,
                        wind_kmh=None,
                        qc_flag=None,
                        stale=False,
                        licence=None,
                        attribution=None,
                    )
                ],
            )
        }
    )
    assert timeline_etag(a, 10) != timeline_etag(obs, 10)
