"""The provider manager (forecasts.provider_manager) and the registry built on it.

Selection from settings (WEATHER_PROVIDER / FORECAST_PROVIDERS, Windy eligibility), the
runtime fallback of a Windy primary to Open-Meteo after recorded failures and the recovery
after a success, and the cheap health checks against mocked HTTP. No live call: every
response is SYNTHETIC (Windy: tests/windy_fixtures.py; Open-Meteo: the recorded meta.json of
tests/fixtures/open_meteo). Every key is a synthetic test value.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import redis
from pydantic import AliasChoices, ValidationError

from app.core.config import Settings
from app.core.redis import get_redis
from app.services.forecasts import provider_manager as pm
from app.services.forecasts import quota, registry
from app.services.forecasts.demo import DemoForecastProvider
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.windy import (
    TESTING_KEY_REASON,
    WindyCredentialsError,
    WindyProvider,
    WindyQuotaError,
    WindyTestingKeyError,
)
from app.services.http import PermanentProviderError, TransientProviderError
from tests.unit.test_open_meteo_adapter import META
from tests.windy_fixtures import WindyServer, as_testing_key, gfs_body

WINDY_KEY = "test-only-windy-key-91c2"  # synthetic: must never appear in any output
OM_KEY = "test-only-open-meteo-key-55d0"
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)


def env_names() -> set[str]:
    names = {name.upper() for name in Settings.model_fields}
    for f in Settings.model_fields.values():
        if isinstance(f.validation_alias, AliasChoices):
            names |= {c.upper() for c in f.validation_alias.choices if isinstance(c, str)}
    return names


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No provider setting from the session (conftest sets demo) and no Redis leftovers."""
    for name in env_names() - {"DATABASE_URL", "REDIS_URL", "APP_ENV", "SECRET_KEY"}:
        monkeypatch.delenv(name, raising=False)
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()
    yield
    with contextlib.suppress(redis.RedisError):
        get_redis().flushdb()


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def windy_primary(**kw: object) -> Settings:
    return make(windy_api_key=WINDY_KEY, windy_storage_authorized=True, **kw)


def production(**kw: object) -> Settings:
    return make(
        app_env="production",
        secret_key="s" * 40,
        cookie_secure=True,
        email_provider="resend",
        resend_api_key="re_test_only",
        **kw,
    )


# --------------------------------------------------------------------------- selection
@pytest.mark.parametrize(
    ("kw", "providers", "built"),
    [
        ({}, ("open_meteo",), OpenMeteoProvider),  # auto without Windy
        ({"windy_api_key": WINDY_KEY}, ("open_meteo",), OpenMeteoProvider),  # a key is no licence
        ({"windy_storage_authorized": True}, ("open_meteo",), OpenMeteoProvider),
        ({"windy_api_key": WINDY_KEY, "windy_storage_authorized": True}, ("windy",), WindyProvider),
        (
            {
                "weather_provider": "open_meteo",
                "windy_api_key": WINDY_KEY,
                "windy_storage_authorized": True,
            },
            ("open_meteo",),
            OpenMeteoProvider,
        ),
        ({"weather_provider": "windy"}, ("open_meteo",), OpenMeteoProvider),  # not production
        (
            {
                "weather_provider": "windy",
                "windy_api_key": WINDY_KEY,
                "windy_storage_authorized": True,
            },
            ("windy",),
            WindyProvider,
        ),
        ({"weather_provider": "demo", "windy_api_key": WINDY_KEY}, ("demo",), DemoForecastProvider),
        (
            {"forecast_providers": "open_meteo,open_meteo_ecmwf", "weather_provider": "demo"},
            ("open_meteo", "open_meteo_ecmwf"),
            OpenMeteoProvider,
        ),
    ],
)
def test_the_auto_selection_matrix(
    kw: dict[str, Any], providers: tuple[str, ...], built: type
) -> None:
    s = make(**kw)
    active = pm.active_provider(s, NOW)
    assert active.providers == providers and active.fallback_from is None
    assert active.primary == providers[0]
    assert registry.active_provider_tokens(s) == list(providers)
    provider = registry.build_provider(active.primary, s)
    assert isinstance(provider, built)
    assert registry.fallback_origin(provider) is None
    assert active.reasons  # always says why
    assert WINDY_KEY not in json.dumps(active.as_dict(NOW))


def test_a_missing_windy_key_selects_open_meteo_and_production_refuses_windy() -> None:
    auto = pm.active_provider(make(), NOW)
    assert auto.providers == ("open_meteo",) and not auto.windy_eligible
    assert any("WINDY_API_KEY" in r for r in auto.reasons)
    with pytest.raises(ValidationError, match="WEATHER_PROVIDER=windy needs"):
        production(weather_provider="windy")
    assert production().forecast_provider_list == ["open_meteo"]  # auto never refuses


