"""Windy -> Open-Meteo fallback within the same refresh (Swell 2.0 design D4).

When the refresh of the Windy primary fails with a failure the provider manager holds (a
refused key, a testing key, the daily quota), ``refresh.refresh_spots`` rebuilds the token
(``registry.build_provider`` then returns the fallback, marked ``fallback_from``) and refreshes
again in the same invocation; the published version records the fallback's provider and
``fallback_from="windy"``. A failure the manager does not hold publishes nothing.

Every provider here is a labelled test double built on the demo generator (SYNTHETIC); the
registry's choice is mimicked by the ``build`` fixture from the recorded provider health,
as ``registry._stand_in`` does with ``provider_manager.active_provider``. No live provider
is contacted.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.services.forecasts import ingestion, provider_manager, registry
from app.services.forecasts.base import RunInfo
from app.services.forecasts.versions import current_version
from app.services.forecasts.windy import WindyCredentialsError
from app.services.http import PermanentProviderError
from app.services.sync import orchestrator
from tests.integration.test_refresh_components import C00, SplitDemo

pytestmark = pytest.mark.integration


class FailingWindy(SplitDemo):
    """SYNTHETIC: stands for the Windy adapter; its fetch fails. ``kind`` "credentials"
    records the failure first (as windy.py does before raising), None raises a failure the
    manager does not hold."""

    code = "windy_test_double"
    kind: ClassVar[str | None] = "credentials"
    fetches: ClassVar[int] = 0

    def fetch(self, spots: Any, run: RunInfo, components: frozenset[str] | None = None) -> Any:
        type(self).fetches += 1
        if self.kind is not None:
            provider_manager.record_provider_failure(
                "windy", self.kind, "synthetic: HTTP 401 Invalid API key"
            )
            raise WindyCredentialsError("synthetic: Windy refused the API key")
        raise PermanentProviderError("synthetic: HTTP 400 for this request")


class StandIn(SplitDemo):
    """SYNTHETIC: stands for the Open-Meteo fallback (the demo generator's records)."""

    fetches: ClassVar[int] = 0

    def fetch(self, spots: Any, run: RunInfo, components: frozenset[str] | None = None) -> Any:
        type(self).fetches += 1
        return super().fetch(spots, run, components)


@pytest.fixture
def windy_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    SplitDemo.cycles = {"wave": C00, "atmosphere": C00}
    SplitDemo.calls = []
    SplitDemo.failing = set()
    SplitDemo.tamper = {}
    SplitDemo.publishing = set()
    SplitDemo.unreadable = set()
    SplitDemo.entered = SplitDemo.gate = None
    FailingWindy.kind = "credentials"
    FailingWindy.fetches = StandIn.fetches = 0
    provider_manager.get_redis().delete(provider_manager.health_key("windy"))
    monkeypatch.setattr(get_settings(), "forecast_providers", "windy")

    def build(name: str, settings: object = None, overrides: Any = None) -> Any:
        assert name == "windy"
        health = provider_manager.provider_health("windy")
        if health is not None and health.blocking(utcnow()):
            provider = StandIn(overrides=overrides, natural_swells=False)
            setattr(provider, registry.FALLBACK_ATTRIBUTE, "windy")  # noqa: B010
            return provider
        return FailingWindy(overrides=overrides, natural_swells=False)

    monkeypatch.setattr(registry, "build_provider", build)
    monkeypatch.setattr(ingestion, "build_provider", build)


def refresh(now: datetime | None = None) -> dict[str, Any]:
    out = orchestrator.refresh_check(trigger="http", now=now)
    return json.loads(json.dumps(out, default=str))


def test_a_refused_windy_key_falls_back_to_open_meteo_in_the_same_refresh(
    db: Session, few_spots: list[str], windy_primary: None
) -> None:
    out = refresh()
    assert out["status"] in ("success", "partial"), out
    assert FailingWindy.fetches == 1 and StandIn.fetches == 1  # one invocation, both tried
    assert len(out["version_ids"]) == 1
    db.expire_all()
    version = current_version(db)
    assert version is not None and version.id == out["version_ids"][0]
    # The version records who supplied the data and who it stood in for (design D4).
    assert version.fallback_from == "windy"
    assert version.source_code == "demo" and version.provider == "demo"
    assert version.spots_updated == len(few_spots)
    stages = {s["name"]: s["status"] for s in out["stages"]}
    assert stages["spots"] == "success" and stages["publish"] == "success"
    # The failed Windy attempt stays on record as a failed run of its own source.
    failed = db.execute(
        select(ForecastRun.status)
        .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
        .where(ForecastSource.code == FailingWindy.code)
    ).scalars()
    assert list(failed) == [ForecastRunStatus.FAILED]
    # The admin side sees the held fallback.
    health = provider_manager.provider_health("windy")
    assert health is not None and health.last_failure_kind == "credentials"


def test_a_failure_the_manager_does_not_hold_publishes_nothing(
    db: Session, few_spots: list[str], windy_primary: None
) -> None:
    FailingWindy.kind = None
    out = refresh()
    assert FailingWindy.fetches == 1 and StandIn.fetches == 0  # no stand-in for a 400
    assert out["version_ids"] == []
    db.expire_all()
    assert current_version(db) is None
