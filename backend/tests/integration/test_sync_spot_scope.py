"""Spot scope of a sync: only the requested providers, only active spots. Owned by the
sync-core package (it owns app.services.forecasts.ingestion).

Created by the foundation with ``acquire_all(only=)`` (F7), which keeps today's behaviour
when ``only`` is None. sync-core adds the inactive-spot scenario of its design.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import get_settings

pytestmark = pytest.mark.integration


def test_acquire_all_only_limits_the_providers_and_keeps_priorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.forecasts import ingestion

    settings = get_settings()
    monkeypatch.setattr(settings, "forecast_providers", "open_meteo,open_meteo_ecmwf,noaa_gfs")
    built: list[str] = []
    acquired: list[tuple[str, int]] = []

    def build(name: str, _settings: object, overrides: object = None) -> str:
        built.append(name)
        return name

    def acquire(_db: object, provider: str, **kw: Any) -> str:
        acquired.append((provider, kw["priority"]))
        return provider

    monkeypatch.setattr(ingestion, "build_provider", build)
    monkeypatch.setattr(ingestion, "acquire_from_provider", acquire)
    every = ingestion.acquire_all(None)  # type: ignore[arg-type]
    assert every == ["open_meteo", "open_meteo_ecmwf", "noaa_gfs"]  # type: ignore[comparison-overlap]
    assert acquired == [("open_meteo", 0), ("open_meteo_ecmwf", 1), ("noaa_gfs", 2)]
    built.clear()
    acquired.clear()
    some = ingestion.acquire_all(None, only={"noaa_gfs", "open_meteo"})  # type: ignore[arg-type]
    assert some == ["open_meteo", "noaa_gfs"]  # type: ignore[comparison-overlap]
    assert built == ["open_meteo", "noaa_gfs"]
    assert acquired == [("open_meteo", 0), ("noaa_gfs", 2)]
    assert ingestion.acquire_all(None, only=()) == []  # type: ignore[arg-type]
    assert built == ["open_meteo", "noaa_gfs"]  # nothing else was contacted