def test_an_explicit_windy_entry_without_a_key_stands_in_with_open_meteo() -> None:
    s = make(forecast_providers="windy", windy_storage_authorized=True)
    active = pm.active_provider(s, NOW)
    assert active.providers == ("open_meteo",) and active.fallback_from == "windy"
    provider = registry.build_provider("windy", s)
    assert isinstance(provider, OpenMeteoProvider)
    assert registry.fallback_origin(provider) == "windy"


def test_windy_without_storage_authorization_is_never_built() -> None:
    with pytest.raises(PermanentProviderError, match="terms forbid storing"):
        registry.build_provider("windy", make(windy_api_key=WINDY_KEY))


# --------------------------------------------------------------------------- fallback
@pytest.mark.parametrize("kind", [pm.CREDENTIALS, pm.TESTING_KEY, pm.QUOTA])
def test_a_recorded_windy_failure_makes_open_meteo_the_primary(kind: str) -> None:
    s = windy_primary()
    now = datetime.now(UTC)  # the registry reads the real clock
    assert pm.active_provider(s, now).providers == ("windy",)
    pm.record_provider_failure("windy", kind, f"synthetic {kind} failure", now=now)
    active = pm.active_provider(s, now + timedelta(minutes=5))
    assert active.providers == ("open_meteo",)
    assert active.fallback_from == "windy" and active.substitute == "open_meteo"
    assert active.fallback_until == now + timedelta(minutes=pm.WINDY_FALLBACK_MINUTES)
    assert any("stands in" in r for r in active.reasons)
    # The planner and the acquisition build the token "windy": they get Open-Meteo, marked.
    built = registry.build_provider("windy", s)
    assert isinstance(built, OpenMeteoProvider)
    assert registry.fallback_origin(built) == "windy"
    # Built from its own token meanwhile (a caller using active_provider_tokens), too.
    assert registry.fallback_origin(registry.build_provider("open_meteo", s)) == "windy"
    assert registry.active_source_codes(s) == ["open_meteo_noaa"]
    assert registry.configured_source_codes(s) == ["windy_gfswave"]  # the settings choice


def test_the_fallback_ends_after_the_hold_or_at_once_after_a_success() -> None:
    s = windy_primary()
    now = datetime.now(UTC)  # the registry reads the real clock
    pm.record_provider_failure("windy", pm.CREDENTIALS, "invalid key", now=now)
    later = now + timedelta(minutes=pm.WINDY_FALLBACK_MINUTES + 1)
    assert pm.active_provider(s, later).providers == ("windy",)  # tried again
    assert isinstance(registry.build_provider("windy", s), OpenMeteoProvider)  # still held
    pm.record_provider_success("windy", now=now + timedelta(seconds=1))
    active = pm.active_provider(s, now + timedelta(seconds=2))
    assert active.providers == ("windy",) and active.fallback_from is None
    assert isinstance(registry.build_provider("windy", s), WindyProvider)
    assert any("no longer holds" in r for r in active.reasons)


def test_a_quota_failure_holds_until_windy_resets_its_counter() -> None:
    s = windy_primary()
    wait = quota.seconds_until_next_utc_day(NOW)  # 19 h at 05:00 UTC
    pm.record_provider_failure("windy", pm.QUOTA, "daily limit", retry_after_s=wait, now=NOW)
    assert pm.active_provider(s, NOW + timedelta(hours=18)).fallback_from == "windy"
    midnight = datetime(2026, 10, 28, tzinfo=UTC)
    assert pm.active_provider(s, midnight + timedelta(seconds=1)).fallback_from is None


def test_transient_failures_count_only_from_the_third_in_a_row() -> None:
    s = windy_primary()
    for _ in range(pm.UNAVAILABLE_THRESHOLD - 1):
        pm.record_provider_failure("windy", pm.UNAVAILABLE, "HTTP 503", now=NOW)
    assert pm.active_provider(s, NOW).providers == ("windy",)
    health = pm.record_provider_failure("windy", pm.UNAVAILABLE, "HTTP 503", now=NOW)
    assert health.consecutive_unavailable == pm.UNAVAILABLE_THRESHOLD
    assert pm.active_provider(s, NOW).providers == ("open_meteo",)
    pm.record_provider_success("windy", now=NOW + timedelta(minutes=1))
    assert pm.provider_health("windy").consecutive_unavailable == 0  # type: ignore[union-attr]


def test_a_transient_failure_never_shortens_a_held_fallback() -> None:
    pm.record_provider_failure("windy", pm.CREDENTIALS, "invalid key", now=NOW)
    pm.record_provider_failure("windy", pm.UNAVAILABLE, "timeout", now=NOW + timedelta(minutes=2))
    assert pm.active_provider(windy_primary(), NOW + timedelta(minutes=3)).fallback_from == "windy"


def test_a_windy_failure_does_not_touch_other_primaries() -> None:
    pm.record_provider_failure("windy", pm.CREDENTIALS, "invalid key", now=NOW)
    s = make(
        forecast_providers="noaa_gfs,windy", windy_api_key=WINDY_KEY, windy_storage_authorized=True
    )
    assert pm.active_provider(s, NOW).providers == ("noaa_gfs", "windy")  # no fallback configured
    assert pm.active_provider(make(), NOW).providers == ("open_meteo",)


