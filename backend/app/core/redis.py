"""Redis client plus small primitives: distributed locks, quota counters, weighted call
budgets and the memory figures the data-health report checks.

The clients connect to ``settings.redis_url`` (REDIS_URL, else KV_URL, the rediss:// URL
of Vercel's Upstash integration) with REDIS_CONNECT_TIMEOUT_SECONDS and
REDIS_SOCKET_TIMEOUT_SECONDS. A connection idle for :data:`HEALTH_CHECK_INTERVAL_SECONDS`
is pinged before use (a hosted Redis drops idle connections). :func:`retry_policy` is set
explicitly (redis-py's default depends on how a client is built) and retries a command
once, at once, after a connection error (a dropped idle connection or a refused connect,
both of which fail fast) and never after a timeout. An unreachable Redis therefore costs
at most one connect timeout per command: measured 2026-10-19 with redis-py 8.1, 1.50 s for
a blackholed address with a 1.5 s connect timeout and under 10 ms for a refused port.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL_SECONDS = 30

_client: redis.Redis | None = None
_bytes_client: redis.Redis | None = None


def retry_policy() -> Retry:
    """One immediate retry after a connection error; none after a timeout."""
    return Retry(NoBackoff(), 1, supported_errors=(redis.ConnectionError,))


def client_options(settings: Settings) -> dict[str, Any]:
    """Keyword arguments of ``redis.Redis.from_url`` (besides ``decode_responses``)."""
    return {
        "socket_timeout": settings.redis_socket_timeout_seconds,
        "socket_connect_timeout": settings.redis_connect_timeout_seconds,
        "health_check_interval": HEALTH_CHECK_INTERVAL_SECONDS,
        "retry": retry_policy(),
    }


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, **client_options(settings)
        )
    return _client


def get_redis_bytes() -> redis.Redis:
    """The same Redis, returning raw bytes (for binary values such as cached bodies)."""
    global _bytes_client
    if _bytes_client is None:
        settings = get_settings()
        _bytes_client = redis.Redis.from_url(
            settings.redis_url, decode_responses=False, **client_options(settings)
        )
    return _bytes_client


def reset_redis() -> None:
    global _client, _bytes_client
    _client = None
    _bytes_client = None


def check_redis() -> bool:
    return bool(get_redis().ping())


_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class LockNotAcquired(Exception):
    """Raised when another worker already holds a job lock."""


def acquire_lock(name: str, ttl_seconds: int = 900, blocking_seconds: float = 0) -> str:
    """Take the lock ``name`` (SET NX EX); returns the token :func:`release_lock` needs.

    Raises LockNotAcquired when another holder keeps it beyond ``blocking_seconds``, and
    redis.RedisError when Redis does not answer (after the client's connect or socket
    timeout and its retry policy, :func:`client_options`).
    """
    client = get_redis()
    key = f"lock:{name}"
    token = secrets.token_hex(16)
    deadline = time.monotonic() + blocking_seconds
    while True:
        if client.set(key, token, nx=True, ex=ttl_seconds):
            return token
        if time.monotonic() >= deadline:
            raise LockNotAcquired(name)
        time.sleep(0.2)


def release_lock(name: str, token: str) -> None:
    """Release the lock ``name`` if ``token`` still holds it (best effort)."""
    try:
        get_redis().eval(_RELEASE_SCRIPT, 1, f"lock:{name}", token)
    except redis.RedisError:
        logger.warning("Failed to release lock %s", name)


@contextmanager
def redis_lock(name: str, ttl_seconds: int = 900, blocking_seconds: float = 0) -> Iterator[None]:
    """Distributed mutex (SET NX EX + token-checked release).

    The TTL guarantees that a crashed worker can never hold a lock forever, which is what
    makes jobs safe across worker restarts.
    """
    token = acquire_lock(name, ttl_seconds, blocking_seconds)
    try:
        yield
    finally:
        release_lock(name, token)


class QuotaExceeded(Exception):
    """Raised when a provider's daily request quota has been used up.

    services.http.request_with_retries re-raises it as ``QuotaExhaustedError``, which is
    both a ``QuotaExceeded`` and a ``PermanentProviderError``.
    """


def consume_quota(provider: str, limit: int, amount: int = 1) -> int:
    """Increment today's (UTC) counter for ``provider`` and raise if over ``limit``.

    Returns the new usage value. Quotas protect paid API budgets: once the daily limit is
    reached, jobs stop calling the provider until the next UTC day.
    """
    client = get_redis()
    day = datetime.now(UTC).strftime("%Y%m%d")
    key = f"quota:{provider}:{day}"
    used = int(client.incrby(key, amount))
    if used == amount:
        client.expire(key, 60 * 60 * 48)
    if used > limit:
        client.decrby(key, amount)
        raise QuotaExceeded(f"{provider} daily quota of {limit} requests reached")
    return used


def quota_usage(provider: str) -> int:
    day = datetime.now(UTC).strftime("%Y%m%d")
    value = get_redis().get(f"quota:{provider}:{day}")
    return int(value) if value else 0


# KEYS = one counter per window; ARGV = amount, then (limit, ttl seconds) per key. Nothing is
# added unless every window has room, so a refused call never uses any budget. Returns 0,
# or the 1-based index of the first full window.
_BUDGET_SCRIPT = """
local amount = tonumber(ARGV[1])
for i = 1, #KEYS do
    local used = tonumber(redis.call('get', KEYS[i]) or '0')
    if used + amount > tonumber(ARGV[2 * i]) + 1e-9 then
        return i
    end
