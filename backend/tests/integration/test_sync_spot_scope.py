"""Spot scope of a sync: only the requested providers, only the active seeded spots.

``acquire_all(only=)`` contacts only the FORECAST_PROVIDERS tokens given and keeps
their priorities (None: every provider, as before); a sync never asks a provider for a
spot that is not active (D4).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ForecastRun, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.services.forecasts.base import FetchResult, RunInfo, SpotPoint

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


def test_a_sync_never_requests_an_inactive_spot(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point forecasts only for the active seeded spots: a deactivated spot is never sent to
    the provider during a sync (D4). The provider is the demo one (synthetic data)."""
    from app.services.forecasts import ingestion
    from app.services.forecasts.demo import DemoForecastProvider
    from app.services.sync.orchestrator import run_sync

    requested: list[list[str]] = []

    class Recording(DemoForecastProvider):
        def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
            requested.append(sorted(p.slug for p in spots))
            return super().fetch(spots, run)

    real_build = ingestion.build_provider

    def build(name: str, settings: object = None, overrides: object = None) -> Any:
        provider = real_build(name, settings, overrides)  # type: ignore[arg-type]
        assert isinstance(provider, DemoForecastProvider)
        return Recording(
            provider.forecast_days,
            overrides=provider.overrides,
            natural_swells=provider.natural_swells,
        )

    monkeypatch.setattr(ingestion, "build_provider", build)
    inactive = "nazare"
    db.execute(text("UPDATE surf_spots SET is_active = false WHERE slug = :s"), {"s": inactive})
    db.commit()
    active = sorted(s for s in few_spots if s != inactive)

    result = run_sync(trigger="cli")
    assert result.status == "success"
    assert requested == [active]  # one request set, active spots only
    db.expire_all()
    spot_id = db.scalar(select(SurfSpot.id).where(SurfSpot.slug == inactive))
    stored = db.scalar(select(func.count()).where(WaveForecast.spot_id == spot_id))
    assert stored == 0
    run = db.scalar(select(ForecastRun).where(ForecastRun.status == ForecastRunStatus.SUCCESS))
    assert run is not None and spot_id not in run.details["spot_ids"]


def test_a_sync_acquires_only_the_spot_providers_its_plan_marked_new(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spots stage asks acquire_all for the new components' providers only: a run
    waiting for its coalescing partner or in a retry wait (e.g. after Open-Meteo's daily
    limit) is not fetched by a sync another component triggered. Run keys are SYNTHETIC."""
    from datetime import UTC, datetime

    from app.services.forecasts import ingestion
    from app.services.sync import orchestrator
    from app.services.sync.types import ComponentStatus, SyncPlan

    now = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)
    asked: list[frozenset[str] | None] = []
    # Two providers: the family-level acquisition (acquire_all). With one provider the spot
    # stage refreshes per component instead (design D6, tests/integration/
    # test_refresh_components.py).
    monkeypatch.setattr(get_settings(), "forecast_providers", "open_meteo,open_meteo_ecmwf")

    def recorder(_db: object, now: object = None, only: Any = None) -> list[Any]:
        asked.append(frozenset(only) if only is not None else None)
        return []

    monkeypatch.setattr(ingestion, "acquire_all", recorder)

    def component(source: str, provider: str, state: Any) -> ComponentStatus:
        return ComponentStatus(
            kind="spots",
            source_code=source,
            provider=provider,
            state=state,
            stored_run_key=f"{provider}:2026101012",
            available_run_key=f"{provider}:2026101018",
            available_cycle=datetime(2026, 10, 10, 18, tzinfo=UTC),
        )

    plan = SyncPlan(
        checked_at=now,
        due=True,
        components=(
            component("open_meteo_noaa", "open_meteo", "new"),
            component("open_meteo_ecmwf", "open_meteo_ecmwf", "waiting"),
        ),
        anchor_cycle=None,
        reasons=("synthetic",),
    )
    result = orchestrator.run_sync(trigger="daemon", plan=plan, now=now)
    assert asked == [frozenset({"open_meteo"})]
    spots = next(s for s in result.stages if s.name == "spots")
    assert [c["source_code"] for c in spots.detail["components"]] == ["open_meteo_noaa"]
