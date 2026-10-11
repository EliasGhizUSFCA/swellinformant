"""Application settings, loaded exclusively from environment variables (or a .env file).

Every secret (database password, API keys, provider tokens) is read here and nowhere
else. Nothing in this module is ever sent to the frontend.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ForecastProviderName = Literal["open_meteo", "windy", "demo"]
FlightProviderName = Literal["duffel", "amadeus", "demo"]
EmailProviderName = Literal["console", "mailpit", "smtp", "resend", "sendgrid"]
# Providers that capture mail locally for development: nothing reaches a real mailbox.
CAPTURE_EMAIL_PROVIDERS = frozenset({"console", "mailpit"})
SmsProviderName = Literal["console", "twilio", "disabled"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # `KEY=` (blank, as in .env.example or `${KEY:-}` in compose) means "use the default".
        env_ignore_empty=True,
    )

    # ------------------------------------------------------------------ general
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Swell Travel Agent"
    log_level: str = "INFO"
    # Public URL of the web app (used in email links). No trailing slash.
    app_base_url: str = "http://localhost:3000"
    # Public URL of the API (used for webhook signature validation). No trailing slash.
    api_base_url: str = "http://localhost:8000"
    secret_key: str = Field(default="dev-insecure-secret-change-me", min_length=16)
    enable_dev_endpoints: bool = False

    # ------------------------------------------------------------------ infrastructure
    database_url: str = "postgresql+psycopg://swell:swell@localhost:5432/swell"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False
    # Queue one full pipeline run when celery beat starts (instead of waiting an interval).
    beat_run_on_start: bool = True
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ------------------------------------------------------------------ http/security
    cors_origins: str = "http://localhost:3000"
    session_cookie_name: str = "sta_session"
    csrf_cookie_name: str = "sta_csrf"
    session_ttl_days: int = 14
    cookie_secure: bool = False
    cookie_domain: str | None = None
    rate_limit_enabled: bool = True
    login_max_failures: int = 8
    login_lockout_minutes: int = 15
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60
    trusted_proxy_count: int = 1

    # ------------------------------------------------------------------ forecasts
    # Comma separated, in priority order. The first provider is the primary source.
    forecast_providers: str = "demo"
    forecast_days: int = Field(default=16, ge=3, le=16)
    forecast_refresh_minutes: int = 180
    forecast_stale_hours: int = 18
    forecast_batch_size: int = Field(default=10, ge=1, le=50)
    forecast_http_timeout_seconds: float = 30.0
    forecast_daily_request_quota: int = 5000
    open_meteo_api_key: str | None = None
    open_meteo_wave_model: str = "ncep_gfswave025"
    open_meteo_atmos_model: str = "gfs_seamless"

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
    # ECMWF open data (CC BY 4.0, attribution required).
    ecmwf_base_url: str = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
    ecmwf_max_lead_hours: int = Field(default=240, ge=24, le=360)
    ecmwf_availability_lag_hours: float = 8.0
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

    # ------------------------------------------------------------------ verification & ML
    verification_enabled: bool = False
    # Forecast sources archived at observation stations for verification.
    point_forecast_sources: str = "gfswave_bulletin"
    ml_artifact_dir: str = "/tmp/swell-ml"  # noqa: S108  (use a persistent volume in production)
    # Apply ACTIVE ML corrections (only models that passed the deployment criteria).
    ml_corrections_enabled: bool = True
    # Map ocean layers (downsampled GFS-Wave + GFS global grids). None = automatic: on
    # when "noaa_gfs" is a forecast provider (the grids are extracted from files that
    # provider downloads anyway); True also fetches them in other modes; False = off.
    map_fields_enabled: bool | None = None
    map_field_resolution_deg: float = Field(default=0.5, ge=0.25, le=2.0)
    map_field_max_lead_hours: int = Field(default=240, ge=24, le=384)
    map_field_step_hours: int = Field(default=3, ge=1, le=12)
    windy_api_key: str | None = None
    # Demo provider only: false = calm background seas, so only simulated swells create
    # events (used by the end-to-end tests for deterministic results).
    demo_natural_swells: bool = True

    # ------------------------------------------------------------------ detection
    detection_lead_min_days: float = 5.0
    detection_lead_max_days: float = 10.0
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
    flight_offer_max_age_hours: float = 24.0
    flight_max_offers_per_search: int = 50
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
    max_alerts_per_user_per_day: int = 10
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

    @field_validator("app_base_url", "api_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

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
        providers = self.forecast_provider_list
        if "demo" in providers and len(providers) > 1:
            raise ValueError(
                "FORECAST_PROVIDERS cannot mix 'demo' with live providers: synthetic data "
                "must never be blended into real forecasts"
            )
        unknown = set(providers) - {"open_meteo", "windy", "demo", "noaa_gfs", "ecmwf_ifs"}
        if unknown:
            raise ValueError(f"Unknown FORECAST_PROVIDERS entries: {sorted(unknown)}")
        if self.detection_lead_min_days > self.detection_lead_max_days:
            raise ValueError("DETECTION_LEAD_MIN_DAYS must be <= DETECTION_LEAD_MAX_DAYS")
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def demo_mode(self) -> bool:
        """True when any configured provider serves synthetic data (shown as a UI ribbon)."""
        return "demo" in self.forecast_provider_list or self.flight_provider == "demo"

    @property
    def forecast_provider_list(self) -> list[str]:
        names = [p.strip() for p in self.forecast_providers.split(",") if p.strip()]
        return names or ["demo"]

    @property
    def observation_provider_list(self) -> list[str]:
        return [p.strip() for p in self.observation_providers.split(",") if p.strip()]

    @property
    def point_forecast_source_list(self) -> list[str]:
        return [p.strip() for p in self.point_forecast_sources.split(",") if p.strip()]

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
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
