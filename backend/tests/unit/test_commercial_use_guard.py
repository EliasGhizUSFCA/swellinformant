"""COMMERCIAL_USE and the free Open-Meteo API (design D13).

Open-Meteo: "You may only use the free API services for non-commercial purposes." With
COMMERCIAL_USE=true and no OPEN_METEO_API_KEY the Open-Meteo providers refuse to be built
or to send any request; check-config and data health report it (tests/unit/
test_sync_health.py, tests/integration/test_data_health.py). No request below reaches a
real host: every client is an httpx.MockTransport that records what it is asked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.services.forecasts.base import SpotPoint
from app.services.forecasts.open_meteo import (
    FREE_API_NON_COMMERCIAL,
    OpenMeteoProvider,
    free_api_refused,
    require_commercial_licence,
)
from app.services.forecasts.open_meteo_ecmwf import OpenMeteoEcmwfProvider
from app.services.forecasts.registry import build_provider
from app.services.http import PermanentProviderError

KEY = "synthetic-open-meteo-key-0123"
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **({"forecast_providers": "open_meteo"} | kw))


class Recorder:
    """Answers nothing useful: the guard must stop every request before it is sent."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(503, text="synthetic: no request should get here")


def client(rec: Recorder) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(rec))


def test_the_guard_needs_both_commercial_use_and_no_key() -> None:
    assert free_api_refused(settings(commercial_use=True)) is True
    assert free_api_refused(settings(commercial_use=True, open_meteo_api_key=KEY)) is False
    assert free_api_refused(settings(commercial_use=False)) is False
    require_commercial_licence(settings())  # the default (non-commercial): no error
    with pytest.raises(PermanentProviderError) as err:
        require_commercial_licence(settings(commercial_use=True))
    assert str(err.value) == FREE_API_NON_COMMERCIAL
    assert FREE_API_NON_COMMERCIAL == (
        "free Open-Meteo API is non-commercial; set OPEN_METEO_API_KEY"
    )
    assert err.value.transient is False  # never retried


@pytest.mark.parametrize("token", ["open_meteo", "open_meteo_ecmwf"])
def test_the_providers_refuse_to_be_built(token: str) -> None:
    commercial = settings(commercial_use=True, forecast_providers=token)
    with pytest.raises(PermanentProviderError, match="non-commercial; set OPEN_METEO_API_KEY"):
        build_provider(token, commercial)
    rec = Recorder()
    cls = OpenMeteoProvider if token == "open_meteo" else OpenMeteoEcmwfProvider
    with pytest.raises(PermanentProviderError):
        cls(commercial, client=client(rec))
    assert rec.calls == []


def test_with_a_key_the_commercial_hosts_are_used() -> None:
    p = build_provider("open_meteo", settings(commercial_use=True, open_meteo_api_key=KEY))
    assert isinstance(p, OpenMeteoProvider)
    assert p.hosts == {
        "marine": "https://customer-marine-api.open-meteo.com",
        "api": "https://customer-api.open-meteo.com",
    }


def test_no_request_is_sent_once_commercial_use_is_switched_on() -> None:
    """A provider built earlier (e.g. by a long-running daemon) stops at the next request."""
    rec = Recorder()
    s = settings()
    p = OpenMeteoProvider(s, client=client(rec), backoff_seconds=0, clock=lambda: NOW)
    s.commercial_use = True
    with pytest.raises(PermanentProviderError, match="non-commercial"):
        p.read_metas()
    with pytest.raises(PermanentProviderError, match="non-commercial"):
        p.current_run(NOW)
    spot = SpotPoint(  # synthetic
        spot_id=1,
        slug="synthetic",
        latitude=0.0,
        longitude=0.0,
        forecast_latitude=0.0,
        forecast_longitude=0.0,
    )
    run = type("Run", (), {"model_runs": (object(),), "run_key": "synthetic"})()
    with pytest.raises(PermanentProviderError, match="non-commercial"):
        p.fetch([spot], run)  # type: ignore[arg-type]
    assert rec.calls == []


def test_acquisition_records_a_permanent_failure_without_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.forecasts import ingestion

    monkeypatch.setenv("FORECAST_PROVIDERS", "open_meteo,open_meteo_ecmwf")
    monkeypatch.setenv("COMMERCIAL_USE", "true")
    monkeypatch.delenv("OPEN_METEO_API_KEY", raising=False)
    get_settings.cache_clear()
    sent: list[object] = []
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: sent.append(a) or None)
    try:
        outcomes = ingestion.acquire_all(object())  # type: ignore[arg-type]  # no DB is used
    finally:
        get_settings.cache_clear()
    assert [(o.source_code, o.status, o.retryable) for o in outcomes] == [
        ("open_meteo", "failed", False),
        ("open_meteo_ecmwf", "failed", False),
    ]
    assert all("non-commercial" in (o.message or "") for o in outcomes)
    assert sent == []


def test_check_config_names_the_missing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app import cli

    monkeypatch.setenv("FORECAST_PROVIDERS", "open_meteo")
    monkeypatch.setenv("COMMERCIAL_USE", "true")
    monkeypatch.delenv("OPEN_METEO_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main(["check-config"]) == 1
        missing = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["missing"]
        assert (
            "OPEN_METEO_API_KEY (COMMERCIAL_USE=true: the free Open-Meteo API is non-commercial)"
            in missing
        )
        monkeypatch.setenv("OPEN_METEO_API_KEY", KEY)
        get_settings.cache_clear()
        assert cli.main(["check-config"]) == 0
        assert KEY not in capsys.readouterr().out  # the key itself is never printed
    finally:
        get_settings.cache_clear()