def test_an_unreachable_redis_keeps_windy_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def get(self, *a: object, **k: object) -> None:
            raise redis.ConnectionError("down")

        def set(self, *a: object, **k: object) -> None:
            raise redis.ConnectionError("down")

    monkeypatch.setattr(pm, "get_redis", lambda: Down())
    assert pm.provider_health("windy") is None
    pm.record_provider_failure("windy", pm.CREDENTIALS, "invalid key", now=NOW)  # no raise
    assert pm.active_provider(windy_primary(), NOW).providers == ("windy",)


def test_failure_details_are_redacted_and_short() -> None:
    health = pm.record_provider_failure(
        "windy", pm.CREDENTIALS, "key=" + WINDY_KEY + " password=hunter2 " + "x" * 500, now=NOW
    )
    assert health.last_failure_detail is not None and len(health.last_failure_detail) <= 300
    assert "hunter2" not in health.last_failure_detail
    with pytest.raises(ValueError, match="unknown provider failure kind"):
        pm.record_provider_failure("windy", "bad", now=NOW)


def test_failure_kinds_of_provider_errors() -> None:
    assert pm.failure_kind(WindyTestingKeyError("t")) == pm.TESTING_KEY
    assert pm.failure_kind(WindyCredentialsError("c")) == pm.CREDENTIALS
    assert pm.failure_kind(WindyQuotaError("q", retry_after_s=60)) == pm.QUOTA
    assert pm.failure_kind(TransientProviderError("503")) == pm.UNAVAILABLE
    assert pm.failure_kind(PermanentProviderError("one spot's 400")) is None
    assert pm.fallback_for("windy") == "open_meteo" and pm.fallback_for("open_meteo") is None


# --------------------------------------------------------------------------- health check
def om_client(calls: list[httpx.Request], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        domain = request.url.path.split("/")[2]
        if status != 200:
            return httpx.Response(status, json={"reason": "synthetic"})
        return httpx.Response(200, json=META[domain])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_open_meteo_check_reads_one_meta_json() -> None:
    calls: list[httpx.Request] = []
    result = pm.health_check("open_meteo", make(), client=om_client(calls), now=NOW)
    assert result.ok and result.requests == 1 and len(calls) == 1
    assert calls[0].url.host == "marine-api.open-meteo.com"
    assert calls[0].url.path == "/data/ncep_gfswave016/static/meta.json"
    assert result.info["last_run_initialisation_time"] == "2026-10-13T18:00:00+00:00"
    keyed: list[httpx.Request] = []
    s = make(open_meteo_api_key=OM_KEY, commercial_use=True)
    assert pm.health_check("open_meteo", s, client=om_client(keyed), now=NOW).ok
    assert keyed[0].url.host == "customer-marine-api.open-meteo.com"
    assert keyed[0].url.params["apikey"] == OM_KEY
    denied = pm.health_check("open_meteo", s, client=om_client([], 401), now=NOW)
    assert denied.status == pm.CREDENTIALS and OM_KEY not in json.dumps(denied.as_dict())


def test_the_open_meteo_check_refuses_commercial_use_without_a_key() -> None:
    calls: list[httpx.Request] = []
    result = pm.health_check("open_meteo", make(commercial_use=True), client=om_client(calls))
    assert result.status == "refused" and calls == []


def test_the_windy_check_is_one_sentinel_request_and_records_nothing() -> None:
    server = WindyServer()
    result = pm.health_check(
        "windy", make(windy_api_key=WINDY_KEY), client=server.client(), now=NOW
    )
    assert result.ok and result.requests == 1 and server.models() == ["gfs"]
    assert result.info["steps"] == 8 and result.info["storage_authorized"] is False
    assert "not stored" in result.detail
    assert pm.provider_health("windy") is None  # nothing recorded
    assert get_redis().keys("windy:run-state:*") == []  # no sentinel state either


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (lambda b: as_testing_key(gfs_body()), pm.TESTING_KEY),
        (lambda b: httpx.Response(400, json={"message": "Invalid API key"}), pm.CREDENTIALS),
        (lambda b: httpx.Response(429, json={"message": "Too many requests"}), pm.QUOTA),
        (lambda b: httpx.Response(503), pm.UNAVAILABLE),
    ],
)
def test_the_windy_check_names_the_problem(response: Any, status: str) -> None:
    server = WindyServer(gfs=response)
    result = pm.health_check(
        "windy", make(windy_api_key=WINDY_KEY), client=server.client(), now=NOW
    )
    assert result.status == status and not result.ok
    assert WINDY_KEY not in json.dumps(result.as_dict())
    assert pm.provider_health("windy") is None
    if status == pm.TESTING_KEY:
        assert TESTING_KEY_REASON in result.detail


def test_the_windy_check_without_a_key_sends_nothing() -> None:
    assert pm.health_check("windy", make(), now=NOW).status == "not_configured"
    with pytest.raises(ValueError, match="no health check"):
        pm.health_check("noaa_gfs", make())
