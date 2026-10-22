"""app.core.ratelimit: the client IP behind the platform's own header, and failing open
quickly when Redis is unreachable.

On Vercel, ``x-vercel-forwarded-for`` carries the client IP and cannot be overwritten by a
proxy in front of Vercel, and Vercel overwrites X-Forwarded-For (vercel.com/docs/headers/
request-headers, read 2026-10-18). With CLIENT_IP_HEADER set and present, nothing a client
puts in X-Forwarded-For changes the rate-limit key. Owned by the api-serverless package.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest
import redis
from starlette.requests import Request

from app.core import ratelimit
from app.core.config import Settings, get_settings
from app.core.errors import RateLimited
from app.core.ratelimit import MAX_CLIENT_IP_LENGTH, client_ip
from app.core.redis import get_redis, reset_redis

PEER = "10.0.0.9"


def request(**headers: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/login",
        "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()],
        "client": (PEER, 52000),
    }
    return Request(scope)


@pytest.fixture
def vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "client_ip_header", "x-vercel-forwarded-for")


def test_the_platform_header_wins(vercel: None) -> None:
    assert client_ip(request(x_vercel_forwarded_for="203.0.113.7")) == "203.0.113.7"
    # Its first entry is the client; whitespace is ignored.
    assert client_ip(request(x_vercel_forwarded_for=" 203.0.113.7 , 10.1.1.1")) == "203.0.113.7"
    assert client_ip(request(x_vercel_forwarded_for="2001:db8::1")) == "2001:db8::1"


def test_a_spoofed_x_forwarded_for_is_ignored_when_the_header_is_present(
    vercel: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for trusted in (0, 1, 2):
        monkeypatch.setattr(get_settings(), "trusted_proxy_count", trusted)
        spoofed = request(
            x_vercel_forwarded_for="203.0.113.7",
            x_forwarded_for="198.51.100.66, 198.51.100.67",
        )
        assert client_ip(spoofed) == "203.0.113.7", trusted


def test_without_the_header_the_old_rules_apply(vercel: None) -> None:
    """Header configured but absent (e.g. a direct request to a local container): the
    TRUSTED_PROXY_COUNT logic, unchanged."""
    assert get_settings().trusted_proxy_count == 1
    assert client_ip(request(x_forwarded_for="198.51.100.1, 203.0.113.7")) == "203.0.113.7"
    assert client_ip(request()) == PEER
    assert client_ip(request(x_vercel_forwarded_for=" ,")) == PEER  # empty first entry


def test_unset_header_keeps_today_s_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.client_ip_header is None
    headers = {"x_forwarded_for": "198.51.100.1, 203.0.113.7", "x_vercel_forwarded_for": "1.2.3.4"}
    assert client_ip(request(**headers)) == "203.0.113.7"  # the platform header is not read
    monkeypatch.setattr(settings, "trusted_proxy_count", 2)
    assert client_ip(request(**headers)) == "198.51.100.1"
    monkeypatch.setattr(settings, "trusted_proxy_count", 0)
    assert client_ip(request(**headers)) == PEER


def test_overlong_values_are_cut(vercel: None) -> None:
    value = client_ip(request(x_vercel_forwarded_for="x" * 500))
    assert value == "x" * MAX_CLIENT_IP_LENGTH


def test_the_setting_is_lower_cased_and_validated() -> None:
    assert Settings(client_ip_header="X-Vercel-Forwarded-For").client_ip_header == (
        "x-vercel-forwarded-for"
    )
    with pytest.raises(ValueError):
        Settings(client_ip_header="x-forwarded-for; drop")


# --------------------------------------------------------------------------- fail open
class CountingRedis:
    """Stands in for the Redis client: every command fails like an unreachable server."""

    def __init__(self) -> None:
        self.commands = 0

    def incr(self, key: str) -> int:
        self.commands += 1
        raise redis.TimeoutError("synthetic: Timeout connecting to server")


@pytest.fixture
def limits_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(ratelimit, "_fail_open", None)
    yield


def test_an_unreachable_redis_is_asked_once_per_fail_open_window(
    limits_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = CountingRedis()
    monkeypatch.setattr(ratelimit, "get_redis", lambda: down)
    clock = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock[0])
    for _ in range(5):  # every request is allowed; only the first one waits for Redis
        ratelimit.hit("login", "203.0.113.7", 1, 300)
    assert down.commands == 1
    clock[0] += ratelimit.FAIL_OPEN_SECONDS + 0.1  # the window is over: Redis is asked again
    ratelimit.hit("login", "203.0.113.7", 1, 300)
    assert down.commands == 2


def test_a_new_client_is_asked_at_once_and_limits_apply_again(
    limits_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After reset_redis() (a new client, e.g. Redis back or reconfigured) the limiter
    stops failing open: the test Redis counts again and the limit holds."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = f"redis://127.0.0.1:{sock.getsockname()[1]}/0"
    settings = get_settings()
    healthy = settings.redis_url
    try:
        monkeypatch.setattr(settings, "redis_url", closed)
        reset_redis()
        started = time.monotonic()
        for _ in range(3):
            ratelimit.hit("fail-open-test", "203.0.113.8", 1, 300)  # refused: allowed
        assert time.monotonic() - started < 1
        monkeypatch.setattr(settings, "redis_url", healthy)
        reset_redis()
        try:
            get_redis().ping()
        except redis.RedisError:
            pytest.skip("Redis not available")
        get_redis().delete(*get_redis().keys("rl:fail-open-test:*") or ["-"])
        ratelimit.hit("fail-open-test", "203.0.113.8", 1, 300)
        with pytest.raises(RateLimited):
            ratelimit.hit("fail-open-test", "203.0.113.8", 1, 300)
    finally:
        keys = get_redis().keys("rl:fail-open-test:*")
        if keys:
            get_redis().delete(*keys)
        reset_redis()


# --------------------------------------------------------------------------- stored keys
def test_counters_never_store_the_address_in_clear_text(limits_on: None) -> None:
    """The Redis key of a counter holds a keyed digest of the IP or email address, never
    the address (on Vercel the Redis host is a third party, Upstash); each address still
    has its own counter. The addresses are synthetic."""
    client = get_redis()
    try:
        client.ping()
    except redis.RedisError:
        pytest.skip("Redis not available")

    def stored() -> list[str]:
        return [str(k) for k in client.keys("rl:digest-test:*")]

    client.delete(*stored() or ["-"])
    try:
        for _ in range(2):
            ratelimit.hit("digest-test", "Surfer@Example.com", 2, 300)
        with pytest.raises(RateLimited):
            ratelimit.hit("digest-test", "Surfer@Example.com", 2, 300)
        ratelimit.hit("digest-test", "203.0.113.9", 2, 300)  # another address: its own count
        keys = stored()
        assert len(keys) == 2
        assert not any("example" in k.lower() or "203.0.113" in k for k in keys), keys
        digest = ratelimit.limit_key("digest-test", "Surfer@Example.com")
        assert len(digest) == 32 and any(f":{digest}:" in k for k in keys)
        assert digest != ratelimit.limit_key("other-bucket", "Surfer@Example.com")
    finally:
        client.delete(*stored() or ["-"])
