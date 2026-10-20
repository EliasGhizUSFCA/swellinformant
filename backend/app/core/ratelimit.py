"""Redis fixed-window rate limiting as a FastAPI dependency.

Fails open (with a warning) if Redis is unreachable, so an infrastructure hiccup does not
lock every user out; login brute force is additionally limited by the per-account
lockout stored in PostgreSQL. After a Redis error the limiter stays open for
:data:`FAIL_OPEN_SECONDS` without asking Redis, so an unreachable Redis (each command
waits up to REDIS_CONNECT_TIMEOUT_SECONDS) slows about one request per process every
FAIL_OPEN_SECONDS instead of every request (twice for a request with two limits).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import redis
from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

FAIL_OPEN_SECONDS = 30.0
# (the Redis client that failed, monotonic time until which the limiter skips it). A new
# client (app.core.redis.reset_redis) is asked again at once.
_fail_open: tuple[redis.Redis, float] | None = None


def _skip_redis(client: redis.Redis) -> bool:
    return _fail_open is not None and _fail_open[0] is client and time.monotonic() < _fail_open[1]


# Longest value used as a rate-limit key (an IPv6 address with a zone index is shorter).
MAX_CLIENT_IP_LENGTH = 64


def client_ip(request: Request) -> str:
    """The client's IP address, the key of the per-client rate limits.

    CLIENT_IP_HEADER set (e.g. ``x-vercel-forwarded-for``, which the platform sets and a
    client cannot overwrite) and present: its first comma-separated entry, and every
    X-Forwarded-For value is ignored. Otherwise X-Forwarded-For from TRUSTED_PROXY_COUNT
    reverse proxies, else the peer address.
    """
    settings = get_settings()
    header = settings.client_ip_header
    if header:
        first = (request.headers.get(header) or "").split(",", 1)[0].strip()
        if first:
            return first[:MAX_CLIENT_IP_LENGTH]
    trusted = settings.trusted_proxy_count
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and trusted > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - trusted)]
    return request.client.host if request.client else "unknown"


def hit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    global _fail_open
    if not get_settings().rate_limit_enabled:
        return
    window = int(time.time() // window_seconds)
    redis_key = f"rl:{bucket}:{key}:{window}"
    client = get_redis()
    if _skip_redis(client):
        return
    try:
        count = int(client.incr(redis_key))
        if count == 1:
            client.expire(redis_key, window_seconds + 5)
    except redis.RedisError as exc:
        _fail_open = (client, time.monotonic() + FAIL_OPEN_SECONDS)
        logger.warning(
            "Rate limiter unavailable (Redis: %s); allowing requests for %.0f s",
            type(exc).__name__,
            FAIL_OPEN_SECONDS,
        )
        return
    if count > limit:
        retry = window_seconds - int(time.time()) % window_seconds
        raise RateLimited(retry_after=retry)


def rate_limit(bucket: str, limit: int, window_seconds: int) -> Callable[[Request], None]:
    """Dependency limiting requests per client IP."""

    def dependency(request: Request) -> None:
        hit(bucket, client_ip(request), limit, window_seconds)

    return dependency
