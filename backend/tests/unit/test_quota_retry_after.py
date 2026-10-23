"""A used-up day or month limit says when to try again (Upgrade 4 integration).

Open-Meteo answered "Daily API request limit exceeded" to the shared sandbox IP on
2026-10-22 at 09:38 UTC; the sync then spent its SYNC_MAX_ATTEMPTS attempts 15 minutes
apart on a day that could not succeed. The error now carries ``retry_after_s`` until the
limit resets (assumed 00:00 UTC for the daily limit, the 1st for the monthly one), the
acquisition outcome keeps it, and the sync waits that long. Responses are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.services.forecasts.ingestion import _failure
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.quota import (
    QuotaWindowExhaustedError,
    seconds_until_next_utc_day,
    seconds_until_next_utc_month,
)
from app.services.http import PermanentProviderError, QuotaExhaustedError
from app.services.sync.detect import apply_result
from app.services.sync.types import (
    KnownRuns,
    StageResult,
    SyncPlan,
    SyncResult,
    known_key,
)

REFUSED_AT = datetime(2026, 10, 22, 9, 38, tzinfo=UTC)
MIDNIGHT = datetime(2026, 10, 23, tzinfo=UTC)


def provider(now: datetime) -> OpenMeteoProvider:
    settings = Settings(_env_file=None, forecast_providers="open_meteo")  # type: ignore[call-arg]
    return OpenMeteoProvider(settings, client=httpx.Client(), clock=lambda: now)


def refusal(reason: str) -> httpx.Response:
    request = httpx.Request("GET", "https://marine-api.open-meteo.test/v1/marine")
    return httpx.Response(429, json={"error": True, "reason": reason}, request=request)


def test_the_utc_resets() -> None:
    assert seconds_until_next_utc_day(REFUSED_AT) == (MIDNIGHT - REFUSED_AT).total_seconds()
    end_of_month = datetime(2026, 12, 31, 23, 0, tzinfo=UTC)
    assert seconds_until_next_utc_month(end_of_month) == 3600.0  # into 2027-01-01
    late = datetime(2026, 10, 22, 23, 59, 30, tzinfo=UTC)
    assert seconds_until_next_utc_day(late) == 60.0  # never less than a minute


def test_open_meteos_daily_limit_waits_until_the_next_utc_day() -> None:
    reason = "Daily API request limit exceeded. Please try again tomorrow."
    with pytest.raises(QuotaWindowExhaustedError) as refused:
        provider(REFUSED_AT)._raise_for_status(refusal(reason))
    error = refused.value
    assert isinstance(error, QuotaExhaustedError) and isinstance(error, PermanentProviderError)
    assert not error.transient  # the job does not retry it within minutes
    assert error.retry_after_s == (MIDNIGHT - REFUSED_AT).total_seconds() == 51720.0


def test_open_meteos_monthly_limit_waits_until_the_next_month() -> None:
    reason = "Monthly API request limit exceeded. Please try again next month."
    with pytest.raises(QuotaWindowExhaustedError) as refused:
        provider(REFUSED_AT)._raise_for_status(refusal(reason))
    next_month = datetime(2026, 11, 1, tzinfo=UTC)
    assert refused.value.retry_after_s == (next_month - REFUSED_AT).total_seconds()


def test_the_outcome_keeps_the_wait_and_the_sync_honours_it() -> None:
    error = QuotaWindowExhaustedError("Open-Meteo: daily limit", retry_after_s=51720.0)
    outcome = _failure("open_meteo_noaa", "gfswave016:2026102206", error)
    assert outcome.status == "failed" and not outcome.retryable
    assert outcome.retry_after_s == 51720.0
    plain = _failure("open_meteo_noaa", "gfswave016:2026102206", PermanentProviderError("no"))
    assert plain.retry_after_s is None

    run_key = "gfswave016:2026102206"
    plan = SyncPlan(checked_at=REFUSED_AT, due=True, components=(), anchor_cycle=None, reasons=())
    stage = StageResult(
        "spots",
        "failed",
        REFUSED_AT,
        10,
        0.1,
        {
            "components": [
                {
                    "source_code": "open_meteo_noaa",
                    "run_key": run_key,
                    "outcome": "failed",
                    "retry_after_s": outcome.retry_after_s,
                }
            ]
        },
    )
    known = KnownRuns(stored={}, map_cycle=None, first_seen={}, attempts={}, loaded_at=REFUSED_AT)
    apply_result(known, SyncResult(1, "failed", plan, [stage], None), now=REFUSED_AT)
    attempts, last = known.attempts[known_key("open_meteo_noaa", run_key)]
    retry = timedelta(minutes=get_settings().sync_retry_minutes)
    # One attempt, and the next is due at 00:00 UTC (the planner waits until last + retry),
    # not SYNC_RETRY_MINUTES after the refusal.
    assert attempts == 1 and last + retry == MIDNIGHT
