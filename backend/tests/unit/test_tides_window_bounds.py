"""app.api.tides window bounds: an instant without a zone is UTC, one with a zone is
converted to UTC, and one outside 1900..2200 (or not expressible in UTC at all) is an
AppError, the API's HTTP 400."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.api.tides import EARLIEST, LATEST, _bounded
from app.core.errors import AppError

PLUS_5 = timezone(timedelta(hours=5))


def test_naive_instants_are_utc_and_aware_ones_are_converted() -> None:
    assert _bounded(datetime(2026, 10, 14, 6), "start") == datetime(2026, 10, 14, 6, tzinfo=UTC)
    converted = _bounded(datetime(2026, 10, 14, 6, tzinfo=PLUS_5), "start")
    assert converted == datetime(2026, 10, 14, 1, tzinfo=UTC)
    assert converted.tzinfo is UTC and converted.hour == 1
    assert _bounded(EARLIEST, "start") == EARLIEST
    assert _bounded(LATEST, "end") == LATEST


@pytest.mark.parametrize(
    ("when", "message"),
    [
        (datetime(1, 1, 1, tzinfo=PLUS_5), "end is out of range"),  # before year 1 in UTC
        (datetime(1899, 12, 31, 23, 59, tzinfo=UTC), "between 1900 and 2200"),
        (datetime(2200, 1, 1, 0, 0, 1), "between 1900 and 2200"),
    ],
)
def test_instants_outside_the_window_are_refused(when: datetime, message: str) -> None:
    with pytest.raises(AppError, match=message):
        _bounded(when, "end")
