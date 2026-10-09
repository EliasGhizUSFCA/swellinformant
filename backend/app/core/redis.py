"""Redis client plus small primitives: distributed locks and quota counters."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return _client


def reset_redis() -> None:
    global _client
    _client = None


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


@contextmanager
def redis_lock(name: str, ttl_seconds: int = 900, blocking_seconds: float = 0) -> Iterator[None]:
    """Distributed mutex (SET NX EX + token-checked release).

    The TTL guarantees that a crashed worker can never hold a lock forever, which is what
    makes jobs safe across worker restarts.
    """
    client = get_redis()
    key = f"lock:{name}"
    token = secrets.token_hex(16)
    deadline = time.monotonic() + blocking_seconds
    while True:
        if client.set(key, token, nx=True, ex=ttl_seconds):
            break
        if time.monotonic() >= deadline:
            raise LockNotAcquired(name)
        time.sleep(0.2)
    try:
        yield
    finally:
        try:
            client.eval(_RELEASE_SCRIPT, 1, key, token)
        except redis.RedisError:  # pragma: no cover - best effort
            logger.warning("Failed to release lock %s", name)


class QuotaExceeded(Exception):
    """Raised when a provider's daily request quota has been used up."""


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
