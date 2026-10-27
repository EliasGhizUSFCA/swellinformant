"""Forecast API counters (forecasts.quota): Windy's daily request counter
(WINDY_DAILY_REQUEST_LIMIT, reset 00:00 UTC) and the usage snapshot the admin dashboard reads
(Open-Meteo weighted calls per window and Windy requests per day). Uses the test Redis."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import redis

from app.core.config import Settings
from app.core.redis import get_redis
from app.services.forecasts import quota

NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_redis() -> Iterator[None]:
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def test_the_usage_snapshot_reads_open_meteo_and_windy_counters() -> None:
    s = make(windy_daily_request_limit=500)
    quota.consume_open_meteo_budget(2.5, now=NOW, settings=s)
    for _ in range(3):
        quota.consume_windy_request(now=NOW, settings=s)
    snap = quota.usage_snapshot(NOW, s)
    assert snap["open_meteo"]["day"] == 2.5 and snap["open_meteo"]["minute"] == 2.5
    assert snap["open_meteo"]["limits"]["day"] == s.open_meteo_calls_per_day
    assert snap["open_meteo"]["available"] is True
    assert snap["windy"] == {
        "day": 3,
        "limit": 500,
        "resets_at": "2026-10-28T00:00:00+00:00",
        "available": True,
    }
    tomorrow = quota.usage_snapshot(NOW + timedelta(days=1), s)
    assert tomorrow["windy"]["day"] == 0  # the counter resets at 00:00 UTC


def test_the_usage_snapshot_says_when_redis_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(keys: object) -> list[float]:
        raise redis.ConnectionError("down")

    monkeypatch.setattr(quota, "budget_usage", down)
    snap = quota.usage_snapshot(NOW, make())
    assert snap["open_meteo"]["available"] is False and snap["open_meteo"]["day"] is None
    assert snap["windy"]["day"] is None and snap["windy"]["available"] is False


def test_the_windy_counter_refuses_at_the_limit_until_midnight() -> None:
    s = make(windy_daily_request_limit=2)
    quota.consume_windy_request(now=NOW, settings=s)
    quota.consume_windy_request(now=NOW, settings=s)
    with pytest.raises(quota.QuotaWindowExhaustedError) as err:
        quota.consume_windy_request(now=NOW, settings=s)
    assert err.value.retry_after_s == quota.seconds_until_next_utc_day(NOW)
    usage = quota.windy_request_usage(NOW, s)
    assert usage is not None and usage["used"] == 2  # the refused request is not counted
    quota.consume_windy_request(now=NOW + timedelta(days=1), settings=s)  # a new UTC day
