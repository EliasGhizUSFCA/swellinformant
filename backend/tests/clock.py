"""A frozen wall clock for tests.

Every module reads the wall clock through ``app.core.database.utcnow`` (imported by name),
which calls ``datetime.now(UTC)`` from that module's globals. Replacing that one global
freezes the clock for all of them, the API's request handlers included, so a test whose
outcome depends on the date (the demo provider's natural swells follow calendar slots) runs
the same on every day.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

import pytest

import app.core.database as database


def freeze_utcnow(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    """Make ``utcnow()`` return ``at`` (timezone-aware) until the test ends."""
    assert at.tzinfo is not None

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

    monkeypatch.setattr(database, "datetime", FrozenDatetime)
