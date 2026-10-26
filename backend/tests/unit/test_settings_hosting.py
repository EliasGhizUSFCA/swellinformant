"""Hosting settings (app.core.config, foundation F1): env aliases, database URL
normalisation, validators, derived properties, the new missing-credential entries and the
declared deployment profiles (app.core.profiles)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, normalize_database_url
from app.core.profiles import (
    FREE_DATABASE_PROFILE,
    FULL_RETENTION_PROFILE,
    HOSTED_PROFILE,
    PROFILES,
    profile_env,
)

NEON_POOLED = (
    "postgres://user:pw@ep-x-pooler.us-east-1.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)
NEON_DIRECT = "postgresql://user:pw@ep-x.us-east-1.aws.neon.tech/neondb?sslmode=require"


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No connection variables from the test session or the developer's shell."""
    for name in (
        "REDIS_URL",
        "KV_URL",
        "DATABASE_URL",
        "DATABASE_URL_UNPOOLED",
        "POSTGRES_URL_NON_POOLING",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# --------------------------------------------------------------------------- normalisation
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgres://u:p@h:5432/db?sslmode=require&channel_binding=require",
            "postgresql+psycopg://u:p@h:5432/db?sslmode=require&channel_binding=require",
        ),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        (
            "postgresql+psycopg://u:p@h/db?sslmode=require",
            "postgresql+psycopg://u:p@h/db?sslmode=require",
        ),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_database_url(url: str, expected: str) -> None:
    assert normalize_database_url(url) == expected


def test_database_urls_are_normalised_from_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", NEON_POOLED)
    clean_env.setenv("DATABASE_URL_UNPOOLED", NEON_DIRECT)
    s = make()
    assert s.database_url == "postgresql+psycopg://" + NEON_POOLED.removeprefix("postgres://")
    assert s.database_url.endswith("?sslmode=require&channel_binding=require")
    assert s.database_url_unpooled == (
        "postgresql+psycopg://" + NEON_DIRECT.removeprefix("postgresql://")
    )
    # Normalised when passed in code too.
    assert make(database_url_unpooled="postgres://a@b/c").database_url_unpooled == (
        "postgresql+psycopg://a@b/c"
    )


# --------------------------------------------------------------------------- aliases
def test_kv_url_is_read_when_redis_url_is_unset(clean_env: pytest.MonkeyPatch) -> None:
    assert make().redis_url == "redis://localhost:6379/0"  # default unchanged
    clean_env.setenv("KV_URL", "rediss://default:tok@x.upstash.io:6379")
    assert make().redis_url == "rediss://default:tok@x.upstash.io:6379"
    clean_env.setenv("REDIS_URL", "redis://local:6379/1")
    assert make().redis_url == "redis://local:6379/1"  # REDIS_URL wins
    clean_env.setenv("REDIS_URL", "")  # blank = unset (env_ignore_empty)
    assert make().redis_url == "rediss://default:tok@x.upstash.io:6379"


def test_unpooled_url_aliases(clean_env: pytest.MonkeyPatch) -> None:
    assert make().database_url_unpooled is None
    clean_env.setenv("POSTGRES_URL_NON_POOLING", "postgresql://u@legacy/db")
    assert make().database_url_unpooled == "postgresql+psycopg://u@legacy/db"
    clean_env.setenv("DATABASE_URL_UNPOOLED", "postgresql://u@direct/db")
    assert make().database_url_unpooled == "postgresql+psycopg://u@direct/db"


def test_aliased_fields_accept_their_field_name_in_code(clean_env: pytest.MonkeyPatch) -> None:
    s = make(redis_url="redis://code:6379/2", database_url_unpooled="postgresql://u@d/db")
    assert s.redis_url == "redis://code:6379/2"
    assert s.database_url_unpooled == "postgresql+psycopg://u@d/db"


