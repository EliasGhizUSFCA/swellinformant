"""Open-Meteo weighted call budget (forecasts.quota) and quota error types (services.http).

Budget tests use the test Redis (REDIS_URL from tests/conftest.py) and are skipped when it
is unreachable; the weighting tests are pure arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis

from app.core.config import Settings
from app.core.redis import QuotaExceeded, get_redis
from app.services.forecasts.quota import (
    BudgetWindowFullError,
    budget_windows,
    call_weight,
    consume_open_meteo_budget,
    open_meteo_budget_usage,
)
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    QuotaExhaustedError,
    TransientProviderError,
    request_with_retries,
)

NOW = datetime(2026, 10, 14, 13, 20, 30, tzinfo=UTC)


@pytest.fixture
def clean_redis() -> Iterator[redis.Redis]:
    client = get_redis()
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis not available")
    client.flushdb()
    yield client
    client.flushdb()


def budget(**limits: int) -> Settings:
    return Settings(_env_file=None, **limits)  # type: ignore[arg-type,call-arg]


# --------------------------------------------------------------------------- weights
def test_weight_follows_the_documented_rule() -> None:
    # 50 spots, 15 wave variables, 16 days: 50 x 1.5 x (16/14)
    assert call_weight(50, 15, 16) == pytest.approx(85.714, abs=1e-3)
    assert call_weight(10, 17, 16) == pytest.approx(10 * 1.7 * 16 / 14)
    # At most 10 variables and 14 days per location count as one call.
    assert call_weight(1, 4, 7) == 1.0
    assert call_weight(1, 10, 14) == 1.0
    assert call_weight(3, 4, 2, models=4) == 12.0
    with pytest.raises(ValueError):
        call_weight(0, 4, 7)


def test_a_full_api_first_cycle_fits_the_free_tier() -> None:
    """Both families for 50 spots: GFS waves (15 vars, 16 days, 2 models routed), GFS wind
    (4 vars), ECMWF waves and wind (4 vars, 15 days)."""
    gfs = call_weight(50, 15, 16) + call_weight(50, 4, 16)
    ecmwf = call_weight(50, 4, 15) + call_weight(50, 4, 15)
    per_cycle = gfs + ecmwf
    assert per_cycle == pytest.approx(250.0, abs=0.5)
    assert per_cycle * 4 < 10_000 and per_cycle * 4 * 31 < 300_000


# --------------------------------------------------------------------------- windows
def test_windows_are_utc_minute_hour_day_and_month() -> None:
    windows = {w.name: w for w in budget_windows(NOW, budget())}
    assert windows["minute"].key.endswith(":202610141320")
    assert windows["minute"].resets_at == datetime(2026, 10, 14, 13, 21, tzinfo=UTC)
    assert windows["hour"].resets_at == datetime(2026, 10, 14, 14, tzinfo=UTC)
    assert windows["day"].resets_at == datetime(2026, 10, 15, tzinfo=UTC)
    assert windows["month"].resets_at == datetime(2026, 11, 1, tzinfo=UTC)
    december = {w.name: w for w in budget_windows(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))}
    assert december["month"].resets_at == datetime(2027, 1, 1, tzinfo=UTC)
    assert [w.limit for w in budget_windows(NOW, budget())] == [600, 5000, 10000, 300000]


def test_budget_counts_weighted_calls(clean_redis: redis.Redis) -> None:
    s = budget()
    consume_open_meteo_budget(85.7, now=NOW, settings=s)
    consume_open_meteo_budget(57.1, now=NOW, settings=s)
    usage = open_meteo_budget_usage(NOW, s)
    assert usage["minute"]["used"] == pytest.approx(142.8)
    assert usage["day"] == {"used": pytest.approx(142.8), "limit": 10000}


def test_minute_overrun_is_transient_and_the_window_resets(clean_redis: redis.Redis) -> None:
    s = budget(open_meteo_calls_per_minute=100)
    consume_open_meteo_budget(85.7, now=NOW, settings=s)
    with pytest.raises(BudgetWindowFullError) as refused:
        consume_open_meteo_budget(57.1, now=NOW, settings=s)
    assert isinstance(refused.value, TransientProviderError)
    assert refused.value.retry_after_s == pytest.approx(30.0)  # 13:20:30 -> 13:21
    # A refused call reserves nothing in any window.
    assert open_meteo_budget_usage(NOW, s)["hour"]["used"] == pytest.approx(85.7)
    consume_open_meteo_budget(57.1, now=NOW + timedelta(seconds=40), settings=s)  # new minute
    assert open_meteo_budget_usage(NOW, s)["hour"]["used"] == pytest.approx(142.8)


def test_day_overrun_is_permanent_quota_exhaustion(clean_redis: redis.Redis) -> None:
    s = budget(open_meteo_calls_per_day=100)
    consume_open_meteo_budget(99.0, now=NOW, settings=s)
    with pytest.raises(QuotaExhaustedError, match="day budget of 100") as refused:
        consume_open_meteo_budget(2.0, now=NOW + timedelta(hours=2), settings=s)
    assert isinstance(refused.value, PermanentProviderError)
    assert isinstance(refused.value, QuotaExceeded)  # flight discovery still stops on it


def test_budget_overrun_sends_no_http_request(clean_redis: redis.Redis) -> None:
    calls: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )
    s = budget(open_meteo_calls_per_minute=1)
    with pytest.raises(BudgetWindowFullError):
        request_with_retries(
            client,
            "GET",
            "https://marine-api.open-meteo.test/v1/marine",
            provider="open_meteo",
            backoff_seconds=0,
            before_attempt=lambda: consume_open_meteo_budget(1.7, settings=s),
        )
    assert calls == []


def test_quota_exceeded_becomes_a_provider_error(clean_redis: redis.Redis) -> None:
    calls: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    )
    kwargs = {"provider": "unit-test", "quota": 1, "backoff_seconds": 0.0}
    assert request_with_retries(client, "GET", "https://x.test/", **kwargs).status_code == 200  # type: ignore[arg-type]
    with pytest.raises(QuotaExhaustedError) as exhausted:
        request_with_retries(client, "GET", "https://x.test/", **kwargs)  # type: ignore[arg-type]
    assert isinstance(exhausted.value, ProviderError) and not exhausted.value.transient
    assert len(calls) == 1


def test_redis_outage_does_not_block_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_: object, **__: object) -> int:
        raise redis.ConnectionError("down")

    monkeypatch.setattr("app.services.forecasts.quota.reserve_budget", broken)
    consume_open_meteo_budget(1000.0, now=NOW, settings=budget(open_meteo_calls_per_minute=1))
