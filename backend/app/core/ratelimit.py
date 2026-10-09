"""Redis fixed-window rate limiting as a FastAPI dependency.

Fails open (with a warning) if Redis is unreachable, so an infrastructure hiccup does not
lock every user out; login brute force is additionally limited by the per-account
lockout stored in PostgreSQL.
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


def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For from TRUSTED_PROXY_COUNT reverse proxies."""
    trusted = get_settings().trusted_proxy_count
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and trusted > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - trusted)]
    return request.client.host if request.client else "unknown"


def hit(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    if not get_settings().rate_limit_enabled:
        return
    window = int(time.time() // window_seconds)
    redis_key = f"rl:{bucket}:{key}:{window}"
    try:
        client = get_redis()
        count = int(client.incr(redis_key))
        if count == 1:
            client.expire(redis_key, window_seconds + 5)
    except redis.RedisError:
        logger.warning("Rate limiter unavailable (Redis); allowing request")
        return
    if count > limit:
        retry = window_seconds - int(time.time()) % window_seconds
        raise RateLimited(retry_after=retry)


def rate_limit(bucket: str, limit: int, window_seconds: int) -> Callable[[Request], None]:
    """Dependency limiting requests per client IP."""

    def dependency(request: Request) -> None:
        hit(bucket, client_ip(request), limit, window_seconds)

    return dependency
