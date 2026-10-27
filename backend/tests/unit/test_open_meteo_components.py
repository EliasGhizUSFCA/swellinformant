"""Open-Meteo components (design D6): wave and atmosphere have run keys of their own, a
component-only fetch requests only that component's API, and servability is checked per
component.

The metadata is the recorded ``meta.json`` of tests/fixtures/open_meteo with SYNTHETIC
run times (``meta_at``) where a test needs a new run on one side; data responses are the
recorded captures or SYNTHETIC series (tests/unit/test_open_meteo_adapter.py helpers).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import redis

from app.core.redis import get_redis
from app.services.forecasts.base import COMPONENT_FIELDS
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.http import RunNotServedError
from app.services.sync import detect
from tests.unit.test_open_meteo_adapter import (
    INIT,
    JBAY,
    META,
    NOW,
    PIPELINE,
    Recorder,
    meta_at,
    provider,
    settings,
    synthetic_hourly,
)

WAVE_DOMAINS = ("ncep_gfswave016", "ncep_gfswave025")


@pytest.fixture(autouse=True)
def clean_redis() -> Iterator[None]:
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def _paths(rec: Recorder) -> list[str]:
    return [c["path"][0] for c in rec.data_calls()]


def test_each_component_has_its_own_run_key_from_its_domains() -> None:
    run = provider(Recorder()).current_run(NOW)
    stamp = f"{INIT:%Y%m%d%H}"
    assert dict(run.component_keys) == {
        "wave": f"gfswave016:{stamp}|gfswave025:{stamp}",
        "atmosphere": f"gfs013:{stamp}|gfs025:{stamp}",
    }
    # The run key is the two joined, wave first: the format stored runs already have.
    assert run.run_key == f"{run.component_keys['wave']}|{run.component_keys['atmosphere']}"
    assert {r.model: r.component for r in run.model_runs} == {
        "ncep_gfswave016": "wave",
        "ncep_gfswave025": "wave",
        "ncep_gfs013": "atmosphere",
        "ncep_gfs025": "atmosphere",
    }


def test_an_atmosphere_fetch_requests_only_the_forecast_api() -> None:
    rec = Recorder()
    p = provider(rec)
    result = p.fetch([PIPELINE, JBAY], p.current_run(NOW), frozenset({"atmosphere"}))
    assert set(_paths(rec)) == {"/v1/gfs"}  # no marine request
    assert len(_paths(rec)) == 1  # one batched request for both spots
    assert result.components == frozenset({"atmosphere"})
    assert set(result.records) == {1, 2} and not result.errors
    rows = result.records[1]
    assert rows and all(r.values.get("wind_speed_kmh") is not None for r in rows)
    wave_fields = COMPONENT_FIELDS["wave"]
    assert all(r.values.get(f) is None for r in rows for f in wave_fields)
    assert rows[0].extra["wind_cell"]["model"] == "gfs_global"  # type: ignore[index]


def test_a_wave_fetch_requests_only_the_marine_api() -> None:
    rec = Recorder()
    p = provider(rec)
    result = p.fetch([PIPELINE], p.current_run(NOW), frozenset({"wave"}))
    assert set(_paths(rec)) == {"/v1/marine"}
    assert result.components == frozenset({"wave"})
    rows = result.records[1]
    assert rows and all(r.values.get("sig_wave_height_m") is not None for r in rows)
    assert all(r.values.get(f) is None for r in rows for f in COMPONENT_FIELDS["atmosphere"])
    assert not result.errors  # no "incomplete: wind unavailable": wind was not asked for


def _wave_being_published(init: datetime, newer: datetime, available: datetime) -> dict[str, Any]:
    """Metadata in which only the wave domains have a newer run, published at
    ``available`` (SYNTHETIC times)."""
    meta = meta_at(init, init + timedelta(hours=5))
    for domain in WAVE_DOMAINS:
        meta[domain] = {
            **meta[domain],
            "last_run_initialisation_time": int(newer.timestamp()),
            "last_run_availability_time": int(available.timestamp()),
        }
    return meta


def test_servability_is_checked_per_component() -> None:
    """While Open-Meteo publishes a new wave run (inside the 10-minute wait), the stored
    wave run can no longer be read unmixed, but the atmosphere still can."""
    init = datetime(2026, 10, 14, 6, tzinfo=UTC)
    newer = init + timedelta(hours=6)
    available = newer + timedelta(hours=5, minutes=15)
    now = available + timedelta(minutes=5)
    rec = Recorder(meta=_wave_being_published(init, newer, available))
    p = OpenMeteoProvider(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: now,
    )
    run = p.current_run(now)
    assert run.wave_model_run_at == init  # the new wave run is not ready yet
    with pytest.raises(RunNotServedError):
        p.ensure_servable(run, now)
    p.ensure_servable(run, now, components=frozenset({"atmosphere"}))  # no error
    with pytest.raises(RunNotServedError):
        p.ensure_servable(run, now, components=frozenset({"wave"}))
    # The detection sees the same: the wave part waits, the atmosphere part is planned.
    checked = detect.servable_run(p)(now)
    assert isinstance(checked, detect.PartlyServedRun)
    assert set(checked.not_served) == {"wave"}
    assert checked.run_key == run.run_key


def test_a_new_run_of_the_other_component_during_a_fetch_does_not_void_it() -> None:
    """A component-only fetch compares only its own domains before and after the data
    requests: the waves moving on meanwhile leave an atmosphere fetch valid."""
    init = datetime(2026, 10, 14, tzinfo=UTC)
    now = init + timedelta(hours=7)
    meta = meta_at(init, init + timedelta(hours=5))
    rec = Recorder(meta=meta, override=synthetic_hourly(init, 48))
    p = OpenMeteoProvider(
        settings(),
        client=httpx.Client(transport=httpx.MockTransport(rec)),
        backoff_seconds=0,
        clock=lambda: now,
    )
    run = p.current_run(now)
    real_request = p._request

    def request_then_publish_waves(*args: Any, **kwargs: Any) -> Any:
        out = real_request(*args, **kwargs)
        for domain in WAVE_DOMAINS:  # a new wave run appears while the wind is fetched
            meta[domain] = {
                **meta[domain],
                "last_run_initialisation_time": int((init + timedelta(hours=6)).timestamp()),
            }
        return out

    p._request = request_then_publish_waves  # type: ignore[method-assign]
    result = p.fetch([PIPELINE], run, frozenset({"atmosphere"}))
    assert result.records[1] and not result.errors
    assert META  # the recorded metadata is untouched (copied by meta_at)