# --------------------------------------------------------------------------- defaults
def test_new_defaults_keep_the_classic_behaviour(clean_env: pytest.MonkeyPatch) -> None:
    s = make()
    assert s.job_runner == "celery" and s.runs_celery is True
    assert s.db_use_unpooled is False and s.db_pool_recycle_seconds == 1800
    assert s.db_prepared_statements is True
    assert s.redis_connect_timeout_seconds == 5.0 and s.redis_socket_timeout_seconds == 5.0
    assert s.cron_secret is None and s.client_ip_header is None
    assert s.migrate_on_build == "off" and s.commercial_use is False
    assert (
        s.sync_check_minutes,
        s.sync_coalesce_minutes,
        s.sync_lease_minutes,
        s.sync_max_attempts,
        s.sync_retry_minutes,
        s.sync_daemon_poll_seconds,
    ) == (10, 90, 45, 6, 15, 60)
    assert s.sync_trigger_sources == "spots,map"
    assert s.sync_trigger_list == frozenset({"spots", "map"})
    assert s.sync_prune_each_pass is False and s.sync_publish_gate is True
    assert s.sync_daemon_init_db is True and s.sync_cpu_budget_hours is None
    assert s.job_interval_floor_minutes == 0
    # Unchanged existing defaults the hosted profile overrides.
    assert s.gefs_enabled is False and s.retention_forecast_days == 10
    assert s.retention_archive_days == 730 and s.retention_job_log_days == 30


# --------------------------------------------------------------------------- validators
def test_cron_secret_needs_16_characters() -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        make(cron_secret="short")
    assert make(cron_secret="x" * 16).cron_secret == "x" * 16


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("X-Vercel-Forwarded-For", "x-vercel-forwarded-for"),
        ("  cf-connecting-ip ", "cf-connecting-ip"),
        ("", None),
        (None, None),
    ],
)
def test_client_ip_header_is_lower_cased(value: str | None, expected: str | None) -> None:
    assert make(client_ip_header=value).client_ip_header == expected


@pytest.mark.parametrize("value", ["x_forwarded_for", "x-real-ip:", "x forwarded", "x-ip\nx-other"])
def test_client_ip_header_rejects_non_header_names(value: str) -> None:
    with pytest.raises(ValidationError, match="CLIENT_IP_HEADER"):
        make(client_ip_header=value)


