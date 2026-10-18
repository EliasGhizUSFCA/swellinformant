"""Deployment profiles: named sets of environment values, declared and never applied by code.

The Settings defaults (app.core.config) stay those of the classic docker compose stack, so a
deployment that sets nothing behaves as before. A hosted deployment copies these values into
its environment (the Vercel project's environment variables, .env.vercel.example of the
local Vercel-style stack); docs/deploy-vercel.md lists them.

* :data:`HOSTED_PROFILE`: Vercel Hobby with a sync daemon, Neon's pooled URL for the API and
  Upstash Redis. GEFS stays off (the daemon's 10 Active CPU-hours a month); GEFS_ENABLED=true
  turns it on.
* :data:`FREE_DATABASE_PROFILE`: retention that fits Neon Free (1 GB): about 1 day of
  forecast history and 1 week of verification archive and job logs, pruned after every sync.
* :data:`FULL_RETENTION_PROFILE`: the retention to restore after upgrading the database
  (Neon Launch). It equals the Settings defaults.

Keys are environment variable names (as the deployment sets them), values are the strings
the deployment writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

HOSTED_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        "JOB_RUNNER": "daemon",
        "MIGRATE_ON_BUILD": "production",
        "CLIENT_IP_HEADER": "x-vercel-forwarded-for",
        "DB_POOL_SIZE": "2",
        "DB_MAX_OVERFLOW": "3",
        "DB_POOL_RECYCLE_SECONDS": "240",
        "DB_PREPARED_STATEMENTS": "false",
        "REDIS_CONNECT_TIMEOUT_SECONDS": "1.5",
        "REDIS_SOCKET_TIMEOUT_SECONDS": "2",
        "FORECAST_PROVIDERS": "open_meteo,open_meteo_ecmwf",
        "MAP_FIELDS_ENABLED": "true",
        "PROVIDER_RESPONSE_CACHE_ENABLED": "false",
        "GEFS_ENABLED": "false",
        "JOB_INTERVAL_FLOOR_MINUTES": "60",
        "DATA_HEALTH_CHECK_MINUTES": "60",
        "SYNC_CPU_BUDGET_HOURS": "10",
        "COMMERCIAL_USE": "false",
    }
)

FREE_DATABASE_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        "RETENTION_FORECAST_DAYS": "1",
        "RETENTION_ARCHIVE_DAYS": "7",
        "RETENTION_JOB_LOG_DAYS": "7",
        "SYNC_PRUNE_EACH_PASS": "true",
        "DATA_HEALTH_DATABASE_MAX_GB": "0.85",
    }
)

FULL_RETENTION_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        "RETENTION_FORECAST_DAYS": "10",
        "RETENTION_ARCHIVE_DAYS": "730",
        "RETENTION_JOB_LOG_DAYS": "30",
        "SYNC_PRUNE_EACH_PASS": "false",
        "DATA_HEALTH_DATABASE_MAX_GB": "20",
    }
)

PROFILES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "hosted": HOSTED_PROFILE,
        "free-database": FREE_DATABASE_PROFILE,
        "full-retention": FULL_RETENTION_PROFILE,
    }
)


def profile_env(*names: str) -> dict[str, str]:
    """The environment of the named profiles merged in order (later names win).

    ``profile_env("hosted", "free-database")`` is the free hosted setup.
    """
    env: dict[str, str] = {}
    for name in names:
        try:
            env.update(PROFILES[name])
        except KeyError:
            raise KeyError(f"unknown profile {name!r}; known: {sorted(PROFILES)}") from None
    return env
