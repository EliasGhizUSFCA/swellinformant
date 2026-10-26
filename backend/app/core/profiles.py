"""Deployment profiles: named sets of environment values, declared and never applied by code.

The Settings defaults (app.core.config) stay those of the classic docker compose stack, so a
deployment that sets nothing behaves as before. A hosted deployment copies these values into
its environment (the Vercel project's environment variables, .env.vercel.example of the
local Vercel-style stack); docs/deploy-vercel.md lists them.

* :data:`HOSTED_PROFILE`: Vercel Hobby with a sync daemon, Neon's pooled URL for the API and
  Upstash Redis. GEFS stays off (the daemon's 10 Active CPU-hours a month); GEFS_ENABLED=true
  turns it on.
* :data:`FREE_DATABASE_PROFILE`: what fits Neon Free. Storage (1 GB): about 1 day of
  forecast history and 1 week of verification archive and job logs, pruned after every
  sync. Egress (5 GB a month): a sync only for a new map cycle (SYNC_TRIGGER_SOURCES=map),
  which also acquires the newest spot runs of both model families, two a day at the 06Z
  and 18Z cycles (MAP_FIELD_CYCLES=06,18). Estimated from the publication lags measured in
  docs/FORECAST_SOURCES.md: Open-Meteo then serves the ECMWF run of that day's 00Z or 12Z,
  which is acquired about 11-12.5 h after its cycle and is 23-24.5 h old at the next sync,
  just under its 24.7 h stale age; with 00Z and 12Z map cycles it would be acquired 17-18.5
  h after its cycle and be stale for about 4-6 h of every 12. Egress measured on the
  replayed 2026-10-16 run (docs/map-layers.md, "Free database budget"): about 1.9 GB a
  month for the syncs and the hourly jobs, against 4.2 GB with a sync for every new spot
  run.
* :data:`FULL_RETENTION_PROFILE`: the retention and sync cadence to restore after upgrading
  the database (Neon Launch). It equals the Settings defaults.

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
        "SYNC_TRIGGER_SOURCES": "map",
        "MAP_FIELD_CYCLES": "06,18",
    }
)

FULL_RETENTION_PROFILE: Mapping[str, str] = MappingProxyType(
    {
        "RETENTION_FORECAST_DAYS": "10",
        "RETENTION_ARCHIVE_DAYS": "730",
        "RETENTION_JOB_LOG_DAYS": "30",
        "SYNC_PRUNE_EACH_PASS": "false",
        "DATA_HEALTH_DATABASE_MAX_GB": "20",
        "SYNC_TRIGGER_SOURCES": "spots,map",
        "MAP_FIELD_CYCLES": "00,12",
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
