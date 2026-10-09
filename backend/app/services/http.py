"""Shared HTTP helper for external providers: retries, backoff and daily quotas."""

from __future__ import annotations

import contextlib
import logging
import random
import time
from typing import Any

import httpx
import redis

from app.core.redis import consume_quota

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ProviderError(Exception):
    """Base class for external provider failures."""

    transient: bool = False


class TransientProviderError(ProviderError):
    """Temporary failure (timeout, 429, 5xx). Safe to retry later."""

    transient = True


class PermanentProviderError(ProviderError):
    """Will not succeed on retry (bad credentials, invalid request, unsupported route)."""


def _sleep_backoff(attempt: int, base: float, retry_after: str | None) -> None:
    if base <= 0:
        return
    delay = base * (2**attempt) + random.uniform(0, base)
    if retry_after:
        with contextlib.suppress(ValueError):
            delay = max(delay, min(float(retry_after), 30.0))
    time.sleep(min(delay, 30.0))


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    quota: int | None = None,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transient failures with exponential backoff + jitter.

    Each attempt counts against the provider's daily quota when ``quota`` is given.
    Returns the final response for non-retryable statuses (callers map 4xx themselves).
    Raises :class:`TransientProviderError` if every attempt failed transiently.
    """
    last_error: str = "no attempt made"
    for attempt in range(attempts):
        if quota is not None:
            try:
                consume_quota(provider, quota)
            except redis.RedisError:
                logger.warning("Quota counter unavailable for %s; continuing", provider)
        try:
            response = client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "%s request failed (attempt %d/%d): %s", provider, attempt + 1, attempts, last_error
            )
            if attempt + 1 < attempts:
                _sleep_backoff(attempt, backoff_seconds, None)
            continue
        if response.status_code in RETRYABLE_STATUS:
            last_error = f"HTTP {response.status_code}"
            logger.warning(
                "%s returned %s (attempt %d/%d)",
                provider,
                response.status_code,
                attempt + 1,
                attempts,
            )
            if attempt + 1 < attempts:
                _sleep_backoff(attempt, backoff_seconds, response.headers.get("Retry-After"))
            continue
        return response
    raise TransientProviderError(f"{provider}: {last_error} after {attempts} attempts")
