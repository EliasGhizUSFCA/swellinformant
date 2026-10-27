"""Swell 2.0 settings (foundation, design B): WEATHER_PROVIDER and the derived provider list
(design D2), the env aliases, the validation of the new fields, the production start-up
checks and the declared profiles. Every key below is a SYNTHETIC test value."""

from __future__ import annotations

import itertools

import pytest
from pydantic import AliasChoices, ValidationError

from app.core.config import WINDY_FALLBACK_PROVIDER, Settings
from app.core.profiles import (
    FREE_DATABASE_PROFILE,
    FULL_RETENTION_OVER_DEFAULTS,
    FULL_RETENTION_PROFILE,
    HOSTED_PROFILE,
    profile_env,
)

WINDY_KEY = "test-only-windy-key-7f3a"  # synthetic; must never appear in a reason or error
OM_KEY = "test-only-open-meteo-key"


def env_names() -> set[str]:
    """Every environment name Settings reads (field names and their aliases)."""
    names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, AliasChoices):
            names |= {c.upper() for c in field.validation_alias.choices if isinstance(c, str)}
    return names


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No setting from the test session (tests/conftest.py sets FORECAST_PROVIDERS=demo) or
    the developer's shell: only what a test passes speaks."""
    for name in env_names():
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def production(**kw: object) -> Settings:
    return make(
        app_env="production",
        secret_key="s" * 40,
        cookie_secure=True,
        email_provider="resend",
        resend_api_key="re_test_only",
        **kw,
    )


# --------------------------------------------------------------------------- derivation
@pytest.mark.parametrize(
    ("choice", "key", "authorized"),
    list(
        itertools.product(["auto", "open_meteo", "windy", "demo"], [None, WINDY_KEY], [False, True])
    ),
)
def test_the_provider_list_is_derived_from_weather_provider(
    choice: str, key: str | None, authorized: bool
) -> None:
    s = make(
        weather_provider=choice,
        windy_api_key=key,
        windy_storage_authorized=authorized,
        flight_provider="duffel",  # demo_mode below then speaks of the forecasts only
    )
    eligible = key is not None and authorized
    expected = {
        "demo": ["demo"],
        "open_meteo": ["open_meteo"],
        "windy": ["windy"] if eligible else ["open_meteo"],
        "auto": ["windy"] if eligible else ["open_meteo"],
    }[choice]
    selection = s.provider_selection()
    assert s.forecast_provider_list == expected
    assert list(selection.providers) == expected and selection.primary == expected[0]
    assert selection.source == "WEATHER_PROVIDER" and selection.weather_provider == choice
    assert s.windy_eligible is eligible and selection.windy_eligible is eligible
    assert selection.fallback == (WINDY_FALLBACK_PROVIDER if expected == ["windy"] else None)
    assert selection.reasons and all(isinstance(r, str) and r for r in selection.reasons)
    assert WINDY_KEY not in " ".join(selection.reasons)
    assert selection.as_dict()["providers"] == expected
    # demo_mode follows the effective list.
    assert s.demo_mode is (expected == ["demo"])


def test_the_reasons_say_why_windy_is_or_is_not_used() -> None:
    nothing = make().provider_selection().reasons[0]
    assert "WEATHER_PROVIDER=auto" in nothing and "Open-Meteo is used" in nothing
    assert "WINDY_API_KEY" in nothing and "WINDY_STORAGE_AUTHORIZED=true" in nothing
    keyed = make(windy_api_key=WINDY_KEY).provider_selection().reasons[0]
    # A key is not a licence: only the authorization is missing.
    assert "WINDY_STORAGE_AUTHORIZED=true missing" in keyed and "WINDY_API_KEY" not in keyed
    windy = make(weather_provider="windy").provider_selection().reasons
    assert any("production refuses WEATHER_PROVIDER=windy" in r for r in windy)
    eligible = make(windy_api_key=WINDY_KEY, windy_storage_authorized=True).provider_selection()
    assert "Windy is the primary" in eligible.reasons[0]
    assert any("runtime fallback: open_meteo" in r for r in eligible.reasons)


