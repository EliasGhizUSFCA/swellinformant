"""Bounded Redis cache of provider HTTP response bodies (point-forecast APIs).

Why: a run whose second request fails (a 429 or a timeout) is retried within minutes by
the Celery task. The responses already received belong to the same model run, so the
retry reads them from here instead of downloading and paying for them again.

What a key means: one request of one provider for one model run, coordinate set and
variable list (:class:`ProviderCacheKey`). The model run is part of the key, so a new
run never reads an old run's body. Entries expire after the model's update interval
(Open-Meteo ``update_interval_seconds``, 6 h for GFS and ECMWF).

Bounds (the cache shares Redis with the Celery broker, which runs with maxmemory and
``noeviction``: a full Redis refuses broker writes, so the cache must stay small):

* bodies larger than PROVIDER_RESPONSE_CACHE_MAX_BYTES (raw, before compression) are
  never cached;
* at most PROVIDER_RESPONSE_CACHE_MAX_KEYS entries: an index (sorted set by expiry) drops
  the entries closest to expiry first when the cap is reached;
* bodies are stored zlib-compressed (JSON shrinks about 5-10 times).

PROVIDER_RESPONSE_CACHE_ENABLED=false turns it off. Redis errors never fail a fetch: the
body is fetched from the provider as if the cache were empty.
"""

from __future__ import annotations

import hashlib
import logging
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import redis

from app.core.config import Settings, get_settings
from app.core.redis import get_redis_bytes

logger = logging.getLogger(__name__)

KEY_PREFIX = "provcache:"
INDEX_KEY = "provcache:index"


@dataclass(frozen=True)
class ProviderCacheKey:
    provider: str
    model: str
    run_init_time: datetime | None
    coords_hash: str
    variables: str

    def redis_key(self) -> str:
        init = (
            "none"
            if self.run_init_time is None
            else f"{self.run_init_time.astimezone(UTC):%Y%m%d%H%M}"
        )
        digest = hashlib.sha256(
            f"{self.provider}|{self.model}|{init}|{self.coords_hash}|{self.variables}".encode()
        ).hexdigest()[:32]
        return f"{KEY_PREFIX}{self.provider}:{self.model}:{init}:{digest}"


def coords_hash(latitudes: Sequence[float], longitudes: Sequence[float], *extra: str) -> str:
    """Stable digest of a coordinate list (4 decimals, as sent) plus request options."""
    text = ",".join(f"{a:.4f}:{b:.4f}" for a, b in zip(latitudes, longitudes, strict=True))
    return hashlib.sha256("|".join((text, *extra)).encode()).hexdigest()[:32]


def _enabled(settings: Settings) -> bool:
    return (
        settings.provider_response_cache_enabled
        and settings.provider_response_cache_max_keys > 0
        and settings.provider_response_cache_max_bytes > 0
    )


def get(key: ProviderCacheKey, settings: Settings | None = None) -> bytes | None:
    """The cached body, or None (miss, disabled, or Redis unavailable)."""
    s = settings or get_settings()
    if not _enabled(s):
        return None
    try:
        raw = get_redis_bytes().get(key.redis_key())
    except redis.RedisError as exc:
        logger.warning("Provider cache unavailable: %s", exc)
        return None
    if raw is None:
        return None
    try:
        return zlib.decompress(bytes(raw))  # type: ignore[arg-type]
    except zlib.error:
        return None


def put(key: ProviderCacheKey, body: bytes, ttl_s: int, settings: Settings | None = None) -> bool:
    """Store ``body`` for ``ttl_s`` seconds; False when refused (too large, off, Redis down)."""
    s = settings or get_settings()
    if not _enabled(s) or ttl_s <= 0 or len(body) > s.provider_response_cache_max_bytes:
        return False
    rkey = key.redis_key()
    now = time.time()
    try:
        client = get_redis_bytes()
        client.zremrangebyscore(INDEX_KEY, "-inf", now)  # entries that have expired
        excess = int(client.zcard(INDEX_KEY)) - s.provider_response_cache_max_keys + 1
        if excess > 0 and client.zscore(INDEX_KEY, rkey) is None:
            popped: list[tuple[bytes, float]] = client.zpopmin(INDEX_KEY, excess)  # type: ignore[assignment]
            victims = [name for name, _ in popped]
            if victims:
                client.delete(*victims)
        pipe = client.pipeline()
        pipe.set(rkey, zlib.compress(body, 6), ex=int(ttl_s))
        pipe.zadd(INDEX_KEY, {rkey: now + ttl_s})
        pipe.execute()
    except redis.RedisError as exc:
        logger.warning("Provider cache unavailable: %s", exc)
        return False
    return True


def get_or_fetch(
    key: ProviderCacheKey,
    fetch: Callable[[], bytes],
    ttl_s: int,
    settings: Settings | None = None,
) -> bytes:
    """The cached body for ``key``, or ``fetch()``'s body (then cached when allowed).

    ``fetch`` raises for a failed request, so errors are never cached.
    """
    cached = get(key, settings)
    if cached is not None:
        return cached
    body = fetch()
    put(key, body, ttl_s, settings)
    return body


def cache_size(settings: Settings | None = None) -> int:
    """Number of live entries (for tests and the operations docs)."""
    try:
        client = get_redis_bytes()
        client.zremrangebyscore(INDEX_KEY, "-inf", time.time())
        return int(client.zcard(INDEX_KEY))
    except redis.RedisError:
        return 0
