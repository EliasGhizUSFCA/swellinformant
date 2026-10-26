"""Bounded provider response cache (forecasts.provider_cache) on the test Redis.

Skipped when Redis is unreachable. Bodies here are synthetic bytes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
import redis

from app.core.config import Settings
from app.core.redis import get_redis_bytes
from app.services.forecasts import provider_cache
from app.services.forecasts.provider_cache import ProviderCacheKey, coords_hash

INIT = datetime(2026, 10, 14, 6, tzinfo=UTC)


@pytest.fixture
def clean_redis() -> Iterator[redis.Redis]:
    client = get_redis_bytes()
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis not available")
    client.flushdb()
    yield client
    client.flushdb()


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def key(n: int = 0, init: datetime = INIT) -> ProviderCacheKey:
    return ProviderCacheKey(
        provider="open_meteo_noaa",
        model="ncep_gfswave016",
        run_init_time=init,
        coords_hash=coords_hash([21.8 + n], [-158.18], "sea"),
        variables="wave_height,wave_period",
    )


def get_or_fetch(
    key: ProviderCacheKey, fetch: Callable[[], bytes], ttl_s: int, settings: Settings
) -> bytes:
    """Read through the cache as the provider does: a miss fetches, then stores the body
    (``fetch`` raises for a failed request, so errors are never cached)."""
    cached = provider_cache.get(key, settings)
    if cached is not None:
        return cached
    body = fetch()
    provider_cache.put(key, body, ttl_s, settings)
    return body


class Counter:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    def __call__(self) -> bytes:
        self.calls += 1
        return self.body


def test_second_identical_fetch_makes_no_request(clean_redis: redis.Redis) -> None:
    s = settings()
    fetch = Counter(b'{"hourly": {"time": [1]}}' * 100)
    first = get_or_fetch(key(), fetch, 3600, s)
    second = get_or_fetch(key(), fetch, 3600, s)
    assert first == second == fetch.body
    assert fetch.calls == 1
    # Stored compressed, with the model update interval as TTL.
    stored = clean_redis.get(key().redis_key())
    assert stored is not None and len(stored) < len(fetch.body)
    assert 0 < clean_redis.ttl(key().redis_key()) <= 3600


def test_another_run_or_point_is_another_entry(clean_redis: redis.Redis) -> None:
    s = settings()
    fetch = Counter(b"body")
    get_or_fetch(key(), fetch, 3600, s)
    get_or_fetch(key(init=datetime(2026, 10, 14, 12, tzinfo=UTC)), fetch, 3600, s)
    get_or_fetch(key(1), fetch, 3600, s)
    assert fetch.calls == 3


def test_a_3_mb_body_is_refused(clean_redis: redis.Redis) -> None:
    s = settings()
    fetch = Counter(os.urandom(3_000_000))
    assert get_or_fetch(key(), fetch, 3600, s) == fetch.body
    assert get_or_fetch(key(), fetch, 3600, s) == fetch.body
    assert fetch.calls == 2  # never cached
    assert clean_redis.get(key().redis_key()) is None
    assert provider_cache.put(key(), b"x" * 2_000_000, 3600, s)  # at the limit: allowed


def test_the_key_cap_is_enforced(clean_redis: redis.Redis) -> None:
    s = settings(provider_response_cache_max_keys=3)
    for n in range(5):
        assert provider_cache.put(key(n), f"body {n}".encode(), 3600 + n, s)
    assert provider_cache.cache_size() == 3
    assert len(clean_redis.keys("provcache:open_meteo_noaa:*")) == 3
    # The entries closest to expiry (the first ones written here) went first.
    assert provider_cache.get(key(0), s) is None and provider_cache.get(key(1), s) is None
    assert provider_cache.get(key(4), s) == b"body 4"


def test_disabled_cache_always_fetches(clean_redis: redis.Redis) -> None:
    s = settings(provider_response_cache_enabled=False)
    fetch = Counter(b"body")
    get_or_fetch(key(), fetch, 3600, s)
    get_or_fetch(key(), fetch, 3600, s)
    assert fetch.calls == 2 and clean_redis.dbsize() == 0


def test_redis_outage_falls_back_to_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def __getattr__(self, name: str) -> object:
            def fail(*_: object, **__: object) -> None:
                raise redis.ConnectionError("down")

            return fail

    monkeypatch.setattr(provider_cache, "get_redis_bytes", lambda: Down())
    fetch = Counter(b"body")
    assert get_or_fetch(key(), fetch, 3600, settings()) == b"body"
    assert provider_cache.put(key(), b"body", 3600, settings()) is False
