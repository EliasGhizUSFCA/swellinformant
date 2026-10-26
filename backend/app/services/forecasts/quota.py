"""Open-Meteo call budget: weighted calls per minute, hour, day and month.

Open-Meteo does not bill HTTP requests. Its pricing page (checked 2026-10-14): "Requests
for data covering more than 10 weather variables or extending over a period of more than 2
weeks for a single location are considered multiple API calls." The weight of one request
is therefore

    locations x max(1, variables / 10) x max(1, days / 14) x models

(:func:`call_weight`; the multi-model factor follows the Ensemble API note that the
weight depends on "the number of locations, variables, or models involved"). Open-Meteo
has not published how it rounds; fractional weights are counted here, so the budget errs
neither way by more than a fraction of a call per request.

The free tier allows 600 calls per minute, 5,000 per hour, 10,000 per day and 300,000
per month (non-commercial only); OPEN_METEO_CALLS_PER_{MINUTE,HOUR,DAY,MONTH} hold the
limits (raise them for a paid plan). :func:`consume_open_meteo_budget` reserves a weight
in all four windows before a request is sent, atomically in Redis, and refuses it when
any window would overflow:

* minute or hour window full: :class:`BudgetWindowFullError` (transient; ``retry_after_s``
  says when the window resets), so the job retries soon;
* day or month window full: :class:`QuotaWindowExhaustedError`, a
  :class:`~app.services.http.QuotaExhaustedError` (permanent for this run; the run ends
  FAILED and the next provider still runs) whose ``retry_after_s`` is the time until the
  window resets. Open-Meteo's own daily or monthly limit (HTTP 429, "Daily API request
  limit exceeded") raises it too, until the next 00:00 UTC or the next month. A sync then
  waits that long before its next attempt (app.services.sync.detect.apply_result) instead
  of spending SYNC_MAX_ATTEMPTS attempts SYNC_RETRY_MINUTES apart on a refused day.

Both Open-Meteo families (FORECAST_PROVIDERS ``open_meteo`` and ``open_meteo_ecmwf``) draw
on the same budget: Open-Meteo counts per IP address (free tier) or per API key. The
metadata endpoints are free ("API calls to the metadata API are not counted toward daily
or monthly request limits") and are not charged. Counters live in Redis and restart from
zero when Redis loses its data (docs/operations.md); when Redis is unreachable the call
goes ahead and a warning is logged, as for the other quota counters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis

from app.core.config import Settings, get_settings
from app.core.redis import budget_usage, reserve_budget
from app.services.http import QuotaExhaustedError, TransientProviderError

logger = logging.getLogger(__name__)

BUDGET_NAME = "open_meteo"
VARIABLES_PER_CALL = 10
DAYS_PER_CALL = 14


def call_weight(locations: int, variables: int, days: float, models: int = 1) -> float:
    """Open-Meteo weighted calls for one request (see the module docstring)."""
    if locations < 1 or variables < 1 or models < 1:
        raise ValueError("a request has at least one location, variable and model")
    return (
        locations
        * max(1.0, variables / VARIABLES_PER_CALL)
        * max(1.0, days / DAYS_PER_CALL)
        * models
    )


class QuotaWindowExhaustedError(QuotaExhaustedError):
    """A day or month call limit is used up (this budget's window or Open-Meteo's own): no
    request succeeds before ``retry_after_s``, when the window resets."""

    def __init__(self, *args: object, retry_after_s: float) -> None:
        super().__init__(*args)
        self.retry_after_s = retry_after_s


def seconds_until_next_utc_day(now: datetime) -> float:
    """Seconds from ``now`` to the next 00:00 UTC (at least 60)."""
    t = now.astimezone(UTC)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(60.0, (midnight - t).total_seconds())


def seconds_until_next_utc_month(now: datetime) -> float:
    """Seconds from ``now`` to 00:00 UTC on the first day of the next month (at least 60)."""
    t = now.astimezone(UTC)
    start = _month_start(t.replace(second=0, microsecond=0), 1)
    return max(60.0, (start - t).total_seconds())


class BudgetWindowFullError(TransientProviderError):
    """The minute or hour budget is used up; the window resets after ``retry_after_s``."""


@dataclass(frozen=True)
class BudgetWindow:
    name: str  # "minute", "hour", "day", "month"
    key: str
    limit: float
    ttl_s: int
    resets_at: datetime


def _month_start(t: datetime, months_ahead: int = 0) -> datetime:
    index = t.year * 12 + (t.month - 1) + months_ahead
    return t.replace(year=index // 12, month=index % 12 + 1, day=1, hour=0, minute=0)


def budget_windows(
    now: datetime | None = None, settings: Settings | None = None, name: str = BUDGET_NAME
) -> list[BudgetWindow]:
    """The four counters (UTC windows) that a call is charged to."""
    s = settings or get_settings()
    t = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    hour = t.replace(minute=0)
    day = hour.replace(hour=0)
    month = _month_start(t)
    return [
        BudgetWindow(
            "minute",
            f"budget:{name}:minute:{t:%Y%m%d%H%M}",
            s.open_meteo_calls_per_minute,
            120,
            t + timedelta(minutes=1),
        ),
        BudgetWindow(
            "hour",
            f"budget:{name}:hour:{hour:%Y%m%d%H}",
            s.open_meteo_calls_per_hour,
            2 * 3600,
            hour + timedelta(hours=1),
        ),
        BudgetWindow(
            "day",
            f"budget:{name}:day:{day:%Y%m%d}",
            s.open_meteo_calls_per_day,
            2 * 86400,
            day + timedelta(days=1),
        ),
        BudgetWindow(
            "month",
            f"budget:{name}:month:{month:%Y%m}",
            s.open_meteo_calls_per_month,
            32 * 86400,
            _month_start(t, 1),
        ),
    ]


def consume_open_meteo_budget(
    weight: float, *, now: datetime | None = None, settings: Settings | None = None
) -> None:
    """Reserve ``weight`` weighted calls in every window, or raise without reserving any."""
    when = (now or datetime.now(UTC)).astimezone(UTC)
    windows = budget_windows(when, settings)
    try:
        full = reserve_budget([(w.key, w.limit, w.ttl_s) for w in windows], weight)
    except redis.RedisError:
        logger.warning("Open-Meteo call budget unavailable (Redis); continuing")
        return
    if not full:
        return
    window = windows[full - 1]
    wait = max(1.0, (window.resets_at - when).total_seconds())
    message = (
        f"Open-Meteo {window.name} budget of {window.limit:g} weighted calls "
        f"(OPEN_METEO_CALLS_PER_{window.name.upper()}) would be exceeded by a request of "
        f"{weight:.1f} calls; it resets at {window.resets_at:%Y-%m-%d %H:%M} UTC"
    )
    if window.name in ("minute", "hour"):
        raise BudgetWindowFullError(message, retry_after_s=wait)
    raise QuotaWindowExhaustedError(message, retry_after_s=wait)


def open_meteo_budget_usage(
    now: datetime | None = None, settings: Settings | None = None
) -> dict[str, dict[str, float]]:
    """Weighted calls used and the limit per window (for the CLI and the docs' checks)."""
    windows = budget_windows(now, settings)
    try:
        used = budget_usage([w.key for w in windows])
    except redis.RedisError:
        return {}
    return {
        w.name: {"used": round(u, 2), "limit": w.limit} for w, u in zip(windows, used, strict=True)
    }