def test_the_default_is_auto_and_without_windy_it_is_open_meteo() -> None:
    s = make()
    assert s.weather_provider == "auto" and s.forecast_providers is None
    assert s.forecast_provider_list == ["open_meteo"]
    assert make(flight_provider="duffel").demo_mode is False


@pytest.mark.parametrize("choice", ["auto", "open_meteo", "windy", "demo"])
def test_forecast_providers_is_an_explicit_override(choice: str) -> None:
    s = make(
        weather_provider=choice,
        forecast_providers="open_meteo,open_meteo_ecmwf",
        windy_api_key=WINDY_KEY,
        windy_storage_authorized=True,
    )
    selection = s.provider_selection()
    assert s.forecast_provider_list == ["open_meteo", "open_meteo_ecmwf"]
    assert selection.source == "FORECAST_PROVIDERS" and selection.fallback is None
    assert "advanced override" in selection.reasons[0]
    assert f"WEATHER_PROVIDER={choice} is not used" in selection.reasons[0]


@pytest.mark.parametrize("blank", ["", " , ", "  "])
def test_a_blank_forecast_providers_means_derived(
    blank: str, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("FORECAST_PROVIDERS", blank)
    clean_env.setenv("WEATHER_PROVIDER", "demo")
    assert make().forecast_provider_list == ["demo"]
    assert make().provider_selection().source == "WEATHER_PROVIDER"


def test_the_existing_override_rules_still_apply() -> None:
    with pytest.raises(ValidationError, match="cannot mix 'demo'"):
        make(forecast_providers="demo,open_meteo")
    with pytest.raises(ValidationError, match="Unknown FORECAST_PROVIDERS"):
        make(forecast_providers="open_meteo,surfline")
    # An explicit windy entry still needs the storage authorization, in every environment.
    with pytest.raises(ValidationError, match="WINDY_STORAGE_AUTHORIZED=true") as refused:
        make(forecast_providers="windy", windy_api_key=WINDY_KEY)
    assert "WINDY_TERMS_ACKNOWLEDGED" in str(refused.value)  # the old name is named too
    assert WINDY_KEY not in str(refused.value.errors(include_input=False))


def test_weather_provider_accepts_only_the_four_choices() -> None:
    with pytest.raises(ValidationError):
        make(weather_provider="open-meteo")
    with pytest.raises(ValidationError):
        make(weather_provider="noaa_gfs")


def test_the_selection_is_read_from_the_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("WEATHER_PROVIDER", "auto")
    clean_env.setenv("WINDY_API_KEY", WINDY_KEY)
    assert make().forecast_provider_list == ["open_meteo"]
    clean_env.setenv("WINDY_STORAGE_AUTHORIZED", "true")
    assert make().forecast_provider_list == ["windy"]
    clean_env.setenv("FORECAST_PROVIDERS", "demo")
    assert make().forecast_provider_list == ["demo"]


# --------------------------------------------------------------------------- aliases
def test_windy_storage_authorized_reads_the_old_name(clean_env: pytest.MonkeyPatch) -> None:
    assert make().windy_storage_authorized is False
    clean_env.setenv("WINDY_TERMS_ACKNOWLEDGED", "true")
    s = make()
    assert s.windy_storage_authorized is True and s.windy_terms_acknowledged is True
    # The new name wins when both are set; a blank new name means unset (env_ignore_empty).
    clean_env.setenv("WINDY_STORAGE_AUTHORIZED", "false")
    assert make().windy_storage_authorized is False
    clean_env.setenv("WINDY_STORAGE_AUTHORIZED", "")
    assert make().windy_storage_authorized is True
    clean_env.delenv("WINDY_TERMS_ACKNOWLEDGED")
    clean_env.setenv("WINDY_STORAGE_AUTHORIZED", "true")
    assert make().windy_terms_acknowledged is True


def test_the_old_windy_name_still_works_in_code() -> None:
    s = make(forecast_providers="noaa_gfs,windy", windy_terms_acknowledged=True)
    assert s.windy_storage_authorized is True
    assert s.forecast_provider_list == ["noaa_gfs", "windy"]
    assert make(windy_storage_authorized=True).windy_terms_acknowledged is True


def test_open_meteo_commercial_is_an_alias_of_commercial_use(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("COMMERCIAL_USE", "true")
    assert make().commercial_use is True
    clean_env.setenv("OPEN_METEO_COMMERCIAL", "false")  # the new name wins
    assert make().commercial_use is False
    clean_env.delenv("COMMERCIAL_USE")
    clean_env.setenv("OPEN_METEO_COMMERCIAL", "true")
    assert make().commercial_use is True
    assert make(commercial_use=False).commercial_use is False  # the field name in code


def test_next_public_app_url_is_an_alias_of_app_base_url(clean_env: pytest.MonkeyPatch) -> None:
    assert make().app_base_url == "http://localhost:3000"
    clean_env.setenv("NEXT_PUBLIC_APP_URL", "https://swell.example.test/")
    assert make().app_base_url == "https://swell.example.test"  # trailing slash removed
    clean_env.setenv("APP_BASE_URL", "https://app.example.test")
    assert make().app_base_url == "https://app.example.test"  # APP_BASE_URL wins
    assert make(app_base_url="https://code.example.test").app_base_url == (
        "https://code.example.test"
    )


def test_every_alias_is_an_environment_name() -> None:
    names = env_names()
    assert {
        "WEATHER_PROVIDER",
        "WINDY_STORAGE_AUTHORIZED",
        "WINDY_TERMS_ACKNOWLEDGED",
        "OPEN_METEO_COMMERCIAL",
        "COMMERCIAL_USE",
        "NEXT_PUBLIC_APP_URL",
        "APP_BASE_URL",
    } <= names


# --------------------------------------------------------------------------- defaults
def test_the_new_defaults() -> None:
    s = make()
    assert (s.windy_check_minutes, s.windy_daily_request_limit) == (15, 10_000)
    assert s.forecast_days == 10
    assert s.forecast_refresh_enabled is True
    assert s.forecast_stale_threshold_minutes == 360
    assert s.forecast_stale_threshold_minutes_by_model is None
    assert s.forecast_stale_threshold_by_model == {}
    assert s.forecast_min_valid_fraction == 0.8
    assert s.forecast_version_cdn_seconds == 15
    assert s.sync_coalesce_minutes == 0
    assert s.sync_trigger_list == frozenset({"spots", "map"})
    assert s.sync_daemon_kind_list == frozenset({"spots", "ensemble", "map"})
    assert s.prediction_archive_enabled is False
    assert s.scoring_weights_json is None and s.scoring_weights is None
    assert s.flight_user_daily_quota == 20
    assert (s.alert_lead_default_min_days, s.alert_lead_default_max_days) == (2, 10)
    assert (s.short_notice_min_days, s.short_notice_max_days) == (3.0, 7.0)
    assert (s.detection_lead_min_days, s.detection_lead_max_days) == (1.0, 10.0)
    assert s.quota_alert_fraction == 0.8
    assert s.refresh_scheduler_expected_minutes == 30


# --------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("forecast_days", 2),
        ("forecast_days", 17),
        ("windy_check_minutes", 4),
        ("windy_check_minutes", 361),
        ("windy_daily_request_limit", 0),
        ("forecast_stale_threshold_minutes", -1),
        ("forecast_min_valid_fraction", 0),
        ("forecast_min_valid_fraction", 1.01),
        ("forecast_version_cdn_seconds", -1),
        ("forecast_version_cdn_seconds", 301),
        ("flight_user_daily_quota", 0),
        ("alert_lead_default_min_days", -1),
        ("alert_lead_default_max_days", 17),
        ("short_notice_min_days", -0.5),
        ("short_notice_max_days", 16.5),
        ("quota_alert_fraction", 0),
        ("quota_alert_fraction", 1.5),
        ("refresh_scheduler_expected_minutes", -1),
        ("refresh_scheduler_expected_minutes", 1441),
        ("sync_daemon_kinds", ""),
        ("sync_daemon_kinds", "spots,gfs"),
    ],
)
def test_the_new_ranges_are_validated(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        make(**{field: bad})


def test_the_limits_are_accepted() -> None:
    s = make(
        forecast_days=16,
        forecast_min_valid_fraction=1,
        refresh_scheduler_expected_minutes=0,  # 0 = no scheduler alert
        forecast_version_cdn_seconds=0,
        alert_lead_default_min_days=0,
        alert_lead_default_max_days=16,
        sync_daemon_kinds=" map ",
    )
    assert s.sync_daemon_kind_list == frozenset({"map"})
    assert make(forecast_days=3).forecast_days == 3


def test_ordered_pairs_are_validated() -> None:
    with pytest.raises(ValidationError, match="ALERT_LEAD_DEFAULT_MIN_DAYS"):
        make(alert_lead_default_min_days=11, alert_lead_default_max_days=10)
    with pytest.raises(ValidationError, match="SHORT_NOTICE_MIN_DAYS"):
        make(short_notice_min_days=8, short_notice_max_days=7)
    with pytest.raises(ValidationError, match="DETECTION_LEAD_MIN_DAYS"):
        make(detection_lead_min_days=11)
    assert make(alert_lead_default_min_days=5, alert_lead_default_max_days=5)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[120]", "must be a JSON object"),
        ('{"ncep_gfswave025": -1}', "whole number of minutes"),
        ('{"ncep_gfswave025": 90.5}', "whole number of minutes"),
        ('{"ncep_gfswave025": true}', "whole number of minutes"),
        ('{"ncep_gfswave025": "90"}', "whole number of minutes"),
        ('{" ": 90}', "empty model name"),
    ],
)
def test_the_per_model_stale_thresholds_are_validated(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match="FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL") as e:
        make(forecast_stale_threshold_minutes_by_model=value)
    assert message in str(e.value)


