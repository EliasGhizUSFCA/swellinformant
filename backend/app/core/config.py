"""Application settings, loaded exclusively from environment variables (or a .env file).

Every secret (database password, API keys, provider tokens) is read here and nowhere
else. Nothing in this module is ever sent to the frontend.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

FlightProviderName = Literal["duffel", "amadeus", "demo"]
# Who runs the background jobs: "celery" (worker + beat, classic docker compose) or "daemon"
# (one long-lived `python -m app.cli sync daemon` process, e.g. a Vercel Daemon).
JobRunnerName = Literal["celery", "daemon"]
# Components whose new model run makes a sync due (SYNC_TRIGGER_SOURCES), and the kinds the
# sync daemon refreshes itself (SYNC_DAEMON_KINDS).
SYNC_TRIGGER_TOKENS = frozenset({"spots", "map", "ensemble"})
# Open-Meteo providers: their free API is for non-commercial use only (COMMERCIAL_USE).
OPEN_METEO_PROVIDER_TOKENS = frozenset({"open_meteo", "open_meteo_ecmwf"})
_HEADER_NAME = re.compile(r"^[a-z0-9-]+$")
EmailProviderName = Literal["console", "mailpit", "smtp", "resend", "sendgrid"]
# FORECAST_PROVIDERS tokens. "open_meteo" is the Open-Meteo GFS family, "open_meteo_ecmwf"
# the Open-Meteo ECMWF family; "windy" also needs WINDY_STORAGE_AUTHORIZED (formerly
# WINDY_TERMS_ACKNOWLEDGED).
FORECAST_PROVIDER_TOKENS = frozenset(
    {"demo", "noaa_gfs", "ecmwf_ifs", "open_meteo", "open_meteo_ecmwf", "windy"}
)
# WEATHER_PROVIDER: the simple provider choice (design D2). FORECAST_PROVIDERS, when set, is
# the advanced override; otherwise Settings.forecast_provider_list is derived from this.
WeatherProviderName = Literal["auto", "open_meteo", "windy", "demo"]
# Where the effective provider list came from (ProviderSelection.source).
SelectionSource = Literal["FORECAST_PROVIDERS", "WEATHER_PROVIDER"]
# The provider a Windy primary falls back to at runtime (design D4).
WINDY_FALLBACK_PROVIDER = "open_meteo"
# Providers that capture mail locally for development: nothing reaches a real mailbox.
CAPTURE_EMAIL_PROVIDERS = frozenset({"console", "mailpit"})
SmsProviderName = Literal["console", "twilio", "disabled"]
# SYNC_LEASE_MINUTES when it is not set and JOB_RUNNER=daemon (see Settings).
DAEMON_LEASE_MINUTES = 15


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # `KEY=` (blank, as in .env.example or `${KEY:-}` in compose) means "use the default".
        env_ignore_empty=True,
        # Fields with an env alias (REDIS_URL / KV_URL, DATABASE_URL_UNPOOLED /
        # POSTGRES_URL_NON_POOLING) still accept their field name in code: Settings(redis_url=).
        validate_by_name=True,
    )

    # ------------------------------------------------------------------ general
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Swell Travel Agent"
    log_level: str = "INFO"
    # Public URL of the web app (used in email links). No trailing slash. NEXT_PUBLIC_APP_URL
    # (the name the frontend build reads for its metadataBase) is read too; APP_BASE_URL wins
    # when both are set.
    app_base_url: str = Field(
        default="http://localhost:3000",
        validation_alias=AliasChoices("APP_BASE_URL", "NEXT_PUBLIC_APP_URL"),
    )
    # Public URL of the API (used for webhook signature validation). No trailing slash.
    api_base_url: str = "http://localhost:8000"
    secret_key: str = Field(default="dev-insecure-secret-change-me", min_length=16)
    enable_dev_endpoints: bool = False

    # ------------------------------------------------------------------ infrastructure
    # postgres:// and postgresql:// URLs (as Neon and other hosts hand them out) are rewritten
    # to the psycopg driver, keeping the query string (normalize_database_url).
    database_url: str = "postgresql+psycopg://swell:swell@localhost:5432/swell"
    # A direct (not pooled) connection to the same database, for the sync daemon, the CLI and
    # migrations. Vercel's Neon integration sets DATABASE_URL_UNPOOLED (older:
    # POSTGRES_URL_NON_POOLING). None = DATABASE_URL is used for everything.
    database_url_unpooled: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL_UNPOOLED", "POSTGRES_URL_NON_POOLING"),
    )
    # true = this process connects through DATABASE_URL_UNPOOLED (engine_database_url); the
    # daemon image sets it. Migrations always prefer the unpooled URL (migration_database_url).
    db_use_unpooled: bool = False
    # Connections older than this are replaced (a pooler or a scaled-to-zero database closes
    # idle connections on its side).
    db_pool_recycle_seconds: int = Field(default=1800, ge=30)
    # false = psycopg never prepares statements (prepare_threshold=None), for a PgBouncer in
    # transaction mode that does not support them.
    db_prepared_statements: bool = True
    # REDIS_URL wins over KV_URL (the name Vercel's Upstash integration sets, rediss://).
    redis_url: str = Field(
        default="redis://localhost:6379/0", validation_alias=AliasChoices("REDIS_URL", "KV_URL")
    )
    redis_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    redis_socket_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False
    # When celery beat starts, queue the sync check (sync.tick) and, with TIDE_PROVIDERS, the
    # tide job (instead of waiting an interval).
    beat_run_on_start: bool = True
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ------------------------------------------------------------------ runner and deployment
    # celery: Celery worker + beat run the jobs (classic docker compose). daemon: one
    # `python -m app.cli sync daemon` process runs them, and the API never imports Celery.
    job_runner: JobRunnerName = "celery"
    # Bearer token of the daily watchdog (GET /api/internal/cron/daily; Vercel Cron sends
    # "Authorization: Bearer $CRON_SECRET"). None = the route answers 404.
    cron_secret: str | None = Field(default=None, min_length=16)
    # Request header that carries the client IP, set by the platform in front of the API and
    # not overwritable by clients (e.g. x-vercel-forwarded-for). None = X-Forwarded-For with
    # TRUSTED_PROXY_COUNT. Lower case.
    client_ip_header: str | None = None
    # `python -m app.cli migrate --on-build`: off = never; production = only when
    # VERCEL_ENV=production; all = every build (also Neon preview branches).
    migrate_on_build: Literal["off", "production", "all"] = "off"
    # true once the site is used commercially (a paywall, ads): the free Open-Meteo API is
    # non-commercial, so an Open-Meteo provider then needs OPEN_METEO_API_KEY (production
    # refuses to start without it). OPEN_METEO_COMMERCIAL is the same setting and wins when
    # both names are set.
    commercial_use: bool = Field(
        default=False, validation_alias=AliasChoices("OPEN_METEO_COMMERCIAL", "COMMERCIAL_USE")
    )

    # ------------------------------------------------------------------ sync
    # Model-run checks of the sync daemon (provider metadata only, no database access).
    sync_check_minutes: int = Field(default=10, ge=5, le=180)
    # Late model domains of one component (e.g. the second wave grid of a family) are waited
    # for this long after the first sighting, then whatever is available is synced. Applies
    # within a component only: a new wave run never waits for the atmosphere or the map
    # (design D6). 0 = no waiting.
    sync_coalesce_minutes: int = Field(default=0, ge=0, le=360)
    # The sync lease (a sync_runs row with status running), renewed after every stage and
    # extended every third of it while a stage runs, so it lapses only when the process
    # holding it died; the next sync then takes it over. Not set: DAEMON_LEASE_MINUTES (15)
    # under JOB_RUNNER=daemon, where a deploy can kill the daemon mid-sync (Vercel sends
    # SIGKILL once terminationGracePeriodSeconds, 600 s in vercel.json, have passed after
    # SIGTERM) and its successor should take over soon; 45 under Celery.
    sync_lease_minutes: int = Field(default=45, ge=10, le=240)
    # Failed attempts per model run before the sync waits for the next run.
    sync_max_attempts: int = Field(default=6, ge=1, le=50)
    sync_retry_minutes: int = Field(default=15, ge=5, le=360)
    # Components whose new model run makes a sync due: a non-empty subset of spots, map,
    # ensemble (sync_trigger_list).
    sync_trigger_sources: str = "spots,map"
    # Component kinds the sync daemon refreshes itself: a non-empty subset of spots, ensemble,
    # map (sync_daemon_kind_list). The hosted profile: map (the spot refreshes run in a Vercel
    # Function, GET|POST /api/internal/refresh, called by a scheduler).
    sync_daemon_kinds: str = "spots,ensemble,map"
    # Prune superseded predictions and map fields after every sync, not only daily.
    sync_prune_each_pass: bool = False
    # After a publishing sync, compute the public spot list and conditions at once (about
    # 1.4 MB of database reads per publication on the 50-spot live data) instead of at the
    # first visitor's request; worth it only when visitors come every hour.
    sync_prewarm_public_cache: bool = False
    # The map index serves the run of the newest published sync (falls back to the newest
    # servable run when nothing was published).
    sync_publish_gate: bool = True
    # How often the daemon picks up kicks ("match my search", "refresh flights").
    sync_daemon_poll_seconds: int = Field(default=60, ge=10, le=600)
    # The daemon migrates, seeds and loads the tide catalog when it starts (idempotent).
    sync_daemon_init_db: bool = True
    # At start the daemon runs, once, every periodic job that has not succeeded within its
    # longest gap (or never): a new deployment gets tide predictions at once instead of at
    # the next 02:20 UTC, and frequent restarts never starve the hourly jobs.
    sync_daemon_catch_up: bool = True
    # Monthly CPU budget of the sync runner in hours (e.g. 5 = Vercel Hobby's daemon quota);
    # health warns near it. None = no budget.
    sync_cpu_budget_hours: float | None = Field(default=None, gt=0)
    # Every periodic job runs at most this often (minutes; 0 = the schedule as written), so
    # a scale-to-zero database is woken at most this often between syncs.
    job_interval_floor_minutes: int = Field(default=0, ge=0, le=1440)

    # ------------------------------------------------------------------ http/security
    cors_origins: str = "http://localhost:3000"
    session_cookie_name: str = "sta_session"
    csrf_cookie_name: str = "sta_csrf"
    session_ttl_days: int = 14
    cookie_secure: bool = False
    cookie_domain: str | None = None
    rate_limit_enabled: bool = True
    # Requests of one client IP an hour that miss the public snapshot cache and so read the
    # database (app.services.public_cache); more answer 429. Cache hits are not counted.
    public_cache_miss_limit_per_hour: int = Field(default=120, ge=1, le=100_000)
    login_max_failures: int = 8
    login_lockout_minutes: int = 15
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60
    # Proxies in front of the API that set or append X-Forwarded-For (app.core.ratelimit).
    # The Next.js /api proxy forwards the header unchanged and adds no entry (verified with
    # Next.js 16.4), so it does not count; 0 = use the socket peer.
    trusted_proxy_count: int = 1

    # ------------------------------------------------------------------ forecasts
    # The weather provider (design D2): auto = Windy when it is eligible (WINDY_API_KEY set
    # and WINDY_STORAGE_AUTHORIZED=true), else Open-Meteo; open_meteo; windy (production
    # refuses it until Windy is eligible; elsewhere Open-Meteo is used until then); demo =
    # synthetic data. Settings.provider_selection() says what was chosen and why.
    weather_provider: WeatherProviderName = "auto"
    # ADVANCED override, comma separated, in priority order (the first provider is the primary
    # source). Tokens (FORECAST_PROVIDER_TOKENS): demo, noaa_gfs, ecmwf_ifs, open_meteo
    # (Open-Meteo, GFS family), open_meteo_ecmwf (Open-Meteo, ECMWF family) and windy (only
    # with WINDY_STORAGE_AUTHORIZED, see the validator). None = derived from WEATHER_PROVIDER
    # (forecast_provider_list).
    forecast_providers: str | None = None
    # The rolling forecast horizon (days); providers with a shorter one are capped.
    forecast_days: int = Field(default=10, ge=3, le=16)
    # Kill switch of every refresh runner (the refresh endpoint, the daemon, Celery): false =
    # nothing is acquired, the published data stay as they are.
    forecast_refresh_enabled: bool = True
    # A spot or component is stale once its last successful update is older than its model's
    # update interval plus this grace (minutes; design D10). Per-model overrides:
    # FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL, a JSON object {"<model>": minutes}.
    forecast_stale_threshold_minutes: int = Field(default=360, ge=0, le=10_080)
    forecast_stale_threshold_minutes_by_model: str | None = None
    # A spot passes validation before staging only when at least this fraction of its hours
    # carry a wave height and a wind speed (design D9).
    forecast_min_valid_fraction: float = Field(default=0.8, gt=0, le=1)
    # CDN cache of GET /api/forecasts/version (s-maxage seconds; browsers never cache it).
    forecast_version_cdn_seconds: int = Field(default=15, ge=0, le=300)
    forecast_refresh_minutes: int = 180
    forecast_stale_hours: int = 18
    # Spots per Open-Meteo request (one weighted call per location either way; 50 = every
    # seeded spot in one request per model, fewer round trips).
    forecast_batch_size: int = Field(default=50, ge=1, le=50)
    forecast_http_timeout_seconds: float = 30.0
    forecast_daily_request_quota: int = 5000
    open_meteo_api_key: str | None = None
    # Deprecated aliases, read only by the single-model Open-Meteo adapter: when set
    # explicitly they override OPEN_METEO_GFS_WAVE_MODELS / OPEN_METEO_GFS_ATMOS_MODEL (see
    # open_meteo_gfs_wave_model_list and open_meteo_gfs_atmos_model_name).
    open_meteo_wave_model: str = "ncep_gfswave025"
    open_meteo_atmos_model: str = "gfs_seamless"
    # Open-Meteo, GFS family (FORECAST_PROVIDERS token "open_meteo"). Wave models in routing
    # order: ncep_gfswave016 covers only about 52.5N..15S (HTTP 400 "No data is available for
    # this location" outside), ncep_gfswave025 is global; one model per request.
    open_meteo_gfs_wave_models: str = "ncep_gfswave016,ncep_gfswave025"
    # One model run per family. "gfs_seamless" (HRRR near the US, GFS elsewhere) is also
    # accepted; the run then records both init times (RunInfo.model_runs).
    open_meteo_gfs_atmos_model: str = "gfs_global"
    # Open-Meteo, ECMWF family (token "open_meteo_ecmwf").
    open_meteo_ecmwf_wave_model: str = "ecmwf_wam025"
    open_meteo_ecmwf_atmos_model: str = "ecmwf_ifs025"
    # Grid cell Open-Meteo samples for a coordinate: "sea" picks the nearest sea cell (wind
    # over water, as at a break), "land" its default land preference, "nearest" either.
    open_meteo_cell_selection: Literal["sea", "nearest", "land"] = "sea"
    # A new model run is used only this long after Open-Meteo reports it available (meta.json).
    open_meteo_availability_wait_minutes: int = Field(default=10, ge=0, le=180)
    # Keep only the model's native output steps (no Open-Meteo-interpolated hours).
    open_meteo_native_steps_only: bool = True
    # Call budgets (free tier, Open-Meteo pricing page): weighted calls, not HTTP requests.
    open_meteo_calls_per_minute: int = Field(default=600, ge=1)
    open_meteo_calls_per_hour: int = Field(default=5000, ge=1)
    open_meteo_calls_per_day: int = Field(default=10000, ge=1)
    open_meteo_calls_per_month: int = Field(default=300000, ge=1)
    # Windy's terms forbid storing its data and derived databases: Windy data are stored,
    # scored and published only once the operator confirms a licence that allows this use
    # (docs/API_SETUP_REQUIRED.md). WINDY_TERMS_ACKNOWLEDGED is the old name, still read (the
    # new name wins when both are set); Settings.windy_terms_acknowledged still answers.
    windy_storage_authorized: bool = Field(
        default=False,
        validation_alias=AliasChoices("WINDY_STORAGE_AUTHORIZED", "WINDY_TERMS_ACKNOWLEDGED"),
    )
    # Minutes between Windy's sentinel-spot checks for a new run (Windy has no run metadata).
    windy_check_minutes: int = Field(default=15, ge=5, le=360)
    # Windy Point Forecast requests a UTC day (Redis counter, reset 00:00 UTC).
    windy_daily_request_limit: int = Field(default=10_000, ge=1)
    # Provider HTTP response cache in Redis (TTL = the model update interval). Bodies larger
    # than the byte limit are not cached; at most MAX_KEYS entries.
    provider_response_cache_enabled: bool = True
    provider_response_cache_max_bytes: int = Field(default=2_000_000, ge=0)
    provider_response_cache_max_keys: int = Field(default=200, ge=0)

    # NOAA open data on AWS (direct GRIB2 by byte range; no key needed).
    # Bandwidth (measured): ~24 MB per forecast step for all 50 spots (15 MB without the
    # global.0p25 fallback grid), ~2 GB per 10-day run at 3-hourly steps
    # (docs/noaa-direct-ingestion.md).
    noaa_gfs_base_url: str = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
    # File layout below the bucket (GFS v16). GFS v17 is expected to rename files, so the
    # layout is configuration: placeholders {ymd}, {hh}, {grid}, {fhour:03d}.
    noaa_gfs_wave_path_template: str = (
        "gfs.{ymd}/{hh}/wave/gridded/gfswave.t{hh}z.{grid}.f{fhour:03d}.grib2"
    )
    noaa_gfs_atmos_path_template: str = "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fhour:03d}"
    noaa_gefs_base_url: str = "https://noaa-gefs-pds.s3.amazonaws.com"
    noaa_step_hours: int = Field(default=3, ge=1, le=6)
    noaa_max_lead_hours: int = Field(default=240, ge=24, le=384)
    noaa_availability_lag_hours: float = 5.0
    # GEFS-Wave ensemble mean/spread (uncertainty); its own job, off by default.
    gefs_enabled: bool = False
    gefs_step_hours: int = Field(default=6, ge=3, le=12)
    gefs_max_lead_hours: int = Field(default=240, ge=24, le=384)
    gefs_availability_lag_hours: float = 7.5
    # GEFS-Wave mean/spread records read per step, and the cycles read (UTC hours).
    gefs_stat_fields: str = "HTSGW,PERPW"
    gefs_cycles: str = "00,06,12,18"
    # ECMWF open data (CC BY 4.0, attribution required).
    ecmwf_base_url: str = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
    ecmwf_max_lead_hours: int = Field(default=240, ge=24, le=360)
    ecmwf_availability_lag_hours: float = 8.0
    # Wave parameters read from ECMWF open data (comma separated GRIB short names).
    ecmwf_wave_params: str = "swh,mwd,pp1d,mwp"
    # Shared by the GRIB ingesters.
    grib_download_workers: int = Field(default=6, ge=1, le=16)
    grib_max_cell_distance_km: float = 60.0

    # ------------------------------------------------------------------ blending
    # Combine the latest run of each configured source into one forecast (see
    # app.services.forecasts.blend). Off = use the highest-priority source only.
    forecast_blend_enabled: bool = True

    # ------------------------------------------------------------------ observations
    # Comma separated: aodn, metoffice, ndbc (NDBC needs www.ndbc.noaa.gov reachable).
    observation_providers: str = "aodn,metoffice,ndbc"
    observations_enabled: bool = False
    observation_lookback_hours: int = Field(default=48, ge=1, le=24 * 31)
    aodn_base_url: str = "https://aodn-cloud-optimised.s3.ap-southeast-2.amazonaws.com"
    metoffice_base_url: str = (
        "https://met-office-marine-observations-data.s3.eu-west-2.amazonaws.com"
    )
    ndbc_base_url: str = "https://www.ndbc.noaa.gov"
    # Read NDBC's 5-day files (5day2) when the look-back window fits in them.
    ndbc_use_5day: bool = True
    # Quality-controlled buoy observations are kept this long (independent of the archive).
    observation_retention_days: int = Field(default=3650, ge=1)

    # ------------------------------------------------------------------ tides
    # Comma separated, in priority order: noaa_coops (NOAA CO-OPS predictions, US stations),
    # harmonic_model (committed harmonic constants evaluated locally). Empty = no tides, and
    # the daily tides.acquire job is not scheduled.
    tide_providers: str = "noaa_coops,harmonic_model"
    noaa_coops_base_url: str = "https://api.tidesandcurrents.noaa.gov"
    # The "application" parameter CO-OPS asks callers to send; empty = APP_NAME.
    noaa_coops_application: str = ""
    tide_model: Literal["eot20", "fes2022"] = "eot20"
    # Harmonic constants per spot (relative to the backend working directory or absolute).
    tide_constants_path: str = "data/tides/constants_eot20.json"
    tide_horizon_days: int = Field(default=16, ge=1, le=31)
    # Past tide predictions kept (the future ones always are).
    tide_retention_past_days: int = Field(default=2, ge=0)

    # ------------------------------------------------------------------ verification & ML
    verification_enabled: bool = False
    # Forecast sources archived at observation stations for verification.
    point_forecast_sources: str = "gfswave_bulletin"
    ml_artifact_dir: str = "/tmp/swell-ml"  # noqa: S108  (use a persistent volume in production)
    # Optional, off by default (spec 2: no ML in the default path): apply an ACTIVE ML
    # correction (only models that passed the deployment criteria).
    ml_corrections_enabled: bool = False
    # Archive every published prediction at each lead time (verification and ML training
    # data; app.services.forecasts.archive). Off by default (design D5); the full-retention
    # profile turns it on.
    prediction_archive_enabled: bool = False
    # Map ocean layers (downsampled GFS-Wave + GFS global grids). None = automatic, decided by
    # app.services.map_fields.map_fields_mode (docs/map-layers.md); True = always fetch them;
    # False = off.
    map_fields_enabled: bool | None = None
    map_field_resolution_deg: float = Field(default=0.5, ge=0.25, le=2.0)
    map_field_max_lead_hours: int = Field(default=240, ge=24, le=384)
    # GFS atmosphere grid read for the map's wind and pressure in standalone mode, and its
    # file layout ({grid} is MAP_FIELD_ATMOS_GRID; other placeholders as in
    # NOAA_GFS_ATMOS_PATH_TEMPLATE). app.services.forecasts.noaa.GfsPaths fills it in.
    map_field_atmos_grid: Literal["0p25", "0p50", "1p00"] = "0p50"
    map_field_atmos_path_template: str = "gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.{grid}.f{fhour:03d}"
    # Map hours per cycle as "start-end:step" ranges in forecast hours (0..120 3-hourly, then
    # 6-hourly to 240), and the GFS cycles read (UTC hours).
    map_field_steps: str = "0-120:3,120-240:6"
    map_field_cycles: str = "00,12"
    # Primary swell, secondary swell and wind-sea partition layers (9 extra variables).
    map_field_partitions: Literal["on", "off"] = "on"
    # A map-field run that would download more than this is stopped (bytes).
    map_field_max_bytes_per_run: int = Field(default=600_000_000, ge=1)
    # The map's ocean layers are flagged stale once their model cycle is older than this (the
    # map index and the data-health page use the same limit). With MAP_FIELD_CYCLES=00,12 a
    # cycle is normally replaced when it is 17-20 h old (12 h cycle + 5 h NOAA lag + up to one
    # FORECAST_REFRESH_MINUTES interval), so 12 h flagged the layers for 5-8 h of every 12;
    # 18 h still flagged the last 2 h of a late pick-up in normal operation, so the default is
    # 21 h: above the normal maximum, the badge then means a cycle was missed or is late
    # (docs/map-layers.md, Limitations).
    map_field_stale_after_hours: float = Field(default=21.0, gt=0)
    windy_api_key: str | None = None
    # Demo provider only: false = calm background seas, so only simulated swells create
    # events (used by the end-to-end tests for deterministic results).
    demo_natural_swells: bool = True

    # ------------------------------------------------------------------ scoring
    # Wind gusts: "off" = shown only; "scored" = used by the wind score where the source's gust
    # definition is documented (ForecastRecord.extra["gust_kind"]).
    wind_gust_scoring: Literal["off", "scored"] = "off"
    # Optional JSON object overriding the component weights of the quality score, e.g.
    # {"wind": 0.35, "tide": 0.0}: non-negative numbers, at least one above 0; the rest
    # keep their defaults. app.services.surf_quality.weights checks the names at start-up.
    scoring_weights_json: str | None = None

    # ------------------------------------------------------------------ detection
    # The global lead-time window of swell detection and matching (days ahead). Each saved
    # search narrows it with its own alert lead times (saved_searches.alert_lead_min_days /
    # _max_days, defaults ALERT_LEAD_DEFAULT_MIN_DAYS / _MAX_DAYS).
    detection_lead_min_days: float = 1.0
    detection_lead_max_days: float = 10.0
    # Default alert lead times of a new saved search (whole days, 0 <= min <= max <= 16).
    alert_lead_default_min_days: int = Field(default=2, ge=0, le=16)
    alert_lead_default_max_days: int = Field(default=10, ge=0, le=16)
    # Short-notice ranking boost: swells starting this many days ahead rank higher (spec 28).
    short_notice_min_days: float = Field(default=3.0, ge=0, le=16)
    short_notice_max_days: float = Field(default=7.0, ge=0, le=16)
    # Permissive on purpose (= the 'Fair' threshold) so every user threshold is satisfiable.
    swell_event_min_score: int = Field(default=40, ge=0, le=100)
    swell_event_min_hours: int = 3
    # An event ends once this many consecutive daylight hours fail to qualify.
    swell_event_break_daylight_hours: int = 9

    # ------------------------------------------------------------------ flights
    flight_provider: FlightProviderName = "demo"
    duffel_access_token: str | None = None
    duffel_base_url: str = "https://api.duffel.com"
    amadeus_client_id: str | None = None
    amadeus_client_secret: str | None = None
    # Amadeus Self-Service (test.api.amadeus.com) was decommissioned on 2026-07-17.
    # Enterprise customers use the production host below (or their contracted host).
    amadeus_base_url: str = "https://api.amadeus.com"
    flight_http_timeout_seconds: float = 45.0
    flight_cache_ttl_hours: float = 6.0
    flight_daily_quota: int = 300
    flight_max_queries_per_match: int = 3
    flight_refresh_hours: float = 12.0
    flight_max_offers_per_search: int = 50
    # Provider flight searches a day for one user (all of their saved searches).
    flight_user_daily_quota: int = Field(default=20, ge=1, le=10_000)
    # Optional static FX table, e.g. '{"EUR": 1.08, "GBP": 1.27}' = USD per 1 unit.
    # Offers in a currency other than the search currency are discarded unless a rate exists.
    fx_rates_usd_json: str | None = None

    # ------------------------------------------------------------------ notifications
    # console: OUTBOX_DIR file; mailpit: local SMTP inbox (web UI at DEV_INBOX_URL);
    # smtp / resend / sendgrid: real delivery.
    email_provider: EmailProviderName = "console"
    email_from: str = "Swell Travel Agent <alerts@localhost>"
    # Where developers read captured mail (shown in the UI for capture providers only).
    dev_inbox_url: str | None = None
    # Delivery webhooks (optional): Resend signs with Svix ("whsec_..."), SendGrid's Event
    # Webhook with an ECDSA key (base64 DER public key from the SendGrid settings page).
    resend_webhook_secret: str | None = None
    sendgrid_webhook_public_key: str | None = None
    resend_api_key: str | None = None
    sendgrid_api_key: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = False
    smtp_use_ssl: bool = False
    sms_provider: SmsProviderName = "console"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_messaging_service_sid: str | None = None
    notification_max_attempts: int = 5
    notification_sending_timeout_minutes: int = 10
    alert_price_drop_pct: float = 10.0
    alert_price_drop_min_amount: float = 50.0
    alert_score_improvement: int = 10
    # Development console adapters write here (not used by live providers).
    outbox_dir: str = "/tmp/swell-outbox"  # noqa: S108

    # ------------------------------------------------------------------ ranking
    # Optional JSON override of opportunity ranking weights per priority profile.
    ranking_weights_json: str | None = None

    # ------------------------------------------------------------------ retention
    retention_forecast_days: int = 10
    retention_flight_days: int = 30
    retention_job_log_days: int = 30
    retention_notification_days: int = 365
    # The prediction archive and verification pairs are kept much longer than forecast
    # runs: they are the only record of what was forecast at each lead time.
    retention_archive_days: int = 730
    # Archive a short digest of the confidence inputs instead of the full inputs.
    archive_confidence_digest: bool = True
    # Daily cleanup deletes the predictions of superseded non-blend runs that no spot is
    # served from (app.services.maintenance.prune_superseded_predictions). Derived data: the
    # run's forecast records are kept, so the predictions can be recomputed.
    prune_superseded_member_predictions: bool = True

    # ------------------------------------------------------------------ data health
    # The ``ops.data_health`` beat job (app.services.health) checks forecast, map-field,
    # observation and job freshness every DATA_HEALTH_CHECK_MINUTES and emails admin
    # users when the status degrades: at most one alert per problem per
    # DATA_HEALTH_ALERT_REPEAT_HOURS, plus a recovery notice once a problem clears.
    data_health_check_minutes: int = Field(default=15, ge=5, le=1440)
    data_health_alerts_enabled: bool = True
    data_health_alert_repeat_hours: float = Field(default=12.0, ge=1.0, le=24.0 * 7)
    # No finished job of any kind for this long = the worker or the scheduler is down
    # (notifications.deliver alone runs every 2 minutes).
    data_health_heartbeat_minutes: int = Field(default=30, ge=10, le=24 * 60)
    # A provider without a new buoy observation for this long is stale. None = the
    # ingestion look-back window (OBSERVATION_LOOKBACK_HOURS): beyond it, the hourly job
    # can no longer fill the gap by itself.
    data_health_observation_stale_hours: float | None = Field(default=None, gt=0)
    # Degraded once the database is larger than this (GB; an alert only, nothing is
    # deleted). An empty env var keeps this 20 GB default (env_ignore_empty); None (only in
    # code) = no limit. 20 GB sits above the computed two-year steady state (about 17 GB with
    # the default retention), so the alert means unexpected growth. Size it from the disk and
    # the growth per day in docs/operations.md ("Capacity").
    data_health_database_max_gb: float | None = Field(default=20.0, gt=0)
    # Degraded once Redis uses more than this fraction of its maxmemory.
    data_health_redis_max_fraction: float = Field(default=0.8, gt=0, le=1)
    # Admin alert once an API quota (Open-Meteo day/month, Windy day, flights day) reaches
    # this fraction.
    quota_alert_fraction: float = Field(default=0.8, gt=0, le=1)
    # Admin alert when no refresh check ran for this many minutes (the external scheduler or
    # the daemon is silent); 0 = no such alert.
    refresh_scheduler_expected_minutes: int = Field(default=30, ge=0, le=1440)
    # Comma-separated addresses the daily watchdog emails when the database itself cannot
    # be reached (e.g. Neon's compute suspended after its quota): the admin recipients are
    # read from the users table, so they cannot be told then. Optional; validated; logs
    # show counts only, never the addresses.
    ops_alert_emails: str | None = None

    # ------------------------------------------------------------------ worker and compose
    # Queues the Celery worker consumes: mirrors docker-entrypoint.sh's default (the routing
    # tests check that every task lands on one). The Redis memory cap of docker-compose.yml:
    # what the Redis health check measures against when Redis runs without a maxmemory.
    worker_queues: str = "celery,bulk"
    redis_maxmemory: str = "256mb"

    @field_validator("app_base_url", "api_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("database_url", "database_url_unpooled")
    @classmethod
    def _normalize_database_url(cls, v: str | None) -> str | None:
        return None if v is None else normalize_database_url(v)

    @field_validator("client_ip_header")
    @classmethod
    def _check_client_ip_header(cls, v: str | None) -> str | None:
        if v is None:
            return None
        name = v.strip().lower()
        if not name:
            return None
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(
                f"CLIENT_IP_HEADER must be a header name (letters, digits, '-'), got {v!r}"
            )
        return name

    @field_validator("ops_alert_emails")
    @classmethod
    def _check_ops_alert_emails(cls, v: str | None) -> str | None:
        if v is None or not _split(v):
            return None
        from email_validator import EmailNotValidError, validate_email

        out: list[str] = []
        for index, entry in enumerate(_split(v), start=1):
            try:
                out.append(validate_email(entry, check_deliverability=False).normalized)
            except EmailNotValidError:
                # The entry is not quoted: the message reaches logs.
                raise ValueError(
                    f"OPS_ALERT_EMAILS entry {index} is not a valid email address"
                ) from None
        return ",".join(out)

    @field_validator("sync_trigger_sources", "sync_daemon_kinds")
    @classmethod
    def _check_component_kinds(cls, v: str, info: ValidationInfo) -> str:
        name = str(info.field_name).upper()
        tokens = set(_split(v))
        if not tokens:
            raise ValueError(f"{name} must name at least one of spots, map, ensemble")
        unknown = tokens - SYNC_TRIGGER_TOKENS
        if unknown:
            raise ValueError(f"Unknown {name} entries {sorted(unknown)}: use spots, map, ensemble")
        return v

    @field_validator("forecast_stale_threshold_minutes_by_model")
    @classmethod
    def _check_stale_by_model(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        for model, minutes in _json_object(v, "FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL").items():
            if not model.strip():
                raise ValueError(
                    "FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL has an empty model name"
                )
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
                raise ValueError(
                    f"FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL[{model!r}] must be a whole "
                    "number of minutes >= 0"
                )
        return v

    @field_validator("scoring_weights_json")
    @classmethod
    def _check_scoring_weights(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        weights = _json_object(v, "SCORING_WEIGHTS_JSON")
        if not weights:
            raise ValueError("SCORING_WEIGHTS_JSON must name at least one component")
        for name, weight in weights.items():
            if (
                isinstance(weight, bool)
                or not isinstance(weight, int | float)
                or not math.isfinite(weight)
                or weight < 0
            ):
                raise ValueError(f"SCORING_WEIGHTS_JSON[{name!r}] must be a number >= 0")
        if not any(weight > 0 for weight in weights.values()):
            raise ValueError("SCORING_WEIGHTS_JSON needs at least one weight above 0")
        from app.services.surf_quality.weights import validate_weights

        validate_weights(weights)  # component names; wave_size above 0
        return v

    @field_validator("map_field_steps")
    @classmethod
    def _check_steps(cls, v: str) -> str:
        _step_ranges(v)  # raises ValueError on a malformed entry
        return v

    @field_validator("map_field_cycles", "gefs_cycles")
    @classmethod
    def _check_cycles(cls, v: str) -> str:
        hours = _split(v)
        if not hours or any(not h.isdigit() or int(h) not in (0, 6, 12, 18) for h in hours):
            raise ValueError(f"cycles must be a comma-separated subset of 00,06,12,18, got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.app_env == "production":
            if self.secret_key.startswith("dev-insecure"):
                raise ValueError("SECRET_KEY must be set to a strong random value in production")
            if self.enable_dev_endpoints:
                raise ValueError("ENABLE_DEV_ENDPOINTS must be false in production")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production (HTTPS)")
            if self.email_capture_only:
                raise ValueError(
                    f"EMAIL_PROVIDER={self.email_provider} only captures mail locally; "
                    "use smtp, resend or sendgrid in production"
                )
            if self.weather_provider == "windy" and not self.windy_eligible:
                raise ValueError(
                    "WEATHER_PROVIDER=windy needs "
                    + " and ".join(self.windy_ineligibility())
                    + " (a Windy key is not a licence to store its data): set both, or use "
                    "WEATHER_PROVIDER=auto or open_meteo until then (docs/WINDY_API_SETUP.md)"
                )
        providers = self.forecast_provider_list
        if (
            self.app_env == "production"
            and self.commercial_use
            and not self.open_meteo_api_key
            and OPEN_METEO_PROVIDER_TOKENS & set(providers)
        ):
            raise ValueError(
                "OPEN_METEO_COMMERCIAL=true (COMMERCIAL_USE) needs OPEN_METEO_API_KEY: the free "
                "Open-Meteo API is for non-commercial use only (docs/API_SETUP_REQUIRED.md)"
            )
        if "demo" in providers and len(providers) > 1:
            raise ValueError(
                "FORECAST_PROVIDERS cannot mix 'demo' with live providers: synthetic data "
                "must never be blended into real forecasts"
            )
        unknown = set(providers) - FORECAST_PROVIDER_TOKENS
        if unknown:
            raise ValueError(f"Unknown FORECAST_PROVIDERS entries: {sorted(unknown)}")
        if "windy" in providers and not self.windy_storage_authorized:
            raise ValueError(
                "FORECAST_PROVIDERS contains 'windy', but Windy's API terms forbid storing its "
                "data, building databases derived from it and redistributing it, which this app "
                "does (forecast runs, blends, archived predictions). Remove 'windy', or set "
                "WINDY_STORAGE_AUTHORIZED=true (formerly WINDY_TERMS_ACKNOWLEDGED) once a "
                "licence that allows this use is in place (docs/API_SETUP_REQUIRED.md)."
            )
        if self.detection_lead_min_days > self.detection_lead_max_days:
            raise ValueError("DETECTION_LEAD_MIN_DAYS must be <= DETECTION_LEAD_MAX_DAYS")
        if self.alert_lead_default_min_days > self.alert_lead_default_max_days:
            raise ValueError("ALERT_LEAD_DEFAULT_MIN_DAYS must be <= ALERT_LEAD_DEFAULT_MAX_DAYS")
        if self.short_notice_min_days > self.short_notice_max_days:
            raise ValueError("SHORT_NOTICE_MIN_DAYS must be <= SHORT_NOTICE_MAX_DAYS")
        if self.job_runner == "daemon" and "sync_lease_minutes" not in self.model_fields_set:
            self.sync_lease_minutes = DAEMON_LEASE_MINUTES
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def unpooled_database_url(self) -> str | None:
        """DATABASE_URL_UNPOOLED when it is set and names the same database as DATABASE_URL.

        The direct and the pooled URL of one database differ in host (and, on some hosts,
        user) but never in database name. Another name means the unpooled URL does not
        lead to this process's database (a stale variable, or a test or tool that pointed
        DATABASE_URL elsewhere): it is ignored with a warning (names only, never the URL),
        so no caller can migrate or write to the database a stale unpooled URL names.
        """
        unpooled = self.database_url_unpooled
        if not unpooled:
            return None
        direct_name, pooled_name = _database_name(unpooled), _database_name(self.database_url)
        if direct_name != pooled_name:
            logger.warning(
                "DATABASE_URL_UNPOOLED names database %r but DATABASE_URL names %r; "
                "ignoring the unpooled URL",
                direct_name,
                pooled_name,
            )
            return None
        return unpooled

    @property
    def engine_database_url(self) -> str:
        """The URL this process's engine connects to: the unpooled URL when DB_USE_UNPOOLED
        is true and :attr:`unpooled_database_url` is usable, else DATABASE_URL."""
        if self.db_use_unpooled and (direct := self.unpooled_database_url):
            return direct
        return self.database_url

    @property
    def migration_database_url(self) -> str:
        """Migrations need a direct connection: :attr:`unpooled_database_url`, else
        DATABASE_URL."""
        return self.unpooled_database_url or self.database_url

    @property
    def ops_alert_email_list(self) -> list[str]:
        """OPS_ALERT_EMAILS as a list of normalised addresses (empty when unset)."""
        return _split(self.ops_alert_emails or "")

    @property
    def sync_trigger_list(self) -> frozenset[str]:
        """SYNC_TRIGGER_SOURCES as a set of component kinds (spots, map, ensemble)."""
        return frozenset(_split(self.sync_trigger_sources))

    @property
    def sync_daemon_kind_list(self) -> frozenset[str]:
        """SYNC_DAEMON_KINDS as a set of component kinds (spots, ensemble, map)."""
        return frozenset(_split(self.sync_daemon_kinds))

    @property
    def runs_celery(self) -> bool:
        """True when Celery worker and beat run the jobs (JOB_RUNNER=celery)."""
        return self.job_runner == "celery"

    @property
    def cors_origin_list(self) -> list[str]:
        return _split(self.cors_origins)

    @property
    def demo_mode(self) -> bool:
        """True when any configured provider serves synthetic data (shown as a UI ribbon)."""
        return "demo" in self.forecast_provider_list or self.flight_provider == "demo"

    @property
    def windy_terms_acknowledged(self) -> bool:
        """The old name of :attr:`windy_storage_authorized` (read-only)."""
        return self.windy_storage_authorized

    def windy_ineligibility(self) -> list[str]:
        """Why Windy may not be the primary provider (empty = eligible): a key and the
        storage authorization are both required (design D3). Never names the key's value."""
        missing: list[str] = []
        if not self.windy_api_key:
            missing.append("WINDY_API_KEY")
        if not self.windy_storage_authorized:
            missing.append("WINDY_STORAGE_AUTHORIZED=true")
        return missing

    @property
    def windy_eligible(self) -> bool:
        """WINDY_API_KEY is set and WINDY_STORAGE_AUTHORIZED=true."""
        return not self.windy_ineligibility()

    def provider_selection(self) -> ProviderSelection:
        """The effective forecast providers and why (design D2): FORECAST_PROVIDERS when set
        (advanced override), else derived from WEATHER_PROVIDER. The provider manager, the
        CLI's provider check and the admin dashboard show :attr:`ProviderSelection.reasons`."""
        explicit = _split(self.forecast_providers or "")
        missing = self.windy_ineligibility()
        eligible = not missing
        windy_state = (
            "Windy is eligible (WINDY_API_KEY set, WINDY_STORAGE_AUTHORIZED=true)"
            if eligible
            else "Windy is not eligible (" + " and ".join(missing) + " missing)"
        )
        reasons: list[str]
        source: SelectionSource
        if explicit:
            providers = tuple(explicit)
            source = "FORECAST_PROVIDERS"
            reasons = [
                f"FORECAST_PROVIDERS={','.join(explicit)} (advanced override; "
                f"WEATHER_PROVIDER={self.weather_provider} is not used)"
            ]
        else:
            source = "WEATHER_PROVIDER"
            choice = self.weather_provider
            if choice == "demo":
                providers = ("demo",)
                reasons = ["WEATHER_PROVIDER=demo: synthetic demo forecasts, not real data"]
            elif choice == "open_meteo":
                providers = ("open_meteo",)
                reasons = ["WEATHER_PROVIDER=open_meteo: Open-Meteo (NOAA GFS and GFS-Wave)"]
            elif eligible:
                providers = ("windy",)
                reasons = [f"WEATHER_PROVIDER={choice}: {windy_state}, so Windy is the primary"]
            else:
                providers = (WINDY_FALLBACK_PROVIDER,)
                reasons = [f"WEATHER_PROVIDER={choice}: {windy_state}, so Open-Meteo is used"]
                if choice == "windy":
                    reasons.append(
                        "production refuses WEATHER_PROVIDER=windy until Windy is eligible"
                    )
        fallback = WINDY_FALLBACK_PROVIDER if providers[0] == "windy" else None
        if fallback:
            reasons.append(
                f"runtime fallback: {fallback} when a Windy refresh fails (credentials, testing "
                "key, quota or availability)"
            )
        return ProviderSelection(
            providers=providers,
            source=source,
            weather_provider=self.weather_provider,
            windy_eligible=eligible,
            fallback=fallback,
            reasons=tuple(reasons),
        )

    @property
    def forecast_provider_list(self) -> list[str]:
        """The effective FORECAST_PROVIDERS tokens in priority order (first = primary):
        :meth:`provider_selection`. Every reader of the provider list uses this."""
        return list(self.provider_selection().providers)

    @property
    def forecast_stale_threshold_by_model(self) -> dict[str, int]:
        """FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL as {model: minutes} (empty when unset)."""
        raw = self.forecast_stale_threshold_minutes_by_model
        return {str(k): int(v) for k, v in json.loads(raw).items()} if raw else {}

    def stale_threshold_minutes(self, model: str | None) -> int:
        """The grace (minutes) past a model's update interval before its data are stale: the
        per-model override when there is one, else FORECAST_STALE_THRESHOLD_MINUTES."""
        overrides = self.forecast_stale_threshold_by_model
        if model is not None and model in overrides:
            return overrides[model]
        return self.forecast_stale_threshold_minutes

    @property
    def scoring_weights(self) -> dict[str, float] | None:
        """SCORING_WEIGHTS_JSON as {component: weight}; None when unset (the defaults)."""
        raw = self.scoring_weights_json
        return {str(k): float(v) for k, v in json.loads(raw).items()} if raw else None

    @property
    def open_meteo_gfs_wave_model_list(self) -> list[str]:
        """GFS-family wave models in routing order (the deprecated alias wins when set)."""
        if "open_meteo_wave_model" in self.model_fields_set:
            return [self.open_meteo_wave_model]
        return _split(self.open_meteo_gfs_wave_models)

    @property
    def open_meteo_gfs_atmos_model_name(self) -> str:
        """GFS-family atmosphere model (the deprecated alias wins when set)."""
        if "open_meteo_atmos_model" in self.model_fields_set:
            return self.open_meteo_atmos_model
        return self.open_meteo_gfs_atmos_model

    @property
    def ecmwf_wave_param_list(self) -> list[str]:
        return _split(self.ecmwf_wave_params)

    @property
    def gefs_stat_field_list(self) -> list[str]:
        return _split(self.gefs_stat_fields)

    @property
    def gefs_cycle_hours(self) -> list[int]:
        return sorted({int(h) for h in _split(self.gefs_cycles)})

    @property
    def map_field_step_ranges(self) -> list[tuple[int, int, int]]:
        """MAP_FIELD_STEPS as (first hour, last hour, step) ranges, in the order given."""
        return _step_ranges(self.map_field_steps)

    @property
    def map_field_cycle_hours(self) -> list[int]:
        return sorted({int(h) for h in _split(self.map_field_cycles)})

    @property
    def tide_provider_list(self) -> list[str]:
        return _split(self.tide_providers)

    @property
    def noaa_coops_application_name(self) -> str:
        return self.noaa_coops_application or self.app_name

    @property
    def worker_queue_list(self) -> list[str]:
        return _split(self.worker_queues)

    @property
    def observation_provider_list(self) -> list[str]:
        return _split(self.observation_providers)

    @property
    def point_forecast_source_list(self) -> list[str]:
        return _split(self.point_forecast_sources)

    @property
    def fx_rates_usd(self) -> dict[str, float]:
        if not self.fx_rates_usd_json:
            return {}
        data = json.loads(self.fx_rates_usd_json)
        return {str(k).upper(): float(v) for k, v in data.items()}

    @property
    def email_capture_only(self) -> bool:
        """True when email is captured locally (development) rather than delivered."""
        return self.email_provider in CAPTURE_EMAIL_PROVIDERS

    @property
    def captured_email_location(self) -> str | None:
        """Where a developer finds captured mail, or None for real delivery."""
        if self.email_provider == "mailpit":
            return self.dev_inbox_url or f"Mailpit at {self.smtp_host}"
        if self.email_provider == "console":
            return f"{self.outbox_dir}/outbox.jsonl"
        return None

    def missing_live_credentials(self) -> list[str]:
        """Names of env vars that are required by the configured live providers but unset."""
        missing: list[str] = []
        if self.flight_provider == "duffel" and not self.duffel_access_token:
            missing.append("DUFFEL_ACCESS_TOKEN")
        if self.flight_provider == "amadeus":
            if not self.amadeus_client_id:
                missing.append("AMADEUS_CLIENT_ID")
            if not self.amadeus_client_secret:
                missing.append("AMADEUS_CLIENT_SECRET")
        if self.email_provider == "resend" and not self.resend_api_key:
            missing.append("RESEND_API_KEY")
        if self.email_provider == "sendgrid" and not self.sendgrid_api_key:
            missing.append("SENDGRID_API_KEY")
        if self.sms_provider == "twilio":
            for name in ("twilio_account_sid", "twilio_auth_token"):
                if not getattr(self, name):
                    missing.append(name.upper())
            if not (self.twilio_from_number or self.twilio_messaging_service_sid):
                missing.append("TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID")
        if "windy" in self.forecast_provider_list and not self.windy_api_key:
            missing.append("WINDY_API_KEY")
        if self.job_runner == "daemon" and self.app_env == "production" and not self.cron_secret:
            missing.append("CRON_SECRET (daily watchdog)")
        selection = self.provider_selection()
        if (
            self.commercial_use
            and OPEN_METEO_PROVIDER_TOKENS & {*selection.providers, selection.fallback or ""}
            and not self.open_meteo_api_key
        ):
            missing.append(
                "OPEN_METEO_API_KEY (COMMERCIAL_USE=true: the free Open-Meteo API is "
                "non-commercial)"
            )
        return missing


