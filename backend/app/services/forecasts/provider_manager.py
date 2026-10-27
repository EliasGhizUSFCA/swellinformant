"""Which forecast provider is active right now (design D4).

Settings choose the providers (:meth:`app.core.config.Settings.provider_selection`:
FORECAST_PROVIDERS when set, else WEATHER_PROVIDER, Windy only when it is eligible). This
module adds the live state: when Windy is the primary and it recently failed with a
credential, testing-key, quota or availability problem, its configured fallback
(Open-Meteo) is the primary instead, with the same normalized records.

* :func:`active_provider` -> :class:`ActiveProvider`: the provider tokens to build (first =
  primary; one primary at a time), the selection reasons, and ``fallback_from="windy"``
  while the fallback stands in. ``registry.build_provider`` consults it, so the planner and
  the acquisition always build the active provider.
* :func:`record_provider_failure` / :func:`record_provider_success`: the provider health in
  Redis. The Windy adapter records a failure before it raises (credentials, testing_key,
  quota; unavailable after UNAVAILABLE_THRESHOLD consecutive transient failures) and a
  success after a good check or fetch. A failure keeps the fallback for
  WINDY_FALLBACK_MINUTES (a quota failure until Windy's counter resets at 00:00 UTC);
  afterwards Windy is tried again, and a success ends the fallback at once.
* :func:`health_check`: one cheap request (Open-Meteo: one ``meta.json``; Windy: one sentinel
  point request, which detects a testing or invalid key). It stores no forecast data and
  records no health.
* :func:`fallback_for` and :func:`failure_kind`: for a caller that falls back within the
  same refresh.

Lean imports only (the refresh Function imports the registry, which imports this module):
provider modules are imported inside :func:`health_check`. Redis failures are logged by type
and read as "no record" (Windy stays primary), never raised.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import redis

from app.core.config import WINDY_FALLBACK_PROVIDER, Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.core.redis import get_redis
from app.services.http import ProviderError, QuotaExhaustedError, TransientProviderError

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

WINDY = "windy"
OPEN_METEO = "open_meteo"
DEMO = "demo"
# Failure kinds of the provider health (admin problems: provider_credentials for the first
# two, quota_near / provider_unavailable for the others, provider_fallback while one holds).
CREDENTIALS = "credentials"
TESTING_KEY = "testing_key"
QUOTA = "quota"
UNAVAILABLE = "unavailable"
FAILURE_KINDS = (CREDENTIALS, TESTING_KEY, QUOTA, UNAVAILABLE)
# How long a recorded Windy failure keeps Open-Meteo as the primary before Windy is tried
# again (a constant, not a setting: one Windy request an hour is cheap, and a fixed key or
# licence is picked up within the hour or at once after a successful refresh).
WINDY_FALLBACK_MINUTES = 60
# Transient failures (timeouts, 5xx after the retries) in a row before Windy counts as
# unavailable: one slow answer never switches the provider.
UNAVAILABLE_THRESHOLD = 3
HEALTH_KEY = "provider-health:v1:{provider}"
HEALTH_TTL_SECONDS = 7 * 86400
DETAIL_MAX_CHARS = 300
# The provider a primary falls back to (design D4: Windy -> Open-Meteo, nothing else).
FALLBACKS: Mapping[str, str] = MappingProxyType({WINDY: WINDY_FALLBACK_PROVIDER})


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _time(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


def fallback_for(provider: str) -> str | None:
    """The provider that stands in for ``provider`` when it fails (``windy`` ->
    ``open_meteo``), or None."""
    return FALLBACKS.get(provider)


# --------------------------------------------------------------------------- health
@dataclass(frozen=True)
class ProviderHealth:
    """The recorded health of one provider (Redis ``provider-health:v1:<provider>``)."""

    provider: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_kind: str | None = None
    last_failure_detail: str | None = None
    # Transient failures since the last success (UNAVAILABLE_THRESHOLD makes it a failure).
    consecutive_unavailable: int = 0
    # The fallback holds until then (None: the last failure does not hold one).
    blocked_until: datetime | None = None

    def blocking(self, now: datetime) -> bool:
        """Whether the last failure keeps the provider out of use at ``now``."""
        if self.blocked_until is None or self.last_failure_at is None:
            return False
        if self.last_success_at is not None and self.last_success_at >= self.last_failure_at:
            return False
        return now < self.blocked_until

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_failure_kind": self.last_failure_kind,
            "last_failure_detail": self.last_failure_detail,
            "consecutive_unavailable": self.consecutive_unavailable,
            "blocked_until": _iso(self.blocked_until),
        }
        if now is not None:
            out["blocking"] = self.blocking(now)
        return out

    @classmethod
    def from_dict(cls, provider: str, data: Mapping[str, Any]) -> ProviderHealth:
        return cls(
            provider=provider,
            last_success_at=_time(data.get("last_success_at")),
            last_failure_at=_time(data.get("last_failure_at")),
            last_failure_kind=data.get("last_failure_kind"),
            last_failure_detail=data.get("last_failure_detail"),
            consecutive_unavailable=int(data.get("consecutive_unavailable") or 0),
            blocked_until=_time(data.get("blocked_until")),
        )


def health_key(provider: str) -> str:
    return HEALTH_KEY.format(provider=provider)


def provider_health(provider: str) -> ProviderHealth | None:
    """The recorded health of ``provider`` (None: nothing recorded, or Redis unreachable)."""
    try:
        raw = get_redis().get(health_key(provider))
    except redis.RedisError as exc:
        logger.warning(
            "Provider health of %s unavailable (Redis: %s)", provider, type(exc).__name__
        )
        return None
    if not raw:
        return None
    try:
        return ProviderHealth.from_dict(provider, json.loads(str(raw)))
    except (ValueError, TypeError):
        logger.warning("Provider health of %s is unreadable; ignored", provider)
        return None


def _save(health: ProviderHealth) -> None:
    try:
        get_redis().set(
            health_key(health.provider), json.dumps(health.as_dict()), ex=HEALTH_TTL_SECONDS
        )
    except redis.RedisError as exc:
        logger.warning(
            "Provider health of %s not recorded (Redis: %s)", health.provider, type(exc).__name__
        )


def record_provider_failure(
    provider: str,
    kind: str,
    detail: str = "",
    *,
    retry_after_s: float | None = None,
    now: datetime | None = None,
) -> ProviderHealth:
    """Record a failure of ``kind`` (FAILURE_KINDS). Credentials, testing-key and quota
    failures hold the fallback for WINDY_FALLBACK_MINUTES, or ``retry_after_s`` when that is
    longer (a daily limit: until 00:00 UTC); an availability failure only from the
    UNAVAILABLE_THRESHOLD-th in a row. ``detail`` is redacted and shortened."""
    if kind not in FAILURE_KINDS:
        raise ValueError(f"unknown provider failure kind {kind!r}")
    at = (now or utcnow()).astimezone(UTC)
    old = provider_health(provider) or ProviderHealth(provider)
    consecutive = old.consecutive_unavailable + 1 if kind == UNAVAILABLE else 0
    hold = timedelta(minutes=WINDY_FALLBACK_MINUTES)
    if retry_after_s is not None:
        hold = max(hold, timedelta(seconds=retry_after_s))
    blocked = None if kind == UNAVAILABLE and consecutive < UNAVAILABLE_THRESHOLD else at + hold
    if kind == UNAVAILABLE and blocked is None and old.blocking(at):
        blocked = old.blocked_until  # a transient failure never shortens a held fallback
    health = ProviderHealth(
        provider=provider,
        last_success_at=old.last_success_at,
        last_failure_at=at,
        last_failure_kind=kind,
        last_failure_detail=redact(detail)[:DETAIL_MAX_CHARS] or None,
        consecutive_unavailable=consecutive,
        blocked_until=blocked,
    )
    _save(health)
    logger.warning(
        "Provider %s failure recorded (%s): %s", provider, kind, health.last_failure_detail
    )
    return health


def record_provider_success(provider: str, *, now: datetime | None = None) -> ProviderHealth:
    """Record a successful check or fetch: the fallback (if any) ends, the counts reset."""
    at = (now or utcnow()).astimezone(UTC)
    old = provider_health(provider) or ProviderHealth(provider)
    health = ProviderHealth(
        provider=provider,
        last_success_at=at,
        last_failure_at=old.last_failure_at,
        last_failure_kind=old.last_failure_kind,
        last_failure_detail=old.last_failure_detail,
        consecutive_unavailable=0,
        blocked_until=None,
    )
    _save(health)
    return health


def failure_kind(exc: BaseException) -> str | None:
    """The FAILURE_KINDS entry an exception of a provider stands for, or None (a failure
    that falling back would not fix, e.g. one spot's 400)."""
    reason = getattr(exc, "reason", None)
    if reason == "windy_testing_key":
        return TESTING_KEY
    if reason == "windy_credentials":
        return CREDENTIALS
    if isinstance(exc, QuotaExhaustedError):
        return QUOTA
    if isinstance(exc, TransientProviderError):
        return UNAVAILABLE
    return None


# --------------------------------------------------------------------------- selection
@dataclass(frozen=True)
class ActiveProvider:
    """What a refresh builds now and why (see :func:`active_provider`)."""

    providers: tuple[str, ...]  # the tokens to build, first = primary
    configured: tuple[str, ...]  # what the settings select (Settings.provider_selection)
    source: str  # FORECAST_PROVIDERS or WEATHER_PROVIDER
    weather_provider: str
    windy_eligible: bool
    fallback: str | None  # the configured runtime fallback of the primary
    fallback_from: str | None = None  # the provider the fallback stands in for, now
    fallback_until: datetime | None = None
    reasons: tuple[str, ...] = ()
    health: Mapping[str, ProviderHealth] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def primary(self) -> str:
        return self.providers[0]

    @property
    def substitute(self) -> str | None:
        """The provider standing in for ``fallback_from`` (None: no fallback now)."""
        return self.fallback if self.fallback_from else None

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        return {
            "providers": list(self.providers),
            "primary": self.primary,
            "configured": list(self.configured),
            "source": self.source,
            "weather_provider": self.weather_provider,
            "windy_eligible": self.windy_eligible,
            "fallback": self.fallback,
            "fallback_from": self.fallback_from,
            "fallback_until": _iso(self.fallback_until),
            "reasons": list(self.reasons),
            "health": {name: h.as_dict(now) for name, h in self.health.items()},
        }


def _replace(providers: tuple[str, ...], old: str, new: str) -> tuple[str, ...]:
    """``providers`` with ``old`` replaced by ``new``, without duplicates, order kept."""
    return tuple(dict.fromkeys(new if p == old else p for p in providers))


def active_provider(
    settings: Settings | None = None, now: datetime | None = None
) -> ActiveProvider:
    """The providers to build now (first = primary) and why.

    From the settings selection; when its primary is Windy, Windy is replaced by its
    fallback (Open-Meteo) while WINDY_API_KEY is missing or a recorded failure holds
    (:meth:`ProviderHealth.blocking`). One Redis read, only when Windy is the primary.
    """
    s = settings or get_settings()
    at = (now or utcnow()).astimezone(UTC)
    selection = s.provider_selection()
    configured = ActiveProvider(
        providers=selection.providers,
        configured=selection.providers,
        source=selection.source,
        weather_provider=selection.weather_provider,
        windy_eligible=selection.windy_eligible,
        fallback=selection.fallback,
        reasons=selection.reasons,
    )
    if selection.primary != WINDY or selection.fallback is None:
        return configured
    fallback = selection.fallback
    standing_in = _replace(selection.providers, WINDY, fallback)
    if not s.windy_api_key:
        return replace(
            configured,
            providers=standing_in,
            fallback_from=WINDY,
            reasons=(
                *selection.reasons,
                f"WINDY_API_KEY is not set: {fallback} stands in for Windy",
            ),
        )
    health = provider_health(WINDY)
    if health is None:
        return configured
    recorded = MappingProxyType({WINDY: health})
    if health.blocking(at):
        assert health.last_failure_at is not None and health.blocked_until is not None
        reason = (
            f"Windy failed at {health.last_failure_at:%Y-%m-%d %H:%M} UTC "
            f"({health.last_failure_kind}: {health.last_failure_detail or 'no detail'}): "
            f"{fallback} stands in until {health.blocked_until:%Y-%m-%d %H:%M} UTC, then Windy "
            "is tried again (a successful Windy refresh ends the fallback at once)"
        )
        return replace(
            configured,
            providers=standing_in,
            fallback_from=WINDY,
            fallback_until=health.blocked_until,
            reasons=(*selection.reasons, reason),
            health=recorded,
        )
    reasons = selection.reasons
    if health.last_failure_at is not None:
        reasons = (
            *reasons,
            f"Windy is the primary; its last recorded failure ({health.last_failure_kind}, "
            f"{health.last_failure_at:%Y-%m-%d %H:%M} UTC) no longer holds the fallback",
        )
    return replace(configured, reasons=reasons, health=recorded)


# --------------------------------------------------------------------------- health check
@dataclass(frozen=True)
class HealthCheckResult:
    """One provider's health check: ``status`` ok, not_configured, refused (licence or
    configuration), credentials, testing_key, quota, unavailable or error."""

    provider: str
    status: str
    detail: str
    checked_at: datetime
    requests: int = 0
    info: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "checked_at": _iso(self.checked_at),
            "requests": self.requests,
            **({"info": dict(self.info)} if self.info else {}),
        }