def test_the_per_model_stale_thresholds_override_the_default() -> None:
    s = make(
        forecast_stale_threshold_minutes=300,
        forecast_stale_threshold_minutes_by_model='{"ncep_gfswave025": 120, "gfs": 0}',
    )
    assert s.forecast_stale_threshold_by_model == {"ncep_gfswave025": 120, "gfs": 0}
    assert s.stale_threshold_minutes("ncep_gfswave025") == 120
    assert s.stale_threshold_minutes("gfs") == 0
    assert s.stale_threshold_minutes("ncep_gfs013") == 300
    assert s.stale_threshold_minutes(None) == 300
    assert (
        make(forecast_stale_threshold_minutes_by_model="  ").forecast_stale_threshold_by_model == {}
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{wind: 1}", "not valid JSON"),
        ('[["wind", 0.3]]', "must be a JSON object"),
        ("{}", "at least one component"),
        ('{"wind": -0.1}', "must be a number >= 0"),
        ('{"wind": "0.3"}', "must be a number >= 0"),
        ('{"wind": false}', "must be a number >= 0"),
        ('{"wind": NaN}', "must be a number >= 0"),
        ('{"wind": Infinity}', "must be a number >= 0"),
        ('{"wind": 0, "tide": 0}', "at least one weight above 0"),
    ],
)
def test_scoring_weights_are_validated(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match="SCORING_WEIGHTS_JSON") as e:
        make(scoring_weights_json=value)
    assert message in str(e.value)


