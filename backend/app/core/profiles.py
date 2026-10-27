"""Deployment profiles: named sets of environment values, declared and never applied by code.

The Settings defaults (app.core.config) stay those of the classic docker compose stack, so a
deployment that sets nothing behaves as before, except the Swell 2.0 defaults (design B):
WEATHER_PROVIDER=auto (Open-Meteo until Windy is eligible: the compose stacks, .env.example
and the tests set demo explicitly), FORECAST_DAYS=10, SYNC_COALESCE_MINUTES=0,
DETECTION_LEAD_MIN_DAYS=1 and the prediction archive off. A hosted deployment copies these
values into its environment (the Vercel project's environment variables, .env.vercel.example
of the local Vercel-style stack); docs/deploy-vercel.md lists them.

* :data:`HOSTED_PROFILE`: Vercel Hobby, Neon's pooled URL for the API and Upstash Redis
  (Swell 2.0, design D2/D5/D8). One weather provider chosen by WEATHER_PROVIDER=auto (Windy
  once it is eligible, else Open-Meteo GFS + GFS-Wave; FORECAST_PROVIDERS is not set), no
  blend, no GEFS ensemble, a 10-day horizon. The spot refreshes run in a Vercel Function
  (GET|POST /api/internal/refresh, called by a scheduler); the optional sync daemon only
  refreshes the GRIB map layers (SYNC_DAEMON_KINDS=map) within Hobby's 5 Active CPU-hours
  a month (SYNC_CPU_BUDGET_HOURS=5).
* :data:`FREE_DATABASE_PROFILE`: what fits Neon Free. Storage (1 GB): about 1 day of
  forecast history and 1 week of verification archive and job logs, pruned after every
  sync, and no prediction archive. Syncs: a new spot model run triggers one
  (SYNC_TRIGGER_SOURCES=spots: the website follows every GFS run, spec section 9); the map
  layers keep the 06Z and 18Z cycles (MAP_FIELD_CYCLES=06,18) for the optional overlay. The
  Upgrade 4 cadence (SYNC_TRIGGER_SOURCES=map: two syncs a day, about 1.9 GB of egress a
  month measured on the replayed 2026-10-16 run, docs/map-layers.md) stays available. The
  egress of a spot-only refresh of the single provider is not measured here yet (the
  refresh package measures it with scripts/measure_db_egress.py).
* :data:`FULL_RETENTION_PROFILE`: the retention and sync cadence to restore after upgrading
  the database (Neon Launch). It equals the Settings defaults, except that it turns the
  prediction archive on (PREDICTION_ARCHIVE_ENABLED=true; off by default, design D5).

Keys are environment variable names (as the deployment sets them), values are the strings
the deployment writes. COMMERCIAL_USE keeps its old name here on purpose: OPEN_METEO_COMMERCIAL
is the same setting and wins when both are set, so an owner who later sets
OPEN_METEO_COMMERCIAL=true next to the pasted COMMERCIAL_USE=false gets the commercial rule.
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
        "WEATHER_PROVIDER": "auto",
        "FORECAST_DAYS": "10",
        "FORECAST_BLEND_ENABLED": "false",
        "MAP_FIELDS_ENABLED": "true",
        "PROVIDER_RESPONSE_CACHE_ENABLED": "false",
        "GEFS_ENABLED": "false",
        "JOB_INTERVAL_FLOOR_MINUTES": "60",
        "DATA_HEALTH_CHECK_MINUTES": "60",
        "SYNC_DAEMON_KINDS": "map",
        "SYNC_CPU_BUDGET_HOURS": "5",
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
        "SYNC_TRIGGER_SOURCES": "spots",
        "MAP_FIELD_CYCLES": "06,18",
        "PREDICTION_ARCHIVE_ENABLED": "false",
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
        "PREDICTION_ARCHIVE_ENABLED": "true",
    }
)

# Settings whose default is the lean value and which the full-retention profile turns back
# on: the one place where that profile differs from the Settings defaults.
FULL_RETENTION_OVER_DEFAULTS = frozenset({"PREDICTION_ARCHIVE_ENABLED"})

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
