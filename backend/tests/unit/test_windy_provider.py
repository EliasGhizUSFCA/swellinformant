"""Windy Point Forecast adapter (forecasts.windy) against SYNTHETIC responses.

There is no Windy key: every response is built by tests/windy_fixtures.py from the documented
format (ts in ms, units, ``<parameter>-surface`` arrays, 3-hourly steps, the testing-key
warning, the invalid-key body). No live Windy call is made. Redis (the test database) holds
the sentinel state, the request counter and the provider health; it is flushed around
every test.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis

from app.core.config import Settings
from app.core.redis import get_redis
from app.services.forecasts import provider_manager, quota
from app.services.forecasts.base import (
    ATMOSPHERE,
    FORECAST_FIELDS,
    WAVE,
    PartlyReadRun,
    SpotPoint,
)
from app.services.forecasts.windy import (
    CREDENTIALS_REASON,
    EXTRA_PRESSURE_KIND,
    GFS,
    GFS_WAVE,
    NATIVE_STEP_S,
    SENTINEL_POINTS,
    SENTINEL_SLUG,
    TESTING_KEY_REASON,
    TESTING_WARNING,
    URL,
    WindyCredentialsError,
    WindyProvider,
    WindyQuotaError,
    WindyTestingKeyError,
    estimate_cycle,
    fingerprint,
    in_availability_window,
    ms_to_utc,
    parse_key_part,
    short_fingerprint,
)
from app.services.http import PermanentProviderError, TransientProviderError
from tests.windy_fixtures import (
    START,
    START_MS,
    STEP_MS,
    WindyServer,
    as_testing_key,
    at_points,
    gfs_body,
    uv_from,
    wave_body,
)

ROOT = Path(__file__).resolve().parents[3]
# Real seed coordinates (data/surf_spots/spots.json): break, then offshore forecast point.
PIPELINE = SpotPoint(1, "pipeline", 21.6655, -158.053, 21.8, -158.18)
JBAY = SpotPoint(2, "jeffreys-bay", -34.032, 24.932, -34.2, 25.0)
ULUWATU = SpotPoint(3, "uluwatu", -8.8155, 115.0868, -8.92, 114.98)
# 05:00 UTC: inside the 00Z cycle's expected availability window (04-08 UTC).
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_redis() -> Iterator[None]:
    """Sentinel state, counters and provider health must not leak between tests."""
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {"windy_api_key": "test-key", "forecast_days": 10}
    return Settings(_env_file=None, **{**base, **kw})  # type: ignore[arg-type,call-arg]


def provider(server: WindyServer, **kw: object) -> WindyProvider:
    return WindyProvider(settings(**kw), client=server.client(), backoff_seconds=0)


def requests_of(p: WindyProvider) -> int:
    return p.tally.snapshot()[0]


# --------------------------------------------------------------------------- requests
def test_requires_a_key() -> None:
    with pytest.raises(PermanentProviderError, match="WINDY_API_KEY"):
        WindyProvider(settings(windy_api_key=None))


def test_one_gfs_and_one_gfs_wave_request_per_spot_with_the_documented_body() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    assert server.models() == [GFS_WAVE, GFS]  # the two sentinel checks
    server.calls.clear()
    result = p.fetch([JBAY, ULUWATU], run)
    # 2 spots x (gfsWave + gfs), then the sentinel check of each model (neither is a spot).
    assert server.models() == [GFS_WAVE, GFS, GFS_WAVE, GFS, GFS_WAVE, GFS]
    assert result.request_count == 6 and result.stats is not None
    assert result.stats.http_status_counts == {"200": 6}
    first_wave, first_gfs = server.calls[0], server.calls[1]
    assert first_wave == {
        "lat": -34.2,
        "lon": 25.0,
        "model": "gfsWave",
        "parameters": ["waves", "windWaves", "swell1", "swell2"],
        "levels": ["surface"],
        "key": "test-key",
    }
    # Wind at the break, waves at the offshore forecast point; 2 decimals as Windy rounds.
    assert (first_gfs["lat"], first_gfs["lon"]) == (-34.03, 24.93)
    assert first_gfs["parameters"] == ["wind", "windGust", "pressure"]
    assert first_gfs["levels"] == ["surface"]
    assert sorted(result.records) == [JBAY.spot_id, ULUWATU.spot_id]
    assert result.components == frozenset({WAVE, ATMOSPHERE})


def test_the_sentinel_spot_fetched_last_is_the_check_and_costs_nothing_more() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    server.calls.clear()
    result = p.fetch([PIPELINE, JBAY], run)
    # Exactly 2 requests per spot: Pipeline last, its two responses are the sentinel check.
    assert result.request_count == 4
    assert server.points(GFS_WAVE) == [(-34.2, 25.0), SENTINEL_POINTS[GFS_WAVE]]
    assert server.points(GFS) == [(-34.03, 24.93), SENTINEL_POINTS[GFS]]


def test_the_sentinel_points_are_the_pipeline_spot_rounded_as_windy_rounds() -> None:
    spots = json.loads((ROOT / "data" / "surf_spots" / "spots.json").read_text())
    spot = next(s for s in spots if s["slug"] == SENTINEL_SLUG)
    assert SENTINEL_POINTS[GFS] == (round(spot["latitude"], 2), round(spot["longitude"], 2))
    assert SENTINEL_POINTS[GFS_WAVE] == (
        round(spot["forecast_latitude"], 2),
        round(spot["forecast_longitude"], 2),
    )


def test_a_component_fetch_requests_only_that_model() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    server.calls.clear()
    waves = p.fetch([JBAY, PIPELINE], run, frozenset({WAVE}))
    assert server.models() == [GFS_WAVE, GFS_WAVE]  # Pipeline is the wave sentinel
    assert waves.components == frozenset({WAVE})
    rec = waves.records[JBAY.spot_id][0]
    assert rec.values["sig_wave_height_m"] == 2.0
    assert all(rec.values[f] is None for f in ("wind_speed_kmh", "wind_gust_kmh"))
    server.calls.clear()
    wind = p.fetch([JBAY], run, frozenset({ATMOSPHERE}))
    assert server.models() == [GFS, GFS]  # the spot, then the gfs sentinel check
    assert wind.components == frozenset({ATMOSPHERE})
    rec = wind.records[JBAY.spot_id][0]
    assert rec.values["sig_wave_height_m"] is None
    assert rec.values["wind_speed_kmh"] == pytest.approx(18.0)


# --------------------------------------------------------------------------- normalization
@pytest.mark.parametrize(
    ("u", "v", "from_deg"),
    [
        # u > 0 blows towards the east, v > 0 towards the north: the wind comes FROM the
        # opposite quadrant.
        (3.0, 4.0, 216.87),  # towards the NE -> from the SW
        (3.0, -4.0, 323.13),  # towards the SE -> from the NW
        (-3.0, -4.0, 36.87),  # towards the SW -> from the NE
        (-3.0, 4.0, 143.13),  # towards the NW -> from the SE
        (5.0, 0.0, 270.0),  # towards the east -> from the west
        (0.0, -5.0, 0.0),  # towards the south -> from the north
        (-5.0, 0.0, 90.0),
        (0.0, 5.0, 180.0),
    ],
)
def test_u_v_become_speed_in_kmh_and_the_direction_the_wind_blows_from(
    u: float, v: float, from_deg: float
) -> None:
    body = gfs_body(2)
    body["wind_u-surface"] = [u, u]
    body["wind_v-surface"] = [v, v]
    server = WindyServer(gfs=lambda b: body)
    p = provider(server)
    rec = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id][0]
    assert rec.values["wind_speed_kmh"] == pytest.approx(5.0 * 3.6)  # |(u, v)| = 5 m/s
    assert rec.values["wind_direction_deg"] == pytest.approx(from_deg, abs=0.05)


def test_wave_directions_stay_from_and_units_match_open_meteo() -> None:
    server = WindyServer()
    p = provider(server)
    rec = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id][0]
    v = rec.values
    assert set(v) == set(FORECAST_FIELDS)
    assert (v["sig_wave_height_m"], v["wave_period_s"], v["wave_direction_deg"]) == (
        2.0,
        12.0,
        300.0,
    )
    assert (v["primary_swell_height_m"], v["primary_swell_direction_deg"]) == (1.8, 305.0)
    assert (v["secondary_swell_height_m"], v["secondary_swell_period_s"]) == (0.6, 9.0)
    assert v["secondary_swell_direction_deg"] == 200.0
    assert (v["wind_wave_height_m"], v["wind_wave_direction_deg"]) == (0.4, 60.0)
    assert v["tertiary_swell_height_m"] is None and v["sea_level_m"] is None  # not served
    assert v["wind_gust_kmh"] == pytest.approx(8.0 * 3.6)
    assert v["pressure_msl_hpa"] == pytest.approx(1013.0)
    assert rec.extra[EXTRA_PRESSURE_KIND] == "surface_unspecified"
    assert rec.extra["gust_kind"] == "unknown"
    assert rec.period_kind is None  # Windy does not say which period it returns
    assert rec.extra["wave_model"] == GFS_WAVE  # first record of the spot


def test_millisecond_timestamps_become_utc_datetimes_without_dst() -> None:
    # 2026-11-01 is the US daylight-saving change; UTC steps stay exactly 3 h apart.
    start = datetime(2026, 11, 1, 3, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    server = WindyServer(
        gfs=lambda b: gfs_body(6, start_ms=start_ms),
        wave=lambda b: wave_body(6, start_ms=start_ms),
    )
    p = provider(server)
    records = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id]
    assert [r.valid_time for r in records] == [start + timedelta(hours=3 * i) for i in range(6)]
    assert all(r.valid_time.utcoffset() == timedelta(0) for r in records)
    assert ms_to_utc(1_761_523_200_000) == datetime(2025, 10, 27, tzinfo=UTC)
    assert ms_to_utc(1_761_523_200_000.0) == ms_to_utc(1_761_523_200_000)


def test_three_hourly_steps_are_stored_as_they_come_never_interpolated() -> None:
    server = WindyServer()
    p = provider(server)
    records = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id]
    gaps = {(b.valid_time - a.valid_time) for a, b in zip(records, records[1:], strict=False)}
    assert gaps == {timedelta(hours=3)}
    assert all(r.extra["native_step_seconds"] == NATIVE_STEP_S for r in records)
    assert not any("interpolated" in r.extra for r in records)


def test_missing_variables_are_none_never_zero() -> None:
    wave = wave_body(3, drop=["swell2_height-surface", "swell2_period-surface"])
    wave["waves_period-surface"] = [12.0, None, 11.0]
    wave["wwaves_height-surface"] = [0.0, 0.4, None]  # 0 m wind sea = no wind sea
    gfs = gfs_body(3, drop=["gust-surface"])
    gfs["wind_u-surface"] = [1.0, None, 0.0]
    gfs["wind_v-surface"] = [1.0, 2.0, 0.0]
    server = WindyServer(gfs=lambda b: gfs, wave=lambda b: wave)
    p = provider(server)
    r0, r1, r2 = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id]
    # swell2 height missing: the whole secondary partition is None (its direction too).
    assert all(r.values["secondary_swell_height_m"] is None for r in (r0, r1, r2))
    assert all(r.values["secondary_swell_direction_deg"] is None for r in (r0, r1, r2))
    assert r1.values["wave_period_s"] is None
    assert r0.values["wind_wave_height_m"] is None and r0.values["wind_wave_period_s"] is None
    assert r2.values["wind_wave_height_m"] is None
    assert all(r.values["wind_gust_kmh"] is None for r in (r0, r1, r2))
    assert "gust_kind" not in r0.extra
    assert r1.values["wind_speed_kmh"] is None and r1.values["wind_direction_deg"] is None
    assert r2.values["wind_speed_kmh"] == 0.0 and r2.values["wind_direction_deg"] is None  # calm


def test_a_series_in_an_unknown_unit_is_left_out_with_a_warning() -> None:
    gfs = gfs_body(2)
    gfs["units"]["pressure-surface"] = "inHg"
    server = WindyServer(gfs=lambda b: gfs)
    p = provider(server)
    result = p.fetch([JBAY], p.current_run(NOW))
    assert result.records[JBAY.spot_id][0].values["pressure_msl_hpa"] is None
    assert any("pressure-surface" in w and "inHg" in w for w in result.warnings)


def test_unit_names_spelled_out_are_read_and_other_units_are_still_left_out() -> None:
    wave = wave_body(2)
    wave["units"]["waves_height-surface"] = "meters"
    wave["units"]["swell1_period-surface"] = "seconds"
    wave["units"]["swell2_height-surface"] = "ft"  # never converted by guesswork
    server = WindyServer(wave=lambda b: wave)
    p = provider(server)
    result = p.fetch([JBAY], p.current_run(NOW))
    v = result.records[JBAY.spot_id][0].values
    assert v["sig_wave_height_m"] == 2.0 and v["primary_swell_period_s"] == 14.0
    assert v["secondary_swell_height_m"] is None and v["secondary_swell_period_s"] is None
    assert any("swell2_height-surface" in w and "'ft'" in w for w in result.warnings)


def test_records_end_at_forecast_days_after_the_estimated_cycle() -> None:
    server = WindyServer(gfs=lambda b: gfs_body(130), wave=lambda b: wave_body(130))
    p = provider(server, forecast_days=3)
    run = p.current_run(NOW)  # estimated 00Z of NOW; the bodies start then too
    records = p.fetch([JBAY], run).records[JBAY.spot_id]
    assert records[0].valid_time == START
    assert records[-1].valid_time == START + timedelta(days=3)


def test_a_cell_without_wave_data_is_a_spot_error() -> None:
    empty = wave_body(3)
    for key in [k for k in empty if k.endswith("-surface")]:
        empty[key] = [None, None, None]
    server = WindyServer(wave=at_points({(-34.2, 25.0): empty}, wave_body()))
    p = provider(server)
    result = p.fetch([JBAY, ULUWATU], p.current_run(NOW))
    assert JBAY.spot_id not in result.records
    assert "no wave data" in result.errors[JBAY.spot_id]
    assert ULUWATU.spot_id in result.records


def test_a_failed_wind_request_leaves_the_spot_incomplete_and_says_so() -> None:
    server = WindyServer(
        gfs=lambda b: (
            httpx.Response(400, json={"message": "lat out of range"})
            if b["lat"] == -34.03
            else gfs_body()
        )
    )
    p = provider(server)
    result = p.fetch([JBAY], p.current_run(NOW))
    assert result.records[JBAY.spot_id][0].values["wind_speed_kmh"] is None
    assert "incomplete: wind and pressure unavailable" in result.errors[JBAY.spot_id]


# --------------------------------------------------------------------------- run identity
def test_current_run_is_a_fingerprint_with_an_estimated_cycle_per_component() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    assert run.estimated and not run.testing
    assert run.metadata_source == "windy_fingerprint"
    wave_key, atmos_key = run.component_keys[WAVE], run.component_keys[ATMOSPHERE]
    assert run.run_key == f"{wave_key}|{atmos_key}"
    assert wave_key == (
        f"gfsWave~{short_fingerprint(fingerprint(GFS_WAVE, wave_body()))}:2026102700"
    )
    assert parse_key_part(atmos_key) == (
        GFS,
        short_fingerprint(fingerprint(GFS, gfs_body())),
        datetime(2026, 10, 27, 0, tzinfo=UTC),
    )
    assert run.wave_model_run_at == run.atmosphere_model_run_at == run.issued_at
    assert [(r.model, r.component) for r in run.model_runs] == [(GFS_WAVE, WAVE), (GFS, ATMOSPHERE)]
    assert all(r.available_at == NOW for r in run.model_runs)  # first sighting
    # The run keys never contain a 10-digit run other than the cycle stamp.
    assert [s for s in run.run_key.split("|")] == [wave_key, atmos_key]


def test_an_idle_check_sends_nothing_and_an_unchanged_check_keeps_the_key() -> None:
    server = WindyServer()
    p = provider(server)
    first = p.current_run(NOW)
    assert len(server.calls) == 2
    again = p.current_run(NOW + timedelta(minutes=10))  # within WINDY_CHECK_MINUTES (15)
    assert len(server.calls) == 2 and again.run_key == first.run_key
    later = p.current_run(NOW + timedelta(hours=2))  # due again: same data, same run
    assert len(server.calls) == 4 and later.run_key == first.run_key
    stored = get_redis().get("windy:run-state:v1:gfs")
    assert stored is not None and json.loads(str(stored))["latest"] == fingerprint(GFS, gfs_body())


def test_outside_the_availability_window_the_sentinel_is_checked_four_times_less_often() -> None:
    quiet = datetime(2026, 10, 27, 2, 10, tzinfo=UTC)  # 02-04 UTC: no cycle expected
    assert not in_availability_window(quiet) and in_availability_window(NOW)
    server = WindyServer()
    p = provider(server)
    p.current_run(quiet)
    p.current_run(quiet + timedelta(minutes=30))  # < 4 x 15 min
    assert len(server.calls) == 2
    p.current_run(quiet + timedelta(minutes=61))
    assert len(server.calls) == 4


def test_a_new_wave_run_changes_only_the_wave_key() -> None:
    state = {"shift": 0.0}
    server = WindyServer(wave=lambda b: wave_body(shift=state["shift"]))
    p = provider(server)
    first = p.current_run(NOW)
    state["shift"] = 0.3  # Windy now serves another gfsWave run
    later = NOW + timedelta(hours=6)  # 11:00 UTC: the 06Z cycle is expected
    second = p.current_run(later)
    assert second.component_keys[ATMOSPHERE] == first.component_keys[ATMOSPHERE]
    assert second.component_keys[WAVE] != first.component_keys[WAVE]
    assert second.wave_model_run_at == datetime(2026, 10, 27, 6, tzinfo=UTC)
    assert second.atmosphere_model_run_at == datetime(2026, 10, 27, 0, tzinfo=UTC)  # original


def test_an_older_fingerprint_seen_again_is_never_offered_as_the_run() -> None:
    state = {"shift": 0.0}
    server = WindyServer(wave=lambda b: wave_body(shift=state["shift"]))
    p = provider(server)
    old = p.current_run(NOW).component_keys[WAVE]
    state["shift"] = 0.3
    new = p.current_run(NOW + timedelta(hours=6)).component_keys[WAVE]
    state["shift"] = 0.0  # a lagging Windy server answers the previous run again
    back = p.current_run(NOW + timedelta(hours=7)).component_keys[WAVE]
    assert len(server.calls) == 6  # the check was due and was sent
    assert back == new and back != old  # the newest run seen stays the available run
    stored = json.loads(str(get_redis().get("windy:run-state:v1:gfsWave")))
    assert stored["latest"] == fingerprint(GFS_WAVE, wave_body(shift=0.3))
    assert stored["runs"].keys() == {
        fingerprint(GFS_WAVE, wave_body()),
        fingerprint(GFS_WAVE, wave_body(shift=0.3)),
    }


def test_an_older_fingerprint_of_the_same_estimated_cycle_never_comes_back() -> None:
    """Two fingerprints within one cycle (Windy re-processed it): their keys name the same
    cycle, so the sync's monotonic rule (newest cycle stamp) cannot tell that the first one
    is older. A lagging answer with it must not be offered again: it would be refetched and
    published as a new version with older data."""
    state = {"shift": 0.0}
    server = WindyServer(wave=lambda b: wave_body(shift=state["shift"]))
    p = provider(server)
    first = p.current_run(NOW).component_keys[WAVE]
    state["shift"] = 0.1
    second = p.current_run(NOW + timedelta(hours=1)).component_keys[WAVE]
    a, b = parse_key_part(first), parse_key_part(second)
    assert a is not None and b is not None and a[2] == b[2]  # the same estimated cycle
    assert first != second
    state["shift"] = 0.0
    again = p.current_run(NOW + timedelta(hours=1, minutes=20)).component_keys[WAVE]
    assert len(server.calls) == 6
    assert again == second


@pytest.mark.parametrize(
    ("now", "previous", "expected"),
    [
        # Nothing known: the newest cycle whose expected availability (+4 h) has passed.
        (datetime(2026, 10, 27, 2, 0, tzinfo=UTC), None, datetime(2026, 10, 26, 18, tzinfo=UTC)),
        (datetime(2026, 10, 27, 4, 5, tzinfo=UTC), None, datetime(2026, 10, 27, 0, tzinfo=UTC)),
        # A change before the next cycle is expected but after it can exist (+3 h): early.
        (
            datetime(2026, 10, 27, 9, 25, tzinfo=UTC),
            datetime(2026, 10, 27, 0, tzinfo=UTC),
            datetime(2026, 10, 27, 6, tzinfo=UTC),
        ),
        # A change too soon for any newer cycle: the same run re-processed.
        (
            datetime(2026, 10, 27, 8, 0, tzinfo=UTC),
            datetime(2026, 10, 27, 0, tzinfo=UTC),
            datetime(2026, 10, 27, 0, tzinfo=UTC),
        ),
        # Missed cycles (a long outage): the newest expected one.
        (
            datetime(2026, 10, 27, 20, 0, tzinfo=UTC),
            datetime(2026, 10, 27, 0, tzinfo=UTC),
            datetime(2026, 10, 27, 12, tzinfo=UTC),
        ),
    ],
)
def test_the_estimated_cycle_is_never_older_than_the_previous_run(
    now: datetime, previous: datetime | None, expected: datetime
) -> None:
    assert estimate_cycle(now, previous) == expected
    if previous is not None:
        assert estimate_cycle(now, previous) >= previous


def test_data_that_change_during_the_fetch_are_not_returned() -> None:
    state = {"shift": 0.0}
    server = WindyServer(gfs=lambda b: gfs_body(shift=state["shift"]))
    p = provider(server)
    run = p.current_run(NOW)

    def shifting(body: dict[str, Any]) -> dict[str, Any]:
        if (body["lat"], body["lon"]) == SENTINEL_POINTS[GFS]:
            return gfs_body(shift=0.5)  # Windy switched runs before the last request
        return gfs_body()

    server.gfs = shifting
    with pytest.raises(TransientProviderError, match="mix runs"):
        p.fetch([JBAY], run)
    # The new fingerprint is recorded: the next check sees the new run without a request.
    server.calls.clear()
    nxt = p.current_run(NOW + timedelta(minutes=1))
    assert server.calls == [] and nxt.component_keys[ATMOSPHERE] != run.component_keys[ATMOSPHERE]
    health = provider_manager.provider_health("windy")
    assert health is not None and health.consecutive_unavailable == 1
    # The new run fetches cleanly: the one changed fetch no longer counts.
    server.gfs = lambda b: gfs_body(shift=0.5)
    assert JBAY.spot_id in p.fetch([JBAY], nxt).records
    health = provider_manager.provider_health("windy")
    assert health is not None and health.consecutive_unavailable == 0
    assert not health.blocking(datetime.now(UTC))


def test_data_that_differ_in_every_fetch_make_windy_unavailable() -> None:
    """SYNTHETIC: every answer differs (an unverified risk: a forum report of identical
    requests answered differently). Each fetch ends with "mix runs"; after
    UNAVAILABLE_THRESHOLD of them Windy is unavailable and Open-Meteo stands in, instead of
    every check refetching 50-100 requests that can never be stored."""
    answers = {"n": 0}

    def differing(body: dict[str, Any]) -> dict[str, Any]:
        answers["n"] += 1
        return gfs_body(shift=0.01 * answers["n"])

    server = WindyServer(gfs=differing)
    p = provider(server)
    for minute in range(provider_manager.UNAVAILABLE_THRESHOLD):
        run = p.current_run(NOW + timedelta(minutes=minute))
        with pytest.raises(TransientProviderError, match="mix runs"):
            p.fetch([JBAY], run, frozenset({ATMOSPHERE}))
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.UNAVAILABLE
    assert health.consecutive_unavailable == provider_manager.UNAVAILABLE_THRESHOLD
    assert health.blocking(datetime.now(UTC))
    s = settings(forecast_providers="windy", windy_storage_authorized=True)
    assert provider_manager.active_provider(s).providers == ("open_meteo",)


def test_a_run_without_windy_component_keys_is_refused() -> None:
    from app.services.forecasts.base import RunInfo

    p = provider(WindyServer())
    with pytest.raises(PermanentProviderError, match="fingerprint"):
        p.fetch([JBAY], RunInfo(run_key="gfsWave:2026102700|gfs:2026102700", issued_at=NOW))


# --------------------------------------------------------------------------- keys
def test_a_testing_key_is_refused_on_the_sentinel_and_recorded() -> None:
    server = WindyServer(gfs=lambda b: as_testing_key(gfs_body()))
    p = provider(server)
    with pytest.raises(WindyTestingKeyError) as err:
        p.current_run(NOW)
    assert err.value.reason == TESTING_KEY_REASON == "windy_testing_key"
    assert "never stored or published" in str(err.value)
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.TESTING_KEY
    assert health.blocking(NOW + timedelta(minutes=1))


def test_testing_data_in_any_fetch_response_end_the_fetch_with_nothing_returned() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    server.wave = at_points({(-8.92, 114.98): as_testing_key(wave_body())}, wave_body())
    with pytest.raises(WindyTestingKeyError):
        p.fetch([JBAY, ULUWATU], run)
    # JBAY was answered first, but the fetch returns nothing at all.
    assert server.points(GFS_WAVE)[-1] == (-8.92, 114.98)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            400, json={"message": "Invalid API key", "error": "Bad Request", "statusCode": 400}
        ),
        httpx.Response(401, json={"message": "Unauthorized"}),
        httpx.Response(403, text="Forbidden"),
    ],
)
def test_an_invalid_key_is_a_credentials_error_after_one_request(response: httpx.Response) -> None:
    server = WindyServer(wave=lambda b: response, gfs=lambda b: response)
    p = provider(server)
    with pytest.raises(WindyCredentialsError) as err:
        p.current_run(NOW)
    assert err.value.reason == CREDENTIALS_REASON
    assert len(server.calls) == 1  # never retried, never one failing request per spot
    assert "test-key" not in str(err.value)
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.CREDENTIALS


def test_a_key_echoed_by_windy_never_reaches_an_error_or_the_health_record() -> None:
    key = "synthetic-windy-key-7f3a91"  # a test value, not a real key
    echo = httpx.Response(400, json={"message": f"Invalid API key {key}"})
    server = WindyServer(wave=lambda b: echo, gfs=lambda b: echo)
    p = WindyProvider(settings(windy_api_key=key), client=server.client(), backoff_seconds=0)
    with pytest.raises(WindyCredentialsError) as err:
        p.current_run(NOW)
    assert key not in str(err.value) and "[REDACTED]" in str(err.value)
    health = provider_manager.provider_health("windy")
    assert health is not None and key not in str(health.as_dict())
    server.wave = lambda b: httpx.Response(418, text=f"unexpected answer for {key}")
    with pytest.raises(PermanentProviderError) as other:
        p.current_run(NOW)
    assert key not in str(other.value)
    server.wave = lambda b: {**wave_body(), "warning": f"{TESTING_WARNING} ({key})"}
    with pytest.raises(WindyTestingKeyError) as testing:
        p.current_run(NOW)
    assert key not in str(testing.value)
    health = provider_manager.provider_health("windy")
    assert health is not None and key not in str(health.as_dict())


def test_the_daily_limit_answer_is_a_quota_error_until_midnight_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The adapter's clock at NOW (05:00 UTC): the wait is the 19 h to Windy's 00:00 UTC reset
    # (with the wall clock the test failed in the last hour of every UTC day).
    from app.services.forecasts import windy as windy_module

    monkeypatch.setattr(windy_module, "utcnow", lambda: NOW)
    server = WindyServer(wave=lambda b: httpx.Response(429, json={"message": "Too many requests"}))
    p = provider(server)
    with pytest.raises(WindyQuotaError) as err:
        p.current_run(NOW)
    assert len(server.calls) == 1  # 429 is not retried
    assert err.value.retry_after_s == pytest.approx(19 * 3600)
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.QUOTA
    limit = WindyServer(gfs=lambda b: httpx.Response(400, json={"message": "Daily limit exceeded"}))
    with pytest.raises(WindyQuotaError):
        provider(limit).fetch(
            [JBAY], provider(WindyServer()).current_run(NOW), frozenset({ATMOSPHERE})
        )


def test_the_windy_daily_request_limit_is_counted_before_each_request() -> None:
    server = WindyServer()
    p = provider(server, windy_daily_request_limit=3)
    run = p.current_run(NOW)  # 2 requests
    with pytest.raises(WindyQuotaError, match="WINDY_DAILY_REQUEST_LIMIT"):
        p.fetch([JBAY, ULUWATU], run)  # the 4th request is refused before it is sent
    assert len(server.calls) == 3
    usage = quota.windy_request_usage(settings=settings(windy_daily_request_limit=3))
    assert usage is not None and usage["used"] == 3 and usage["limit"] == 3


def test_every_request_failing_transiently_counts_as_unavailable() -> None:
    server = WindyServer()
    p = provider(server)
    run = p.current_run(NOW)
    server.gfs = server.wave = lambda b: httpx.Response(503)
    result = p.fetch([JBAY], run)
    assert result.records == {} and "HTTP 503" in result.errors[JBAY.spot_id]
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.UNAVAILABLE
    assert health.consecutive_unavailable == 1 and not health.blocking(NOW)


def test_a_failed_sentinel_holds_back_only_its_own_component_when_partial() -> None:
    """Design D6: the gfsWave sentinel timing out does not hold a new gfs run back. Without
    ``partial`` the check raises as before; with it, the atmosphere run comes back alone and
    names why the waves are unknown; nothing records a success."""
    server = WindyServer(wave=lambda b: httpx.Response(503))
    p = provider(server)
    with pytest.raises(TransientProviderError):
        p.current_run(NOW)
    run = p.current_run(NOW, partial=True)
    assert isinstance(run, PartlyReadRun) and set(run.unread) == {WAVE}
    assert set(run.component_keys) == {ATMOSPHERE} and run.estimated
    assert run.run_key == run.component_keys[ATMOSPHERE]
    assert run.wave_model_run_at is None
    assert run.atmosphere_model_run_at == datetime(2026, 10, 27, 0, tzinfo=UTC)
    assert [(r.model, r.component) for r in run.model_runs] == [(GFS, ATMOSPHERE)]
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.UNAVAILABLE
    assert health.last_success_at is None
    # The readable component is fetched on its own with that run.
    result = p.fetch([JBAY], run, frozenset({ATMOSPHERE}))
    (first, *_) = result.records[JBAY.spot_id]
    assert first.values["wind_speed_kmh"] is not None
    assert first.values["sig_wave_height_m"] is None


def test_partial_still_raises_when_no_sentinel_answers() -> None:
    server = WindyServer(wave=lambda b: httpx.Response(503), gfs=lambda b: httpx.Response(503))
    with pytest.raises(TransientProviderError):
        provider(server).current_run(NOW, partial=True)


def test_a_successful_check_records_success() -> None:
    provider_manager.record_provider_failure("windy", provider_manager.CREDENTIALS, "old key")
    p = provider(WindyServer())
    p.current_run(NOW)
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_success_at is not None
    assert not health.blocking(NOW)


def test_the_source_names_the_windy_attribution_and_licence() -> None:
    info = provider(WindyServer()).source_info()
    assert (info.code, info.provider, info.family) == ("windy_gfswave", "windy", "ncep_gfs")
    assert (info.wave_model, info.atmosphere_model) == ("gfsWave", "gfs")
    assert "Contains data from the Windy database" in info.attribution
    assert "WINDY_STORAGE_AUTHORIZED" in info.license_notes
    assert URL == "https://api.windy.com/api/point-forecast/v2"


def test_each_component_reports_the_six_hour_update_interval() -> None:
    p = provider(WindyServer())
    assert p.update_interval_seconds(WAVE) == p.update_interval_seconds(ATMOSPHERE) == 21600
    with pytest.raises(ValueError, match="unknown component"):
        p.update_interval_seconds("tide")


def test_the_synthetic_fixture_u_v_follow_the_documented_convention() -> None:
    # A wind FROM the north (0 deg) blows towards the south: v < 0, u = 0.
    u, v = uv_from(5.0, 0.0)
    assert u == pytest.approx(0.0, abs=1e-12) and v == pytest.approx(-5.0)
    assert STEP_MS == 3 * 3600 * 1000 and START_MS % STEP_MS == 0


# --------------------------------------------------------------------------- equivalence
def test_windy_records_have_the_open_meteo_structure_units_and_types() -> None:
    """The same physical values reach the same normalized records from both providers.

    Open-Meteo: its recorded Pipeline responses (tests/fixtures/open_meteo). Windy: a
    SYNTHETIC response carrying those values in Windy's format (ts in ms, u/v in m/s,
    pressure in Pa, swell1/swell2/wwaves), at every third hour (Windy's 3-hourly steps).
    """
    from tests.unit.test_open_meteo_adapter import PIPELINE as OM_PIPELINE
    from tests.unit.test_open_meteo_adapter import Recorder
    from tests.unit.test_open_meteo_adapter import fetch as om_fetch

    _, om_result = om_fetch(Recorder(), [OM_PIPELINE])
    om = [r for r in om_result.records[OM_PIPELINE.spot_id] if r.valid_time.hour % 3 == 0]
    assert len(om) >= 8
    ts = [int(r.valid_time.timestamp() * 1000) for r in om]

    def series(name: str, scale: float = 1.0) -> list[float | None]:
        return [None if r.values[name] is None else r.values[name] * scale for r in om]

    wave: dict[str, Any] = {"ts": ts, "units": {}}
    for prefix, field in (
        ("swell1", "primary_swell"),
        ("swell2", "secondary_swell"),
        ("wwaves", "wind_wave"),
    ):
        for part, suffix in (
            ("height", "height_m"),
            ("period", "period_s"),
            ("direction", "direction_deg"),
        ):
            wave[f"{prefix}_{part}-surface"] = series(f"{field}_{suffix}")
    wave["waves_height-surface"] = series("sig_wave_height_m")
    wave["waves_period-surface"] = series("wave_period_s")
    wave["waves_direction-surface"] = series("wave_direction_deg")
    us: list[float | None] = []
    vs: list[float | None] = []
    for r in om:
        speed, direction = r.values["wind_speed_kmh"], r.values["wind_direction_deg"]
        if speed is None or direction is None:
            us.append(None)
            vs.append(None)
            continue
        u, v = uv_from(speed / 3.6, direction)
        us.append(u)
        vs.append(v)
    gfs = {
        "ts": ts,
        "units": {"wind_u-surface": "m*s-1", "wind_v-surface": "m*s-1", "pressure-surface": "Pa"},
        "wind_u-surface": us,
        "wind_v-surface": vs,
        "gust-surface": series("wind_gust_kmh", 1 / 3.6),
        "pressure-surface": series("pressure_msl_hpa", 100.0),
    }
    server = WindyServer(gfs=lambda b: gfs, wave=lambda b: wave)
    p = provider(server, forecast_days=3)
    windy = p.fetch([JBAY], p.current_run(NOW)).records[JBAY.spot_id]

    assert [r.valid_time for r in windy] == [r.valid_time for r in om]
    unserved = {f for f in FORECAST_FIELDS if f.startswith("tertiary_swell")} | {"sea_level_m"}
    compared = 0
    for w, o in zip(windy, om, strict=True):
        assert set(w.values) == set(o.values) == set(FORECAST_FIELDS)
        assert w.valid_time.tzinfo is not None and w.valid_time.utcoffset() == timedelta(0)
        assert type(w.extra["native_step_seconds"]) is type(o.extra["native_step_seconds"]) is int
        for name in FORECAST_FIELDS:
            if name in unserved:
                assert w.values[name] is None  # Windy has no third swell and no sea level
                continue
            ov, wv = o.values[name], w.values[name]
            assert (ov is None) == (wv is None), name
            if ov is not None and wv is not None:
                assert isinstance(wv, float) and isinstance(ov, float)
                tolerance = 0.11 if name.endswith("direction_deg") else 1e-6
                assert wv == pytest.approx(ov, abs=tolerance), name
                compared += 1
    assert compared > 8 * 10