end
for i = 1, #KEYS do
    redis.call('incrbyfloat', KEYS[i], ARGV[1])
    if redis.call('ttl', KEYS[i]) < 0 then
        redis.call('expire', KEYS[i], tonumber(ARGV[2 * i + 1]))
    end
end
return 0
"""


def reserve_budget(windows: Sequence[tuple[str, float, int]], amount: float) -> int:
    """Add ``amount`` to every ``(key, limit, ttl_seconds)`` counter, or to none.

    Returns 0 when the amount fits every window, otherwise the 1-based index of the first
    window that is full (nothing is added then). Counters are floats (weighted calls).
    """
    if not windows:
        return 0
    keys = [key for key, _, _ in windows]
    args: list[Any] = [repr(float(amount))]
    for _, limit, ttl in windows:
        args += [repr(float(limit)), int(ttl)]
    return int(get_redis().eval(_BUDGET_SCRIPT, len(keys), *keys, *args))


def budget_usage(keys: Sequence[str]) -> list[float]:
    values = get_redis().mget(list(keys)) if keys else []
    return [float(v) if v else 0.0 for v in values]


_MEMORY_UNITS = {"": 1, "b": 1, "k": 1000, "kb": 1024, "m": 1000**2, "mb": 1024**2}
_MEMORY_UNITS |= {"g": 1000**3, "gb": 1024**3}


def parse_memory_size(text: str | None) -> int | None:
    """Bytes of a redis.conf memory size ("256mb", "1gb", "100k"); None if unreadable.

    Redis's units: k/m/g are powers of 1000, kb/mb/gb powers of 1024 (redis.conf).
    """
    value = (text or "").strip().lower()
    number = value.rstrip("kmgb")
    unit = value[len(number) :]
    if not number.isdigit() or unit not in _MEMORY_UNITS:
        return None
    return int(number) * _MEMORY_UNITS[unit]


def redis_memory() -> dict[str, Any]:
    """INFO memory figures the health check uses: used_memory, maxmemory (0 = no cap)
    and maxmemory_policy."""
    info = get_redis().info("memory")
    return {
        "used_memory": int(info.get("used_memory", 0)),
        "maxmemory": int(info.get("maxmemory", 0)),
        "maxmemory_policy": str(info.get("maxmemory_policy", "")),
    }