def health_check(
    provider: str,
    settings: Settings | None = None,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> HealthCheckResult:
    """A cheap live check of ``provider`` (``open_meteo``, ``windy`` or ``demo``).

    Open-Meteo: one ``meta.json`` (the first wave model's domain; the customer host with
    OPEN_METEO_API_KEY). Windy: one sentinel point request (gfs), which shows a testing key,
    an invalid key or a used-up quota. Nothing is stored, no health is recorded; the
    request counts against Windy's daily limit like any other.
    """
    s = settings or get_settings()
    at = (now or utcnow()).astimezone(UTC)
    if provider == DEMO:
        return HealthCheckResult(provider, "ok", "synthetic demo data: nothing to check", at)
    if provider == OPEN_METEO:
        return _check_open_meteo(s, client, at)
    if provider == WINDY:
        return _check_windy(s, client, at)
    raise ValueError(f"no health check for provider {provider!r} (open_meteo, windy, demo)")


def _check_open_meteo(s: Settings, client: httpx.Client | None, at: datetime) -> HealthCheckResult:
    from app.services.forecasts.open_meteo import (
        FREE_API_NON_COMMERCIAL,
        HTTP_PROVIDER,
        ModelMeta,
        OpenMeteoProvider,
        free_api_refused,
    )
    from app.services.http import request_with_retries

    if free_api_refused(s):
        return HealthCheckResult(OPEN_METEO, "refused", FREE_API_NON_COMMERCIAL, at)
    p = OpenMeteoProvider(s, client=client, backoff_seconds=0)
    model, host, domain = p.domains()[0]
    params = {"apikey": s.open_meteo_api_key} if s.open_meteo_api_key else {}
    hosts = "customer" if s.open_meteo_api_key else "free"
    url = f"{p.hosts[host]}/data/{domain}/static/meta.json"
    try:
        resp = request_with_retries(
            p.client,
            "GET",
            url,
            provider=HTTP_PROVIDER,
            attempts=1,
            backoff_seconds=0,
            params=params,
        )
    except ProviderError as exc:
        return HealthCheckResult(
            OPEN_METEO, UNAVAILABLE, redact(str(exc))[:DETAIL_MAX_CHARS], at, 1
        )
    info: dict[str, Any] = {"hosts": hosts, "model": model, "domain": domain}
    if resp.status_code in (401, 403):
        detail = f"Open-Meteo refused the API key (HTTP {resp.status_code})"
        return HealthCheckResult(OPEN_METEO, CREDENTIALS, detail, at, 1, info)
    if resp.status_code == 429:
        return HealthCheckResult(OPEN_METEO, QUOTA, "Open-Meteo answered HTTP 429", at, 1, info)
    if resp.status_code != 200:
        detail = f"Open-Meteo metadata answered HTTP {resp.status_code}"
        return HealthCheckResult(OPEN_METEO, UNAVAILABLE, detail, at, 1, info)
    try:
        meta = ModelMeta.parse(domain, resp.json())
    except (ValueError, TypeError) as exc:
        detail = f"Open-Meteo metadata unreadable ({type(exc).__name__})"
        return HealthCheckResult(OPEN_METEO, UNAVAILABLE, detail, at, 1, info)
    info.update(
        last_run_initialisation_time=_iso(meta.init_time),
        last_run_availability_time=_iso(meta.available_at),
        update_interval_seconds=meta.update_interval_s,
    )
    detail = f"{domain} run {meta.init_time:%Y-%m-%d %H}Z is published ({hosts} hosts)"
    return HealthCheckResult(OPEN_METEO, "ok", detail, at, 1, info)


def _check_windy(s: Settings, client: httpx.Client | None, at: datetime) -> HealthCheckResult:
    from app.services.forecasts.windy import (
        GFS,
        SENTINEL_SLUG,
        WindyProvider,
        fingerprint,
        ms_to_utc,
        short_fingerprint,
    )

    if not s.windy_api_key:
        return HealthCheckResult(WINDY, "not_configured", "WINDY_API_KEY is not set", at)
    p = WindyProvider(s, client=client, backoff_seconds=0, record_health=False)
    try:
        data = p.sentinel(GFS)
    except ProviderError as exc:
        kind = failure_kind(exc) or "error"
        return HealthCheckResult(
            WINDY, kind, redact(str(exc))[:DETAIL_MAX_CHARS], at, p.tally.snapshot()[0]
        )
    times = [ms_to_utc(t) for t in data.get("ts") or []]
    info: dict[str, Any] = {
        "model": GFS,
        "sentinel": SENTINEL_SLUG,
        "steps": len(times),
        "first_step": _iso(times[0]) if times else None,
        "last_step": _iso(times[-1]) if times else None,
        "units": dict(data.get("units") or {}),
        "fingerprint": short_fingerprint(fingerprint(GFS, data)),
        "storage_authorized": s.windy_storage_authorized,
    }
    detail = f"Windy answered {len(times)} {GFS} steps for the {SENTINEL_SLUG} sentinel"
    if not s.windy_storage_authorized:
        detail += "; WINDY_STORAGE_AUTHORIZED is not true, so Windy data are not stored"
    return HealthCheckResult(WINDY, "ok", detail, at, p.tally.snapshot()[0], info)


# --------------------------------------------------------------------------- CLI report
def provider_check_report(
    settings: Settings | None = None,
    providers: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The ``forecast provider-check`` report: selection, eligibility, recorded health and
    the health check of ``providers`` (default: the active primary and its fallback).
    Never contains a key's value; stores nothing."""
    s = settings or get_settings()
    at = (now or utcnow()).astimezone(UTC)
    active = active_provider(s, at)
    if providers is None:
        providers = list(dict.fromkeys([active.primary, *(p for p in [active.fallback] if p)]))
        providers = [p for p in providers if p in (OPEN_METEO, WINDY, DEMO)]
    from app.services.forecasts.open_meteo import free_api_refused

    eligibility = {
        WINDY: {
            "eligible": s.windy_eligible,
            "missing": s.windy_ineligibility(),
            "api_key_set": bool(s.windy_api_key),
            "storage_authorized": s.windy_storage_authorized,
        },
        OPEN_METEO: {
            "commercial_use": s.commercial_use,
            "api_key_set": bool(s.open_meteo_api_key),
            "hosts": "customer" if s.open_meteo_api_key else "free",
            "free_api_refused": free_api_refused(s),
        },
    }
    checks = [health_check(p, s, client=client, now=at) for p in providers]
    recorded = {name: h.as_dict(at) for name in (WINDY, OPEN_METEO) if (h := provider_health(name))}
    return {
        "checked_at": _iso(at),
        "selection": s.provider_selection().as_dict(),
        "active": active.as_dict(at),
        "eligibility": eligibility,
        "recorded_health": recorded,
        "health_checks": [c.as_dict() for c in checks],
        "ok": all(c.ok for c in checks),
        "stored": "nothing (provider-check stores no forecast data and records no health)",
    }