def test_sync_trigger_sources_are_a_non_empty_known_subset() -> None:
    assert make(sync_trigger_sources="spots").sync_trigger_list == frozenset({"spots"})
    assert make(sync_trigger_sources=" map , ensemble,spots ").sync_trigger_list == frozenset(
        {"spots", "map", "ensemble"}
    )
    with pytest.raises(ValidationError, match="at least one"):
        make(sync_trigger_sources=" , ")
    with pytest.raises(ValidationError, match="Unknown SYNC_TRIGGER_SOURCES"):
        make(sync_trigger_sources="spots,gefs")


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("sync_check_minutes", 4),
        ("sync_check_minutes", 181),
        ("sync_coalesce_minutes", -1),
        ("sync_coalesce_minutes", 361),
        ("sync_lease_minutes", 9),
        ("sync_lease_minutes", 241),
        ("sync_max_attempts", 0),
        ("sync_max_attempts", 51),
        ("sync_retry_minutes", 4),
        ("sync_retry_minutes", 361),
        ("sync_daemon_poll_seconds", 9),
        ("sync_daemon_poll_seconds", 601),
        ("sync_cpu_budget_hours", 0),
        ("job_interval_floor_minutes", -1),
        ("job_interval_floor_minutes", 1441),
        ("db_pool_recycle_seconds", 29),
        ("redis_connect_timeout_seconds", 0),
        ("redis_socket_timeout_seconds", 31),
        ("job_runner", "lambda"),
        ("migrate_on_build", "preview"),
    ],
)
def test_ranges_and_choices_are_validated(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        make(**{field: bad})


# --------------------------------------------------------------------------- properties
def test_engine_and_migration_urls() -> None:
    pooled = "postgresql+psycopg://u@pooler/db"
    direct = "postgresql+psycopg://u@direct/db"
    only_pooled = make(database_url=pooled)
    assert only_pooled.engine_database_url == pooled
    assert only_pooled.migration_database_url == pooled
    both = make(database_url=pooled, database_url_unpooled=direct)
    assert both.engine_database_url == pooled  # the API stays on the pooler
    assert both.migration_database_url == direct
    daemon = make(database_url=pooled, database_url_unpooled=direct, db_use_unpooled=True)
    assert daemon.engine_database_url == direct
    # DB_USE_UNPOOLED without an unpooled URL falls back to DATABASE_URL.
    assert make(database_url=pooled, db_use_unpooled=True).engine_database_url == pooled


def test_runs_celery_follows_job_runner() -> None:
    assert make(job_runner="celery").runs_celery is True
    assert make(job_runner="daemon").runs_celery is False


# --------------------------------------------------------------------------- credentials
def production(**kw: object) -> Settings:
    return make(
        app_env="production",
        enable_dev_endpoints=False,
        secret_key="s" * 40,
        cookie_secure=True,
        email_provider="resend",
        resend_api_key="re_x",
        **kw,
    )


def test_daemon_in_production_lists_the_cron_secret_but_still_starts() -> None:
    # No new hard refusal: the settings load, the missing secret is reported.
    s = production(job_runner="daemon")
    assert "CRON_SECRET (daily watchdog)" in s.missing_live_credentials()
    assert (
        "CRON_SECRET (daily watchdog)"
        not in production(job_runner="daemon", cron_secret="c" * 32).missing_live_credentials()
    )
    assert not any("CRON_SECRET" in m for m in production().missing_live_credentials())
    assert not any("CRON_SECRET" in m for m in make(job_runner="daemon").missing_live_credentials())


def test_commercial_use_needs_an_open_meteo_key() -> None:
    entry = "OPEN_METEO_API_KEY (COMMERCIAL_USE=true: the free Open-Meteo API is non-commercial)"
    for providers in ("open_meteo", "open_meteo_ecmwf", "noaa_gfs,open_meteo_ecmwf"):
        commercial = make(forecast_providers=providers, commercial_use=True)
        assert entry in commercial.missing_live_credentials(), providers
        keyed = make(forecast_providers=providers, commercial_use=True, open_meteo_api_key="k")
        assert entry not in keyed.missing_live_credentials(), providers
        assert entry not in make(forecast_providers=providers).missing_live_credentials()
    assert (
        entry
        not in make(forecast_providers="noaa_gfs", commercial_use=True).missing_live_credentials()
    )


# --------------------------------------------------------------------------- profiles
def _env_names() -> dict[str, str]:
    """Environment variable name -> field name, for every Settings field."""
    return {name.upper(): name for name in Settings.model_fields}


def _same(got: object, value: str) -> bool:
    """A settings value equals the env string a profile writes."""
    if isinstance(got, bool):
        return got is (value == "true")
    if isinstance(got, int | float):
        return float(got) == float(value)
    return str(got) == value


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_every_profile_entry_is_a_setting_that_validates(
    name: str, clean_env: pytest.MonkeyPatch
) -> None:
    fields = _env_names()
    profile = PROFILES[name]
    assert set(profile) <= set(fields), set(profile) - set(fields)
    for key, value in profile.items():
        clean_env.setenv(key, value)
    s = make()
    for key, value in profile.items():
        got = getattr(s, fields[key])
        assert _same(got, value), (key, got)


def test_profiles_are_declared_not_default(clean_env: pytest.MonkeyPatch) -> None:
    defaults = make()
    fields = _env_names()
    # The full-retention profile is exactly today's defaults (restored after upgrading).
    for key, value in FULL_RETENTION_PROFILE.items():
        assert _same(getattr(defaults, fields[key]), value), key
    assert set(FULL_RETENTION_PROFILE) == set(FREE_DATABASE_PROFILE)
    # The free-database values differ from the defaults: declared, not applied.
    for key, value in FREE_DATABASE_PROFILE.items():
        assert not _same(getattr(defaults, fields[key]), value), key
    assert HOSTED_PROFILE["JOB_RUNNER"] == "daemon" and defaults.job_runner == "celery"
    # Lead decision: GEFS stays off in the hosted profile.
    assert HOSTED_PROFILE["GEFS_ENABLED"] == "false"


def test_profile_env_merges_in_order() -> None:
    env = profile_env("hosted", "free-database")
    assert env["JOB_RUNNER"] == "daemon" and env["RETENTION_FORECAST_DAYS"] == "1"
    assert profile_env("free-database", "full-retention")["RETENTION_FORECAST_DAYS"] == "10"
    with pytest.raises(KeyError, match="unknown profile"):
        profile_env("pro")