@dataclass(frozen=True)
class ProviderSelection:
    """What :meth:`Settings.provider_selection` chose.

    * ``providers``: the effective FORECAST_PROVIDERS tokens, first = primary;
    * ``source``: ``FORECAST_PROVIDERS`` (the explicit override) or ``WEATHER_PROVIDER``;
    * ``windy_eligible``: WINDY_API_KEY set and WINDY_STORAGE_AUTHORIZED=true;
    * ``fallback``: the provider a Windy primary falls back to at runtime, else None;
    * ``reasons``: plain sentences for operators (never a credential's value).
    """

    providers: tuple[str, ...]
    source: SelectionSource
    weather_provider: str
    windy_eligible: bool
    fallback: str | None
    reasons: tuple[str, ...]

    @property
    def primary(self) -> str:
        return self.providers[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "providers": list(self.providers),
            "primary": self.primary,
            "source": self.source,
            "weather_provider": self.weather_provider,
            "windy_eligible": self.windy_eligible,
            "fallback": self.fallback,
            "reasons": list(self.reasons),
        }


def normalize_database_url(url: str) -> str:
    """Use the psycopg driver for plain PostgreSQL URLs.

    'postgres://' and 'postgresql://' (the forms Neon and most hosts hand out) become
    'postgresql+psycopg://'; the rest of the URL, query string included (sslmode,
    channel_binding), is kept. Other schemes are returned unchanged.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def _database_name(url: str) -> str | None:
    """The database name of a SQLAlchemy URL (None when it cannot be parsed)."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        return make_url(url).database
    except ArgumentError:
        return None


