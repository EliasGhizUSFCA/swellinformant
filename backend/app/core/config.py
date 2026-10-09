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
EmailProviderName = Literal["console", "smtp", "resend", "sendgrid"]
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
    email_provider: EmailProviderName = "console"
    email_from: str = "Swell Travel Agent <alerts@localhost>"
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
        providers = self.forecast_provider_list
        if "demo" in providers and len(providers) > 1:
            raise ValueError(
                "FORECAST_PROVIDERS cannot mix 'demo' with live providers: synthetic data "
                "must never be blended into real forecasts"
            )
        unknown = set(providers) - {"open_meteo", "windy", "demo"}
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
    def fx_rates_usd(self) -> dict[str, float]:
        if not self.fx_rates_usd_json:
            return {}
        data = json.loads(self.fx_rates_usd_json)
        return {str(k).upper(): float(v) for k, v in data.items()}

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
