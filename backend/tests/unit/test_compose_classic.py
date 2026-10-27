"""The classic docker compose stack (docker-compose.yml) passes the Upgrade 4 runner, sync and
connection settings through without changing their defaults, and offers the sync daemon as
an optional profile that replaces Celery worker and beat (docs/sync.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "docker-compose.yml"
VERCEL_JSON = ROOT / "vercel.json"

# Settings the API, the worker or the daemon read since Upgrade 4. The classic stack must
# let .env set them, and its defaults must be the Settings defaults, so that a stack that
# sets nothing behaves as before.
PASSED_THROUGH = {
    "JOB_RUNNER",
    "CRON_SECRET",
    "CLIENT_IP_HEADER",
    "COMMERCIAL_USE",
    "DB_PREPARED_STATEMENTS",
    "DB_POOL_RECYCLE_SECONDS",
    "REDIS_CONNECT_TIMEOUT_SECONDS",
    "REDIS_SOCKET_TIMEOUT_SECONDS",
    "SYNC_CHECK_MINUTES",
    "SYNC_COALESCE_MINUTES",
    "SYNC_TRIGGER_SOURCES",
    "SYNC_RETRY_MINUTES",
    "SYNC_MAX_ATTEMPTS",
    "SYNC_LEASE_MINUTES",
    "SYNC_PUBLISH_GATE",
    "SYNC_PRUNE_EACH_PASS",
    "SYNC_DAEMON_POLL_SECONDS",
    "SYNC_DAEMON_INIT_DB",
    "SYNC_CPU_BUDGET_HOURS",
    "JOB_INTERVAL_FLOOR_MINUTES",
    "MAP_FIELD_MAX_LEAD_HOURS",
    "SYNC_PREWARM_PUBLIC_CACHE",
    "OPS_ALERT_EMAILS",
    # Swell 2.0 (design B).
    "FORECAST_PROVIDERS",
    "FORECAST_DAYS",
    "FORECAST_REFRESH_ENABLED",
    "FORECAST_STALE_THRESHOLD_MINUTES",
    "FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL",
    "FORECAST_MIN_VALID_FRACTION",
    "FORECAST_VERSION_CDN_SECONDS",
    "WINDY_STORAGE_AUTHORIZED",
    "WINDY_CHECK_MINUTES",
    "WINDY_DAILY_REQUEST_LIMIT",
    "SYNC_DAEMON_KINDS",
    "PREDICTION_ARCHIVE_ENABLED",
    "SCORING_WEIGHTS_JSON",
    "DETECTION_LEAD_MIN_DAYS",
    "ALERT_LEAD_DEFAULT_MIN_DAYS",
    "ALERT_LEAD_DEFAULT_MAX_DAYS",
    "SHORT_NOTICE_MIN_DAYS",
    "SHORT_NOTICE_MAX_DAYS",
    "FLIGHT_USER_DAILY_QUOTA",
    "QUOTA_ALERT_FRACTION",
    "REFRESH_SCHEDULER_EXPECTED_MINUTES",
}
# Old names still read through an alias (AliasChoices), passed through empty.
ALIASES_PASSED_THROUGH = {"OPEN_METEO_COMMERCIAL", "WINDY_TERMS_ACKNOWLEDGED"}


def backend_env_defaults() -> dict[str, str]:
    """``NAME: ${NAME:-default}`` entries of the x-backend-env block."""
    block = COMPOSE.read_text().split("x-backend-env: &backend-env", 1)[1].split("\nx-", 1)[0]
    found = re.findall(r"^  ([A-Z][A-Z0-9_]+): \$\{([A-Z][A-Z0-9_]+):-([^}]*)\}$", block, re.M)
    for key, var, _default in found:
        assert key == var, f"{key} is read from ${{{var}}}"
    return {key: default for key, _var, default in found}


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    return dict(yaml.safe_load(COMPOSE.read_text()))


def _settings(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Settings:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_new_settings_are_passed_through_with_todays_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = backend_env_defaults()
    missing = sorted(PASSED_THROUGH - set(defaults))
    assert not missing, f"not passed to the containers: {missing}"
    settings_names = {name.upper() for name in Settings.model_fields}
    assert settings_names >= PASSED_THROUGH
    # The compose defaults, as the containers receive them, load to the Settings defaults
    # (an empty value means "the default": env_ignore_empty).
    plain = _settings(monkeypatch, {})
    composed = _settings(monkeypatch, {name: defaults[name] for name in PASSED_THROUGH})
    for name in sorted(PASSED_THROUGH):
        field = name.lower()
        assert getattr(composed, field) == getattr(plain, field), name
    assert composed.job_runner == "celery"
    assert composed.cron_secret is None
    assert composed.client_ip_header is None
    assert ALIASES_PASSED_THROUGH <= set(defaults)
    assert all(defaults[name] == "" for name in ALIASES_PASSED_THROUGH)


def test_the_classic_stack_keeps_demo_forecasts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings default to WEATHER_PROVIDER=auto (Open-Meteo without Windy); this stack keeps
    synthetic demo data until .env chooses live sources, so nothing calls a provider."""
    defaults = backend_env_defaults()
    assert defaults["WEATHER_PROVIDER"] == "demo" and defaults["FORECAST_PROVIDERS"] == ""
    forecasts = {name: defaults[name] for name in ("WEATHER_PROVIDER", "FORECAST_PROVIDERS")}
    assert _settings(monkeypatch, forecasts).forecast_provider_list == ["demo"]
    # A live choice in .env reaches the containers.
    live = _settings(monkeypatch, forecasts | {"WEATHER_PROVIDER": "open_meteo"})
    assert live.forecast_provider_list == ["open_meteo"]


def test_worker_and_beat_stay_the_default_runner(compose: dict[str, Any]) -> None:
    services = compose["services"]
    for name in ("backend", "worker", "beat"):
        assert "profiles" not in services[name], name
    assert services["worker"]["command"] == ["worker"]
    assert services["beat"]["command"] == ["beat"]


def test_the_optional_sync_service_runs_the_daemon(compose: dict[str, Any]) -> None:
    sync = compose["services"]["sync"]
    # Opt-in only: a plain `docker compose up` never starts it next to beat.
    assert sync["profiles"] == ["sync"]
    assert sync["command"] == ["sync"]  # docker-entrypoint.sh: python -m app.cli sync daemon
    assert sync["environment"]["JOB_RUNNER"] == "daemon"
    assert sync["build"]["dockerfile"] == "backend/Dockerfile"
    assert sync["depends_on"]["backend"]["condition"] == "service_healthy"
    if VERCEL_JSON.is_file():
        daemon = json.loads(VERCEL_JSON.read_text())["daemons"]["sync"]
        grace = daemon["terminationGracePeriodSeconds"]
        assert sync["stop_grace_period"] == f"{grace}s"
    text = COMPOSE.read_text()
    # The comment tells the owner how to switch without running beat next to the daemon.
    assert "--profile sync up -d --scale worker=0 --scale beat=0" in text
