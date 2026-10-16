"""NOAA CO-OPS client: parsing of real captured answers (tests/fixtures/coops), the request
it sends, the HTTP-200 error body, subordinate-station rules and the cosine curve."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.http import PermanentProviderError
from app.services.tides import coops

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coops"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _client(handler: Any, **settings: Any) -> coops.CoopsClient:
    return coops.CoopsClient(
        Settings(**settings),
        httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
    )


def test_hilo_of_a_subordinate_station_parses_as_utc_events() -> None:
    points = coops.parse_predictions(_load("hilo_1612668.json"), "MLLW")
    assert len(points) == 8
    assert all(p.t.tzinfo is UTC and p.datum == "MLLW" and not p.interpolated for p in points)
    assert points[0].t == datetime(2026, 10, 14, 1, 59, tzinfo=UTC)
    assert (points[0].event, points[0].height_m) == ("high", 0.218)
    assert [p.event for p in points] == ["high", "low"] * 4  # high and low alternate


def test_hourly_curve_and_msl_offset_of_a_harmonic_station() -> None:
    mllw = {p.t: p.height_m for p in coops.parse_predictions(_load("h_9414131.json"), "MLLW")}
    msl = {p.t: p.height_m for p in coops.parse_predictions(_load("msl_9414131.json"), "MSL")}
    assert len(mllw) == 7 and len(msl) == 4
    hourly = coops.parse_predictions(_load("h_9414131.json"), "MLLW")
    assert all(p.event is None for p in hourly)
    shared = sorted(set(mllw) & set(msl))
    assert shared
    # MSL is 0.911 m above MLLW at Pillar Point (CO-OPS rounds to the millimetre).
    assert all(abs(mllw[t] - msl[t] - 0.911) <= 0.002 for t in shared)
    datums = coops.parse_datums(_load("datums_9414131.json"))
    assert datums["MLLW"] == 0.0 and datums["MSL"] == pytest.approx(0.911)
    assert "GT" not in datums and "HWI" not in datums  # ranges and intervals are not levels
    # A station without datums answers "datums": null (9414275 on 2026-10-15).
    assert coops.parse_datums({"units": "meters", "datums": None, "epoch": None}) == {}
    with pytest.raises(PermanentProviderError):
        coops.parse_datums({"units": "meters"})


def test_an_error_body_at_http_200_raises() -> None:
    body = _load("h_1612668_error.json")
    with pytest.raises(PermanentProviderError, match="No Predictions data was found"):
        coops.parse_predictions(body, "MLLW")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _client(handler)
    start = datetime(2026, 10, 14, tzinfo=UTC)
    with pytest.raises(PermanentProviderError):
        client.predictions("1612668", start, start + timedelta(days=2), "h", "MLLW")


def test_the_request_asks_for_utc_metric_json_with_the_application_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_load("hilo_1612668.json"))

    client = _client(handler, noaa_coops_application="SwellTravelAgentTest")
    start = datetime(2026, 10, 14, tzinfo=UTC)
    points = client.predictions(
        "1612668", start, start + timedelta(days=2), "hilo", "MLLW", kind="subordinate"
    )
    assert len(points) == 8 and client.requests == 1
    params = dict(seen[0].url.params)
    assert seen[0].url.path == "/api/prod/datagetter"
    assert params == {
        "product": "predictions",
        "application": "SwellTravelAgentTest",
        "station": "1612668",
        "begin_date": "20261014 00:00",
        "end_date": "20261016 00:00",
        "datum": "MLLW",
        "time_zone": "gmt",
        "units": "metric",
        "interval": "hilo",
        "format": "json",
    }


def test_the_application_name_falls_back_to_app_name_and_is_never_an_email() -> None:
    assert coops.application_name(Settings(app_name="Swell Travel Agent")) == "Swell Travel Agent"
    assert (
        coops.application_name(
            Settings(noaa_coops_application="someone@example.com", app_name="SwellApp")
        )
        == "SwellApp"
    )
    assert "@" not in coops.application_name(
        Settings(noaa_coops_application="a@b.c", app_name="x@y.z")
    )


def test_a_subordinate_station_refuses_anything_but_hilo_relative_to_mllw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request expected")

    client = _client(handler)
    start = datetime(2026, 10, 14, tzinfo=UTC)
    for interval, datum in (("h", "MLLW"), ("30", "MLLW"), ("hilo", "MSL")):
        with pytest.raises(PermanentProviderError, match="subordinate"):
            client.predictions(
                "1612668", start, start + timedelta(days=1), interval, datum, kind="subordinate"
            )
    with pytest.raises(PermanentProviderError, match="invalid station id"):
        client.predictions("../x", start, start + timedelta(days=1), "hilo", "MLLW")


def test_station_list_kinds_and_reference_stations() -> None:
    stations = coops.parse_station_list(_load("stations_tidepredictions.trimmed.json"))
    assert stations["1612668"].kind == "subordinate"
    assert stations["1612668"].reference_code == "1612340"
    assert stations["9414131"].kind == "harmonic" and stations["9414131"].reference_code is None
    assert stations["TWC0419"].kind == "subordinate"


def test_cosine_curve_meets_every_event_and_is_monotonic_between_them() -> None:
    events = coops.parse_predictions(_load("hilo14_9414131.json"), "MLLW")
    assert len(events) == 54
    curve = coops.cosine_curve(events, timedelta(minutes=6), events[0].t)
    assert all(p.interpolated and p.event is None and p.datum == "MLLW" for p in curve)
    by_time = {p.t: p.height_m for p in curve}
    for a, b in zip(events, events[1:], strict=False):
        inside = [by_time[t] for t in sorted(by_time) if a.t <= t <= b.t]
        assert inside
        lo, hi = sorted((a.height_m, b.height_m))
        assert all(lo - 1e-9 <= h <= hi + 1e-9 for h in inside)
        steps = [y - x for x, y in zip(inside, inside[1:], strict=False)]
        assert (
            all(s >= -1e-9 for s in steps)
            if b.height_m > a.height_m
            else all(s <= 1e-9 for s in steps)
        )
    # On a grid that contains the event times, the curve passes through every event.
    minute = coops.cosine_curve(events, timedelta(minutes=1), events[0].t)
    at = {p.t: p.height_m for p in minute}
    assert all(abs(at[e.t] - e.height_m) < 1e-6 for e in events)


def test_cosine_curve_refuses_mixed_datums() -> None:
    t = datetime(2026, 10, 14, tzinfo=UTC)
    events = [
        coops.TidePoint(t, 0.0, "MLLW", "low"),
        coops.TidePoint(t + timedelta(hours=6), 1.0, "MSL", "high"),
    ]
    with pytest.raises(ValueError, match="datums"):
        coops.cosine_curve(events, timedelta(minutes=30))
