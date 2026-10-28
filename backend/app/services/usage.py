"""API usage reporting (spec section 16 "API calls consumed", design D12, contract C5).

What the app spent against each upstream allowance, for the admin freshness dashboard
(``GET /api/admin/freshness``) and the ``quota_near`` admin problem
(app.services.health):

* **Forecast APIs** (:func:`forecast_usage`): the Open-Meteo weighted-call counters of the
  current minute, hour, day and month (app.services.forecasts.quota; Open-Meteo bills
  weighted calls, not HTTP requests) and the Windy request counter of the current UTC day.
  Both live in Redis; when Redis cannot be read the counts are None (never 0).
* **Upstream requests of the last 24 h** (:func:`forecast_requests`): per forecast source,
  the runs started, the HTTP responses by status and the weighted calls the runs recorded
  (``forecast_runs.details["stats"]``, app.services.forecasts.ingestion). Non-2xx
  responses are the "failed requests" of the dashboard.
* **Flights** (:func:`flight_usage`): provider searches stored today (``flight_searches``,
  by provider and failed) and the configured provider's FLIGHT_DAILY_QUOTA counter in Redis
  (``app.core.redis.consume_quota``; every attempt counts, retries included). The demo
  provider has no quota.
* **Emails** (:func:`email_usage`): account emails (``email_messages``) and alert emails
  (``notifications``, channel email) created today, and how many of them failed.

All windows are UTC (the providers' quotas reset at 00:00 UTC). Queries are bounded: the
newest :data:`RECENT_RUNS` forecast runs by primary key, today's flight searches through
``ix_flight_searches_searched_at``; the two email tables have no index on ``created_at``
alone, so their counts scan the rows RETENTION_NOTIFICATION_DAYS keeps (alerts) and the
account emails (a few per user).

``quota.usage_snapshot`` (design D3) reads the Open-Meteo windows and the Windy day counter
in one Redis round trip.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.redis import get_redis
from app.models import EmailMessage, FlightSearch, ForecastRun, ForecastSource, Notification
from app.models.enums import (
    EmailDeliveryStatus,
    FlightSearchStatus,
    ForecastRunStatus,
    NotificationChannel,
    NotificationStatus,
)
from app.services.forecasts import quota

logger = logging.getLogger(__name__)

# Forecast runs read for the 24-hour request report (newest first, by primary key). A
# component refresh stores one run per changed component and model cycle (about 8 a day
# per source) plus retries of failed spots: far fewer than this.
RECENT_RUNS = 200
REQUEST_WINDOW = timedelta(hours=24)
FAILED_EMAIL_STATUSES = frozenset(
    {
        EmailDeliveryStatus.FAILED,
        EmailDeliveryStatus.BOUNCED,
        EmailDeliveryStatus.REJECTED,
        EmailDeliveryStatus.COMPLAINED,
    }
)
FAILED_ALERT_STATUSES = frozenset(
    {
        NotificationStatus.FAILED,
        NotificationStatus.BOUNCED,
        NotificationStatus.REJECTED,
        NotificationStatus.COMPLAINED,
    }
)


def day_start(now: datetime) -> datetime:
    """00:00 UTC of ``now``'s UTC day."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- forecast APIs
