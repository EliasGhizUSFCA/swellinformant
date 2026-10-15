"""Open-Meteo ECMWF-family adapter (forecasts.open_meteo_ecmwf) against recorded responses.

Uses the real captures of tests/fixtures/open_meteo (see test_open_meteo_adapter.py) for
values. The run times in the metadata are moved where a test needs a 00Z/12Z run; those
metadata (and any series built in a test) are SYNTHETIC.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis

from app.core.redis import get_redis
from app.services.forecasts.blend import blend_source_info
from app.services.forecasts.families import are_independent
from app.services.forecasts.open_meteo_ecmwf import OpenMeteoEcmwfProvider
from app.services.forecasts.registry import SOURCE_CODES, build_provider
from app.services.http import RunNotServedError
from tests.unit.test_open_meteo_adapter import (
    META,
    NOW,
    PIPELINE,
    THURSO,
    Recorder,
    meta_at,
    settings,
    synthetic_hourly,
)

CYCLE_12Z = datetime(2026, 10, 13, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_redis() -> Iterator[None]:
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def provider(rec: Recorder, now: datetime = NOW, **kw: object) -> OpenMeteoEcmwfProvider:
    return OpenMeteoEcmwfProvider(
        settings(**kw),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: now,
    )


def meta_12z() -> dict[str, dict[str, object]]:
    """The captured metadata with the run moved to 12Z (SYNTHETIC), so the captured hours
    (2026-10-14 00Z..23Z+) are leads 12..59 h of a 00/12Z run."""
    return {**META, **meta_at(CYCLE_12Z, CYCLE_12Z + timedelta(hours=7), end_hours=363)}


def test_registered_as_the_ecmwf_family() -> None:
    assert SOURCE_CODES["open_meteo_ecmwf"] == "open_meteo_ecmwf"
    p = build_provider("open_meteo_ecmwf", settings())
    assert isinstance(p, OpenMeteoEcmwfProvider)
    info = p.source_info()
    assert (info.code, info.family, info.kind) == ("open_meteo_ecmwf", "ecmwf_ifs", "deterministic")
    assert (info.wave_model, info.atmosphere_model) == ("ecmwf_wam025", "ecmwf_ifs025")
    assert "Copyright European Centre for Medium-Range Weather Forecasts" in info.attribution
    assert "Open-Meteo.com" in info.attribution
    # Independent of the GFS family, so the blend combines the two.
    assert are_independent("ecmwf_ifs", "ncep_gfs")
    assert blend_source_info().family == "blend"


def test_peak_period_mean_period_and_gust_kind() -> None:
    rec = Recorder(meta=meta_12z())
    p = provider(rec)
    run = p.current_run(NOW)
    assert run.run_key == "ecmwf_wam025:2026101312|ecmwf_ifs025:2026101312"
    result = p.fetch([PIPELINE], run)
    rows = result.records[1]
    first = rows[0]
    assert first.valid_time == datetime(2026, 10, 14, tzinfo=UTC)  # lead 12 h
    # wave_peak_period is Tp; wave_period (ECMWF mwp, the mean period) only in extra.
    assert first.values["wave_period_s"] == 14.65 and first.period_kind == "tp"
    assert first.extra["mean_period_tm10_s"] == 11.3
    assert first.values["sig_wave_height_m"] == 2.26
    # ECMWF open data have no partitions: none stored, nothing invented.
    for name, value in first.values.items():
        if "swell" in name or name.startswith("wind_wave") or name == "sea_level_m":
            assert value is None, name
    assert first.values["wind_gust_kmh"] == 36.4
    assert first.extra["gust_kind"] == "ecmwf_max_3h"
    # Native 3-hourly steps only: the 48 captured hours give 16 rows.
    assert len(rows) == 16
    assert all((r.valid_time - CYCLE_12Z).total_seconds() % (3 * 3600) == 0 for r in rows)
    assert {r.extra["native_step_seconds"] for r in rows} == {10800}
    requests = [c for c in rec.data_calls()]
    assert [c["path"][0] for c in requests] == ["/v1/marine", "/v1/ecmwf"]
    assert requests[0]["hourly"] == ["wave_height,wave_direction,wave_peak_period,wave_period"]
    assert all(c["cell_selection"] == ["sea"] and c["forecast_days"] == ["3"] for c in requests)


def test_horizon_is_15_days_and_six_hourly_after_144_h() -> None:
    rec = Recorder(meta=meta_12z(), override=synthetic_hourly(CYCLE_12Z, 400))
    p = provider(rec, now=CYCLE_12Z + timedelta(hours=8), forecast_days=16)
    rows = p.fetch([PIPELINE], p.current_run(CYCLE_12Z + timedelta(hours=8))).records[1]
    leads = [int((r.valid_time - CYCLE_12Z).total_seconds() // 3600) for r in rows]
    assert leads[:3] == [0, 3, 6] and 144 in leads and 147 not in leads and 150 in leads
    assert leads[-1] == 360  # 15 days (forecast_days is capped at 15 for this family)
    assert len(rows) == 49 + 36
    assert rec.data_calls()[0]["forecast_days"] == ["15"]


def test_only_00z_and_12z_runs_are_read() -> None:
    """The captured metadata says 18Z: that run stops at 144 h and Open-Meteo continues the
    series from the older run, so the 12Z run is the current one and it can no longer be
    read unmixed: nothing is fetched until the next 00Z run."""
    rec = Recorder()
    p = provider(rec)
    run = p.current_run(NOW)
    assert run.wave_model_run_at == CYCLE_12Z and "2026101312" in run.run_key
    with pytest.raises(RunNotServedError, match="only 00/12Z runs") as refused:
        p.ensure_servable(run)
    assert refused.value.retry_after_s is None  # wait for the next poll, no retry storm
    with pytest.raises(RunNotServedError):
        p.fetch([PIPELINE, THURSO], run)
    assert rec.data_calls() == []


def test_a_12z_run_in_its_wait_window_keeps_the_00z_run() -> None:
    available = CYCLE_12Z + timedelta(hours=7)
    p = provider(Recorder(meta=meta_at(CYCLE_12Z, available)))
    run = p.current_run(available + timedelta(minutes=4))
    assert run.wave_model_run_at == CYCLE_12Z - timedelta(hours=12)  # 06Z is skipped
