"""Fixtures of the sync tests (tests/integration/test_sync_*.py, test_pipeline.py).

The map layers come from the SYNTHETIC NODD bucket of tests.synthetic_gfs (constant fields
in the real file layout, made up for testing; not model output): the standalone map-field
provider is replaced by one reading that bucket, for the 18Z cycle of 2026-10-10 with the
map hours f000, f012 and f024. The spot forecasts come from the demo provider (synthetic
by design). ``NOW`` is the time the syncs of these tests run at.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.config import get_settings
from tests.synthetic_gfs import SyntheticGfs
from tests.synthetic_map_fields import add_map_hours, field_settings

NOW = datetime(2026, 10, 10, 23, 40, tzinfo=UTC)  # nominal cycle with a 5 h lag: 18Z
CYCLE = datetime(2026, 10, 10, 18, tzinfo=UTC)
HOURS = [0, 12, 24]


@dataclass
class MapWorld:
    """The synthetic bucket behind every NoaaGfsFieldsProvider built while it is installed."""

    world: SyntheticGfs
    built: int = 0

    def fail_everything(self, status: int = 503) -> None:
        for path in list(self.world.files):
            self.world.fail[path] = status


def install_synthetic_map(monkeypatch: pytest.MonkeyPatch) -> MapWorld:
    """MAP_FIELDS_ENABLED=true (the standalone mode with the demo spot provider) and the
    map-field provider reading the synthetic bucket."""
    from app.services.forecasts import noaa

    real = noaa.NoaaGfsFieldsProvider
    world = SyntheticGfs()
    add_map_hours(world, CYCLE, HOURS)
    out = MapWorld(world)
    settings = field_settings(map_lead=24, providers="demo", map_fields_enabled=True)

    def factory(*_args: Any, **_kw: Any) -> noaa.NoaaGfsFieldsProvider:
        out.built += 1
        return real(settings, client=world.client(), backoff_seconds=0)

    monkeypatch.setattr(noaa, "NoaaGfsFieldsProvider", factory)
    monkeypatch.setattr(get_settings(), "map_fields_enabled", True)
    return out


@dataclass
class QueryCounter:
    """Counts statements and connection checkouts on an engine while installed."""

    statements: list[str]
    checkouts: int = 0

    @property
    def total(self) -> int:
        return len(self.statements) + self.checkouts


def count_queries(engine: Engine) -> tuple[QueryCounter, Callable[[], None]]:
    """Install SQLAlchemy event listeners (before_cursor_execute and pool checkout) and
    return the counter and a function that removes them."""
    counter = QueryCounter(statements=[])

    def on_execute(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        counter.statements.append(statement)

    def on_checkout(*_args: Any) -> None:
        counter.checkouts += 1

    event.listen(engine, "before_cursor_execute", on_execute)
    event.listen(engine.pool, "checkout", on_checkout)

    def remove() -> None:
        event.remove(engine, "before_cursor_execute", on_execute)
        event.remove(engine.pool, "checkout", on_checkout)

    return counter, remove
