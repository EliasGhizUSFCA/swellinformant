"""Windy through the real acquisition path (forecasts.ingestion) on the test database.

Every Windy response is SYNTHETIC (tests/windy_fixtures.py, written from the documented
format; there is no Windy key). Checks: a testing key's data are never stored (no forecast
row, no version) and switch the next build to Open-Meteo; a licensed answer is stored with
Windy's 3-hourly steps, estimated run times and the request count of design D3.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ForecastRun, ForecastSource, WaveForecast
from app.models.version import ForecastVersion
from app.services.forecasts import ingestion, provider_manager, registry, windy
from app.services.forecasts.open_meteo import OpenMeteoProvider
from app.services.forecasts.windy import WindyProvider
from tests.windy_fixtures import WindyServer, as_testing_key, gfs_body, wave_body

pytestmark = pytest.mark.integration


@pytest.fixture
def windy_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "forecast_providers", "windy")
    monkeypatch.setattr(s, "windy_api_key", "test-only-windy-key")
    monkeypatch.setattr(s, "windy_storage_authorized", True)


def serve(monkeypatch: pytest.MonkeyPatch, server: WindyServer) -> None:
    monkeypatch.setattr(windy, "http_client", lambda *a, **k: server.client())


def windy_rows(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(WaveForecast.id))
            .join(ForecastRun, WaveForecast.run_id == ForecastRun.id)
            .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
            .where(ForecastSource.code == WindyProvider.code)
        )
        or 0
    )


def test_testing_key_data_are_never_stored_and_switch_to_open_meteo(
    db: Session, few_spots: list[str], windy_primary: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = WindyServer(
        gfs=lambda b: as_testing_key(gfs_body()), wave=lambda b: as_testing_key(wave_body())
    )
    serve(monkeypatch, server)
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.source_code == WindyProvider.code and outcome.status == "failed"
    assert "windy_testing_key" in outcome.message and not outcome.retryable
    assert len(server.calls) == 1  # the first sentinel answer ends it
    assert windy_rows(db) == 0
    assert db.scalar(select(func.count(ForecastRun.id))) == 0  # no run row either
    assert db.scalar(select(func.count(ForecastVersion.id))) == 0
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == provider_manager.TESTING_KEY
    # The next refresh builds Open-Meteo for the token "windy" (no request is sent here).
    built = registry.build_provider("windy")
    assert isinstance(built, OpenMeteoProvider)
    assert registry.fallback_origin(built) == "windy"


def test_testing_data_seen_during_a_fetch_store_nothing(
    db: Session, few_spots: list[str], windy_primary: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def wave(body: dict[str, object]) -> dict[str, object]:
        calls["n"] += 1
        return wave_body() if calls["n"] <= 2 else as_testing_key(wave_body())

    server = WindyServer(wave=wave)
    serve(monkeypatch, server)
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.status == "failed" and "windy_testing_key" in outcome.message
    run = db.scalar(select(ForecastRun))
    assert run is not None and run.status.value == "failed"  # recorded, without records
    assert windy_rows(db) == 0


def test_a_licensed_windy_answer_is_stored_with_estimated_runs(
    db: Session, few_spots: list[str], windy_primary: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = WindyServer(gfs=lambda b: gfs_body(40), wave=lambda b: wave_body(40))
    serve(monkeypatch, server)
    (outcome,) = ingestion.acquire_all(db)
    assert outcome.status == "success", outcome.message
    run = db.get(ForecastRun, outcome.run_id)
    assert run is not None
    # 5 spots x 2 requests; Pipeline (a spot here) is the sentinel check: nothing more.
    assert run.request_count == 10
    assert run.details["metadata_source"] == "windy_fingerprint"
    assert [m["model"] for m in run.details["model_runs"]] == ["gfsWave", "gfs"]
    assert run.run_key.startswith("gfsWave~") and "|gfs~" in run.run_key
    assert run.spot_count == 5 and windy_rows(db) == 5 * 40
    times = db.scalars(
        select(WaveForecast.valid_time)
        .where(WaveForecast.run_id == run.id, WaveForecast.spot_id == run.details["spot_ids"][0])
        .order_by(WaveForecast.valid_time)
    ).all()
    assert {b - a for a, b in zip(times, times[1:], strict=False)} == {timedelta(hours=3)}
    row = db.scalar(select(WaveForecast).where(WaveForecast.run_id == run.id).limit(1))
    assert row is not None
    assert row.wind_speed_kmh == pytest.approx(18.0) and row.pressure_msl_hpa == pytest.approx(
        1013.0
    )
    assert row.tertiary_swell_height_m is None and "tertiary_swell_height_m" in row.missing_fields
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_success_at is not None
    # The same run again: the sentinel is not due yet, nothing new is fetched.
    server.calls.clear()
    (again,) = ingestion.acquire_all(db)
    assert again.status == "skipped" and server.calls == []