def test_scoring_weights_are_parsed() -> None:
    s = make(scoring_weights_json='{"wind": 0.35, "tide": 0, "wave_size": 1}')
    assert s.scoring_weights == {"wind": 0.35, "tide": 0.0, "wave_size": 1.0}
    assert make(scoring_weights_json="").scoring_weights is None


# --------------------------------------------------------------------------- production
def test_production_refuses_windy_until_it_is_eligible() -> None:
    with pytest.raises(ValidationError, match="WEATHER_PROVIDER=windy needs") as no_key:
        production(weather_provider="windy")
    assert "WINDY_API_KEY" in str(no_key.value) and "WINDY_STORAGE_AUTHORIZED" in str(no_key.value)
    with pytest.raises(ValidationError, match="WINDY_STORAGE_AUTHORIZED=true") as keyed:
        production(weather_provider="windy", windy_api_key=WINDY_KEY)
    message = str(keyed.value.errors(include_input=False))
    assert "WINDY_API_KEY and" not in message and WINDY_KEY not in message
    with pytest.raises(ValidationError, match="WINDY_API_KEY"):
        production(weather_provider="windy", windy_storage_authorized=True)
    ok = production(
        weather_provider="windy", windy_api_key=WINDY_KEY, windy_storage_authorized=True
    )
    assert ok.forecast_provider_list == ["windy"]
    # auto never refuses: it uses Open-Meteo until Windy is eligible.
    assert production().forecast_provider_list == ["open_meteo"]
    # Outside production WEATHER_PROVIDER=windy starts, on Open-Meteo.
    assert make(weather_provider="windy").forecast_provider_list == ["open_meteo"]