def forecast_usage(now: datetime | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """``{checked_at, open_meteo: {minute, hour, day, month, limits, resets_at, available},
    windy: {day, limit, resets_at, available, counted}}``: the forecast API counters, read
    in one Redis round trip by ``quota.usage_snapshot`` (fail-open: the counts are None when
    Redis cannot be read). ``counted``: this build counts Windy requests (always true)."""
    s = settings or get_settings()
    when = (now or utcnow()).astimezone(UTC)
    data = dict(quota.usage_snapshot(when, s))
    data["windy"] = {**dict(data.get("windy") or {}), "counted": True}
    return data


# --------------------------------------------------------------------------- requests 24 h
@dataclass
class SourceRequests:
    """What the forecast runs of one source cost upstream in the window."""

    source: str
    provider: str | None
    runs: int = 0
    failed_runs: int = 0
    requests: int = 0
    weighted_calls: float | None = None
    bytes_downloaded: int | None = None
    http_status_counts: dict[str, int] = field(default_factory=dict)

    @property
    def failed_requests(self) -> int:
        """Responses that were not 2xx (and transport failures the provider counted)."""
        return sum(n for status, n in self.http_status_counts.items() if not status.startswith("2"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provider": self.provider,
            "runs": self.runs,
            "failed_runs": self.failed_runs,
            "requests": self.requests,
            "weighted_calls": None
            if self.weighted_calls is None
            else round(self.weighted_calls, 2),
            "bytes_downloaded": self.bytes_downloaded,
            "http_status_counts": dict(sorted(self.http_status_counts.items())),
            "failed_requests": self.failed_requests,
        }


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def forecast_requests(
    db: Session, now: datetime | None = None, window: timedelta = REQUEST_WINDOW
) -> list[SourceRequests]:
    """Per forecast source, the runs started in the last ``window`` and what their fetches
    recorded (``details["stats"]``), from the newest :data:`RECENT_RUNS` runs."""
    now = now or utcnow()
    since = now - window
    rows = db.execute(
        select(
            ForecastRun.source_id,
            ForecastRun.status,
            ForecastRun.started_at,
            ForecastRun.request_count,
            ForecastRun.details["stats"],
        )
        .order_by(ForecastRun.id.desc())
        .limit(RECENT_RUNS)
    ).all()
    recent = [r for r in rows if r.started_at is not None and r.started_at >= since]
    if not recent:
        return []
    sources = {
        sid: (code, provider)
        for sid, code, provider in db.execute(
            select(ForecastSource.id, ForecastSource.code, ForecastSource.provider).where(
                ForecastSource.id.in_({r.source_id for r in recent})
            )
        )
    }
    out: dict[str, SourceRequests] = {}
    for row in recent:
        code, provider = sources.get(row.source_id, (str(row.source_id), None))
        entry = out.setdefault(code, SourceRequests(code, provider))
        entry.runs += 1
        if row.status == ForecastRunStatus.FAILED:
            entry.failed_runs += 1
        stats = row[4] if isinstance(row[4], dict) else {}
        requests = _int(stats.get("requests"))
        entry.requests += requests if requests is not None else int(row.request_count or 0)
        weighted = stats.get("weighted_calls")
        if isinstance(weighted, int | float) and not isinstance(weighted, bool):
            entry.weighted_calls = (entry.weighted_calls or 0.0) + float(weighted)
        downloaded = _int(stats.get("bytes_downloaded"))
        if downloaded is not None:
            entry.bytes_downloaded = (entry.bytes_downloaded or 0) + downloaded
        for status, n in (stats.get("http_status_counts") or {}).items():
            count = _int(n)
            if count:
                entry.http_status_counts[str(status)] = (
                    entry.http_status_counts.get(str(status), 0) + count
                )
    return sorted(out.values(), key=lambda e: e.source)


# --------------------------------------------------------------------------- flights
def flight_quota_key(provider: str, now: datetime) -> str:
    """The Redis key of a provider's daily request counter, as
    ``app.core.redis.consume_quota`` writes it (``quota:<provider>:<YYYYMMDD>``, UTC)."""
    return f"quota:{provider}:{now.astimezone(UTC):%Y%m%d}"


def flight_usage(
    db: Session, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Flight provider searches stored today (by provider; failed ones), and the configured
    provider's FLIGHT_DAILY_QUOTA counter (``quota`` None for the demo provider, which has
    no quota; ``used`` None when Redis cannot be read)."""
    s = settings or get_settings()
    now = now or utcnow()
    start = day_start(now)
    rows = db.execute(
        select(FlightSearch.provider, FlightSearch.status, func.count())
        .where(FlightSearch.searched_at >= start)
        .group_by(FlightSearch.provider, FlightSearch.status)
    ).all()
    per_provider: Counter[str] = Counter()
    failed = 0
    for provider, status, n in rows:
        per_provider[str(provider)] += int(n)
        if status == FlightSearchStatus.FAILED:
            failed += int(n)
    quota_out: dict[str, Any] | None = None
    if s.flight_provider != "demo":
        used: int | None
        try:
            raw = get_redis().get(flight_quota_key(s.flight_provider, now))
            used = int(raw) if raw else 0
        except (redis.RedisError, ValueError) as exc:
            logger.warning("Flight quota counter unavailable: %s", type(exc).__name__)
            used = None
        quota_out = {
            "used": used,
            "limit": s.flight_daily_quota,
            "resets_at": (start + timedelta(days=1)).isoformat(),
        }
    return {
        "provider": s.flight_provider,
        "today": sum(per_provider.values()),
        "per_provider": dict(sorted(per_provider.items())),
        "failed_today": failed,
        "quota": quota_out,
        "user_daily_quota": s.flight_user_daily_quota,
    }


# --------------------------------------------------------------------------- emails
def email_usage(
    db: Session, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Emails created today: account emails (verification, password reset, tests) and alert
    emails, and how many of them failed (failed, bounced, rejected or complained)."""
    s = settings or get_settings()
    start = day_start(now or utcnow())
    account, account_failed = db.execute(
        select(
            func.count(),
            func.count().filter(EmailMessage.status.in_(FAILED_EMAIL_STATUSES)),
        ).where(EmailMessage.created_at >= start)
    ).one()
    alerts, alerts_failed = db.execute(
        select(
            func.count(),
            func.count().filter(Notification.status.in_(FAILED_ALERT_STATUSES)),
        ).where(
            Notification.channel == NotificationChannel.EMAIL,
            Notification.created_at >= start,
        )
    ).one()
    return {
        "provider": s.email_provider,
        "today": int(account) + int(alerts),
        "account": int(account),
        "alerts": int(alerts),
        "failed": int(account_failed) + int(alerts_failed),
    }


# --------------------------------------------------------------------------- report
def failed_request_counts(
    requests: list[SourceRequests], flights_failed: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """``[{provider, status, count}]``: non-2xx forecast responses by source and status,
    plus failed flight searches by provider (status ``failed``), largest first."""
    out: dict[tuple[str, str], int] = defaultdict(int)
    for entry in requests:
        for status, n in entry.http_status_counts.items():
            if not status.startswith("2"):
                out[(entry.source, status)] += n
    for provider, n in (flights_failed or {}).items():
        if n:
            out[(provider, "failed")] += n
    return [
        {"provider": p, "status": st, "count": n}
        for (p, st), n in sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def failed_flight_searches(db: Session, now: datetime, window: timedelta) -> dict[str, int]:
    """Failed flight searches of the last ``window`` by provider."""
    rows = db.execute(
        select(FlightSearch.provider, func.count())
        .where(
            FlightSearch.searched_at >= now - window,
            FlightSearch.status == FlightSearchStatus.FAILED,
        )
        .group_by(FlightSearch.provider)
    ).all()
    return {str(p): int(n) for p, n in rows}


def usage_report(
    db: Session, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Everything above in one dict (the dashboard's ``api_usage`` and ``failed_requests``)."""
    s = settings or get_settings()
    now = now or utcnow()
    requests = forecast_requests(db, now)
    forecast = forecast_usage(now, s)
    return {
        "checked_at": now.isoformat(),
        "open_meteo": forecast.get("open_meteo") or {},
        "windy": forecast.get("windy") or {},
        "flights": flight_usage(db, now, s),
        "emails": email_usage(db, now, s),
        "requests_24h": [r.as_dict() for r in requests],
        "failed_requests_24h": failed_request_counts(
            requests, failed_flight_searches(db, now, REQUEST_WINDOW)
        ),
    }
