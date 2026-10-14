"""Loaders for the spot timeline fixtures (tests/fixtures/timeline, see its README).

``jbay_blend_3h.json`` holds real stored rows (one blend run for Jeffreys Bay, 3-hourly).
The helpers here turn them into transient ORM objects and, for the cadence and time-zone
cases, re-time them. Re-timed series are SYNTHETIC: real values placed at made-up valid
times, used only to exercise step and day logic in tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from app.models import SurfQualityPrediction, SurfSpot, WaveForecast
from app.models.enums import ConfidenceLabel, QualityLabel

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "timeline"
Rows = list[tuple[SurfQualityPrediction, WaveForecast]]


@cache
def jbay_doc() -> dict[str, Any]:
    return json.loads((FIXTURES / "jbay_blend_3h.json").read_text())  # type: ignore[no-any-return]


def _parse(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
        return datetime.fromisoformat(value)
    return value


def build_rows(times: list[datetime] | None = None) -> Rows:
    """Transient (prediction, forecast) pairs from the J-Bay fixture, optionally re-timed to
    ``times`` (one per row, in order; at most as many as the fixture has)."""
    raw = jbay_doc()["rows"]
    if times is not None:
        raw = raw[: len(times)]
    out: Rows = []
    for i, row in enumerate(raw):
        p = {k: _parse(v) for k, v in row["prediction"].items()}
        f = dict(row["forecast"])
        t = times[i] if times is not None else p["valid_time"]
        p["valid_time"] = t
        p["label"] = QualityLabel(p["label"])
        p["confidence_label"] = ConfidenceLabel(p["confidence_label"])
        forecast = WaveForecast(valid_time=t, issued_at=p["issued_at"], **f)
        pred = SurfQualityPrediction(**p, forecast=forecast)
        out.append((pred, forecast))
    return out


def jbay_spot() -> SurfSpot:
    s = jbay_doc()["spot"]
    return SurfSpot(
        slug=s["slug"], timezone=s["timezone"], latitude=s["latitude"], longitude=s["longitude"]
    )


def spot(slug: str, tz: str, lat: float, lon: float) -> SurfSpot:
    """A transient spot (not stored) for the time-zone cases."""
    return SurfSpot(slug=slug, timezone=tz, latitude=lat, longitude=lon)


def regular_times(start: datetime, step_hours: float, n: int) -> list[datetime]:
    return [start + timedelta(hours=step_hours * i) for i in range(n)]


def ecmwf_shaped_times(start: datetime, n3: int, n6: int) -> list[datetime]:
    """3-hourly for ``n3`` steps, then 6-hourly for ``n6`` (the ECMWF open-data cadence)."""
    first = regular_times(start, 3, n3)
    return first + regular_times(first[-1] + timedelta(hours=6), 6, n6)
