"""Shared HTTP helper for external providers: retries, backoff, quotas and byte accounting."""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis

from app.core.redis import QuotaExceeded, consume_quota

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class ProviderError(Exception):
    """Base class for external provider failures."""

    transient: bool = False


class TransientProviderError(ProviderError):
    """Temporary failure (timeout, 429, 5xx). Safe to retry later.

    ``retry_after_s``: how long the caller should wait before retrying, when the provider
    or the failing check says so (e.g. a call budget window that resets in 40 s).
    """

    transient = True

    def __init__(self, *args: object, retry_after_s: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after_s = retry_after_s


class RunNotServedError(TransientProviderError):
    """The provider does not serve the model run being acquired, unmixed, right now.

    Open-Meteo publishes a new run gradually (its docs ask to wait 10 minutes after the
    availability time) and fills hours beyond a short run's horizon with the previous
    run. Ingestion records no run for this; with ``retry_after_s`` the job retries then,
    without it the acquisition waits for the next scheduled poll.
    """


class PermanentProviderError(ProviderError):
    """Will not succeed on retry (bad credentials, invalid request, unsupported route)."""


class QuotaExhaustedError(PermanentProviderError, QuotaExceeded):
    """A call budget (app.core.redis quota) is used up: do not call again in this window.

    Also a :class:`~app.core.redis.QuotaExceeded`, so callers that stop on an exhausted
    quota (flight discovery) keep doing so, while forecast ingestion handles it like any
    other permanent provider error (the run ends FAILED, the next provider still runs).
    """


@dataclass(eq=False)
class HttpTally:
    """httpx response hook: responses, statuses and bytes received (thread-safe).

    ``bytes_downloaded`` counts the bytes on the wire (compressed bodies as received,
    ``httpx.Response.num_bytes_downloaded``), so it is what the provider actually sent.
    Install it with :meth:`install`; read the totals with :meth:`snapshot`.
    """

    responses: int = 0
    bytes_downloaded: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        with self._lock:
            self.responses += 1
            # Bodies built in memory (test transports) report 0 bytes on the wire.
            self.bytes_downloaded += response.num_bytes_downloaded or len(response.content)
            key = str(response.status_code)
            self.status_counts[key] = self.status_counts.get(key, 0) + 1

    def install(self, client: httpx.Client) -> None:
        hooks = client.event_hooks
        client.event_hooks = {
            "request": list(hooks.get("request", [])),
            "response": [*hooks.get("response", []), self],
        }

    def snapshot(self) -> tuple[int, int, dict[str, int]]:
        with self._lock:
            return self.responses, self.bytes_downloaded, dict(self.status_counts)


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
    retry_statuses: Collection[int] = RETRYABLE_STATUS,
    before_attempt: Callable[[], None] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying transient failures with exponential backoff + jitter.

    Each attempt counts against the provider's daily quota when ``quota`` is given; a used
    up quota raises :class:`QuotaExhaustedError` before the request is sent.
    ``before_attempt`` runs before every attempt (e.g. a weighted call budget, see
    app.services.forecasts.quota); whatever it raises propagates and nothing is sent.
    ``retry_statuses`` are the HTTP statuses retried (callers that interpret a status
    themselves, such as Open-Meteo's 429 reasons, leave it out).
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
            except QuotaExceeded as exc:
                raise QuotaExhaustedError(str(exc)) from exc
        if before_attempt is not None:
            before_attempt()
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
        if response.status_code in retry_statuses:
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