def _json_object(value: str, name: str) -> dict[str, Any]:
    """A setting that holds a JSON object (the value itself is never quoted in the error)."""
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(f"{name} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a JSON object")
    return data


def _split(value: str) -> list[str]:
    """Entries of a comma-separated setting, stripped, blanks dropped."""
    return [p.strip() for p in value.split(",") if p.strip()]


def _step_ranges(value: str) -> list[tuple[int, int, int]]:
    """Parse "0-120:3,120-240:6" into [(0, 120, 3), (120, 240, 6)]."""
    out: list[tuple[int, int, int]] = []
    for entry in _split(value):
        try:
            span, step = entry.split(":")
            first, last = span.split("-")
            item = (int(first), int(last), int(step))
        except ValueError as exc:
            raise ValueError(f"MAP_FIELD_STEPS entry {entry!r} is not 'start-end:step'") from exc
        if item[0] < 0 or item[1] <= item[0] or item[2] < 1:
            raise ValueError(f"MAP_FIELD_STEPS entry {entry!r} needs 0 <= start < end, step >= 1")
        out.append(item)
    if not out:
        raise ValueError("MAP_FIELD_STEPS is empty")
    return out


def redacted(exc: ValidationError) -> ValidationError:
    """``exc`` without the rejected values: pydantic's own message quotes each input, and a
    SECRET_KEY or CRON_SECRET that is too short is still a secret. The setting, the error
    type and its message stay (``exc.errors(include_input=False)``)."""
    details: list[InitErrorDetails] = []
    for error in exc.errors(include_url=False):
        item = InitErrorDetails(type=error["type"], loc=error["loc"], input="[hidden]")
        if "ctx" in error:
            item["ctx"] = error["ctx"]
        details.append(item)
    return ValidationError.from_exception_data(exc.title, details, hide_input=True)


@lru_cache
def get_settings() -> Settings:
    """The settings of this process (loaded once). An invalid setting raises a
    ValidationError that names the setting and the reason but never its value, so it is
    safe in the Vercel function's, the daemon's and the CLI's logs (:func:`redacted`)."""
    try:
        return Settings()
    except ValidationError as exc:
        raise redacted(exc) from None
