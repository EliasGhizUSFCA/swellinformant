"""SYNTHETIC Windy Point Forecast API v2 responses, written from the documented format.

No Windy key exists for this project, so nothing here was recorded from Windy. The shapes
follow https://api.windy.com/point-forecast/docs (read 2026-10-27): ``ts`` in milliseconds
since 1970, a ``units`` object, one array per ``<parameter>-surface`` key, ``null`` for "no
value at that time", a missing key for "no forecast for that combination"; ``windWaves``
answers ``wwaves_*``; wind as ``wind_u``/``wind_v`` in m/s; pressure in Pa (Windy staff's
example response, community.windy.com topic 36428); 3-hourly steps (Windy staff, 2026); the
testing-key ``warning`` quoted from that same staff example. Every VALUE is synthetic.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.services.forecasts.windy import TESTING_WARNING

STEP_MS = 3 * 3600 * 1000
START = datetime(2026, 10, 27, 0, tzinfo=UTC)
START_MS = int(START.timestamp() * 1000)
GFS_UNITS = {
    "wind_u-surface": "m*s-1",
    "wind_v-surface": "m*s-1",
    "gust-surface": "m*s-1",
    "pressure-surface": "Pa",
}
WAVE_UNITS = {
    **{f"{p}_height-surface": "m" for p in ("waves", "swell1", "swell2", "wwaves")},
    **{f"{p}_period-surface": "s" for p in ("waves", "swell1", "swell2", "wwaves")},
    **{f"{p}_direction-surface": "deg" for p in ("waves", "swell1", "swell2", "wwaves")},
}


def times(steps: int, start_ms: int = START_MS) -> list[int]:
    return [start_ms + STEP_MS * i for i in range(steps)]


def uv_from(speed_ms: float, from_deg: float) -> tuple[float, float]:
    """u, v (m/s, the direction the air moves TO) of a wind blowing FROM ``from_deg``."""
    rad = math.radians(from_deg)
    return -speed_ms * math.sin(rad), -speed_ms * math.cos(rad)


def gfs_body(
    steps: int = 8,
    *,
    start_ms: int = START_MS,
    speed_ms: float = 5.0,
    from_deg: float = 45.0,
    gust_ms: float = 8.0,
    pressure_pa: float = 101_300.0,
    shift: float = 0.0,
    drop: Iterable[str] = (),
) -> dict[str, Any]:
    """A gfs response (``wind``, ``windGust``, ``pressure``); ``shift`` changes every value
    (another run), ``drop`` leaves series out."""
    u, v = uv_from(speed_ms + shift, from_deg)
    body: dict[str, Any] = {
        "ts": times(steps, start_ms),
        "units": dict(GFS_UNITS),
        "wind_u-surface": [u] * steps,
        "wind_v-surface": [v] * steps,
        "gust-surface": [gust_ms + shift] * steps,
        "pressure-surface": [pressure_pa + 100 * shift] * steps,
    }
    for key in drop:
        body.pop(key, None)
        body["units"].pop(key, None)
    return body


def wave_body(
    steps: int = 8,
    *,
    start_ms: int = START_MS,
    hs: float = 2.0,
    shift: float = 0.0,
    drop: Iterable[str] = (),
) -> dict[str, Any]:
    """A gfsWave response (``waves``, ``windWaves``, ``swell1``, ``swell2``)."""
    body: dict[str, Any] = {
        "ts": times(steps, start_ms),
        "units": dict(WAVE_UNITS),
        "waves_height-surface": [hs + shift] * steps,
        "waves_period-surface": [12.0] * steps,
        "waves_direction-surface": [300.0] * steps,
        "swell1_height-surface": [1.8 + shift] * steps,
        "swell1_period-surface": [14.0] * steps,
        "swell1_direction-surface": [305.0] * steps,
        "swell2_height-surface": [0.6] * steps,
        "swell2_period-surface": [9.0] * steps,
        "swell2_direction-surface": [200.0] * steps,
        "wwaves_height-surface": [0.4] * steps,
        "wwaves_period-surface": [4.0] * steps,
        "wwaves_direction-surface": [60.0] * steps,
    }
    for key in drop:
        body.pop(key, None)
        body["units"].pop(key, None)
    return body


def as_testing_key(body: dict[str, Any]) -> dict[str, Any]:
    """The same body as a TESTING key answers it (the documented trial warning)."""
    return {**body, "warning": TESTING_WARNING}


Responder = Callable[[dict[str, Any]], httpx.Response | dict[str, Any]]


@dataclass
class WindyServer:
    """MockTransport handler: answers per model (``gfs`` / ``gfsWave``) from callables of
    the request body; records every request body (the key is kept to check it is sent)."""

    gfs: Responder = field(default=lambda body: gfs_body())
    wave: Responder = field(default=lambda body: wave_body())
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        responder = self.gfs if body["model"] == "gfs" else self.wave
        out = responder(body)
        return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def models(self) -> list[str]:
        return [c["model"] for c in self.calls]

    def points(self, model: str) -> list[tuple[float, float]]:
        return [(c["lat"], c["lon"]) for c in self.calls if c["model"] == model]


def at_points(responses: Mapping[tuple[float, float], dict[str, Any]], default: dict[str, Any]):  # type: ignore[no-untyped-def]
    """A responder answering per (lat, lon) of the request, else ``default``."""

    def respond(body: dict[str, Any]) -> dict[str, Any]:
        return responses.get((body["lat"], body["lon"]), default)

    return respond