def test_production_refuses_commercial_open_meteo_without_a_key() -> None:
    with pytest.raises(ValidationError, match="OPEN_METEO_COMMERCIAL=true"):
        production(commercial_use=True)
    with pytest.raises(ValidationError, match="needs OPEN_METEO_API_KEY"):
        production(commercial_use=True, forecast_providers="noaa_gfs,open_meteo_ecmwf")
    assert production(commercial_use=True, open_meteo_api_key=OM_KEY).commercial_use is True
    # No Open-Meteo provider: nothing to refuse.
    assert production(commercial_use=True, weather_provider="demo").forecast_provider_list == [
        "demo"
    ]
    # Outside production it is reported, not refused (the existing rule).
    entry = "OPEN_METEO_API_KEY (COMMERCIAL_USE=true: the free Open-Meteo API is non-commercial)"
    assert entry in make(commercial_use=True).missing_live_credentials()


def test_a_windy_primary_reports_the_fallbacks_commercial_key() -> None:
    entry = "OPEN_METEO_API_KEY (COMMERCIAL_USE=true: the free Open-Meteo API is non-commercial)"
    windy = make(commercial_use=True, windy_api_key=WINDY_KEY, windy_storage_authorized=True)
    assert windy.forecast_provider_list == ["windy"]
    # The runtime fallback (Open-Meteo) could not run commercially without a key.
    assert entry in windy.missing_live_credentials()
    keyed = make(
        commercial_use=True,
        open_meteo_api_key=OM_KEY,
        windy_api_key=WINDY_KEY,
        windy_storage_authorized=True,
    )
    assert entry not in keyed.missing_live_credentials()


def test_an_explicit_windy_entry_without_a_key_is_reported() -> None:
    s = make(forecast_providers="windy", windy_storage_authorized=True)
    assert "WINDY_API_KEY" in s.missing_live_credentials()


# --------------------------------------------------------------------------- profiles
def _profile_settings(*names: str) -> Settings:
    return make(**{k.lower(): v for k, v in profile_env(*names).items()})


def test_the_hosted_profile_derives_one_provider() -> None:
    assert "FORECAST_PROVIDERS" not in HOSTED_PROFILE
    s = _profile_settings("hosted", "free-database")
    assert s.weather_provider == "auto" and s.forecast_provider_list == ["open_meteo"]
    assert s.forecast_blend_enabled is False and s.gefs_enabled is False
    assert s.sync_daemon_kind_list == frozenset({"map"})
    assert s.sync_cpu_budget_hours == 5 and s.forecast_days == 10
    assert s.sync_trigger_list == frozenset({"spots"}) and s.map_field_cycle_hours == [6, 18]
    assert s.prediction_archive_enabled is False
    windy = make(
        **{k.lower(): v for k, v in HOSTED_PROFILE.items()},
        windy_api_key=WINDY_KEY,
        windy_storage_authorized=True,
    )
    assert windy.forecast_provider_list == ["windy"]  # activation is configuration only


def test_the_full_retention_profile_turns_the_archive_back_on() -> None:
    assert {"PREDICTION_ARCHIVE_ENABLED"} == FULL_RETENTION_OVER_DEFAULTS
    assert FULL_RETENTION_PROFILE["PREDICTION_ARCHIVE_ENABLED"] == "true"
    assert FREE_DATABASE_PROFILE["PREDICTION_ARCHIVE_ENABLED"] == "false"
    assert _profile_settings("hosted", "full-retention").prediction_archive_enabled is True
    assert _profile_settings("hosted", "free-database").prediction_archive_enabled is False
