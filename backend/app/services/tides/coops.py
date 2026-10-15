"""NOAA CO-OPS tide predictions (https://api.tidesandcurrents.noaa.gov; US Government work,
public domain; NOS asks for attribution, https://tidesandcurrents.noaa.gov/disclaimers.html).

Data API (https://api.tidesandcurrents.noaa.gov/api/prod/): ``datagetter?product=predictions``
with ``time_zone=gmt`` (times in UTC), ``units=metric`` (metres), ``format=json``, a
``datum`` and an ``interval``. Every request carries the ``application`` parameter CO-OPS asks
callers to send (NOAA_COOPS_APPLICATION, else APP_NAME; never an e-mail address). From the
API documentation (checked 2026-10-14):

* "Harmonic tide prediction stations can provide tide predictions on any available
  interval. Subordinate tide prediction stations can only provide tide predictions on a
  high / low interval." and "Subordinate tide prediction stations must use datum=MLLW".
  A subordinate station asked for another interval answers HTTP **200** with
  ``{"error": {"message": "No Predictions data was found. ..."}}`` (captured:
  tests/fixtures/coops/h_1612668_error.json); any ``error`` body raises
  :class:`PermanentProviderError` rather than being read as "no tides".
* High/low predictions: up to 10 years per request; other intervals: 1 year.

Metadata API (https://api.tidesandcurrents.noaa.gov/mdapi/prod/): the list of prediction
stations (``stations.json?type=tidepredictions``: ``type`` "R" harmonic, "S" subordinate,
``reference_id`` of a subordinate station; about 2 MB, read at most once a day per process)
and a station's datums (``stations/{id}/datums.json?units=metric``, heights above the
station datum, converted here to metres above MLLW).

A subordinate station publishes only its high and low waters. :func:`cosine_curve` fills the
curve between them with the classical cosine (half-period) interpolation

    h(t) = h1 + (h2 - h1) * (1 - cos(pi * (t - t1) / (t2 - t1))) / 2

which reproduces every event's height at its time and is monotonic between events. Those
points are flagged ``interpolated`` and are never presented as the station's own output.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from app.core.config import Settings, get_settings
from app.services.http import PermanentProviderError, request_with_retries

logger = logging.getLogger(__name__)

PROVIDER = "noaa_coops"
SOURCE = "noaa_coops"
LICENCE = "Public domain (US Government)"
ATTRIBUTION = "NOAA/NOS/CO-OPS tide predictions (tidesandcurrents.noaa.gov)"
DATAGETTER = "api/prod/datagetter"
STATIONS_PATH = "mdapi/prod/webapi/stations.json"
# CO-OPS station ids: digits (9414131) or letters and digits (TWC0419, TPT2779). They go
# into URL paths, so anything else is refused rather than requested.
STATION_ID_RE = re.compile(r"[A-Za-z0-9]{1,16}")
DATUMS = ("MLLW", "MSL", "MLW", "MHW", "MHHW", "MTL", "DTL", "STND", "NAVD")
INTERVALS = ("hilo", "h", "60", "30", "15", "10", "6", "5", "1")
STATION_LIST_TTL_S = 24 * 3600.0
# Requested windows are small (TIDE_HORIZON_DAYS <= 31); CO-OPS allows a year for curves.
MAX_DAYS = {"hilo": 3650}
MAX_DAYS_DEFAULT = 365

Interval = Literal["hilo", "h", "60", "30", "15", "10", "6", "5", "1"]
StationKind = Literal["harmonic", "subordinate"]


@dataclass(frozen=True)
class TidePoint:
    """One predicted height (metres above ``datum``) at ``t`` (UTC)."""

    t: datetime
    height_m: float
    datum: str
    # "high" / "low" for high/low-water predictions; None on a curve.
    event: Literal["high", "low"] | None = None
    # True for curve points computed here between a subordinate station's events.
    interpolated: bool = False


@dataclass(frozen=True)
class CoopsStation:
    code: str
    name: str
    latitude: float
    longitude: float
    kind: StationKind
    reference_code: str | None


_station_cache: dict[str, tuple[float, dict[str, CoopsStation]]] = {}
_station_lock = threading.Lock()


def application_name(settings: Settings) -> str:
    """The ``application`` parameter: NOAA_COOPS_APPLICATION or APP_NAME, never an e-mail."""
    name = settings.noaa_coops_application_name.strip()
    if "@" in name:
        logger.warning("NOAA_COOPS_APPLICATION looks like an e-mail address; sending APP_NAME")
        name = settings.app_name.strip()
    if "@" in name or not name:
        name = "SwellTravelAgent"
    return name[:64]


def _check_station(code: str) -> str:
    if not STATION_ID_RE.fullmatch(code):
        raise PermanentProviderError(f"noaa_coops: invalid station id {code!r}")
    return code


def _parse_time(raw: str) -> datetime:
    # time_zone=gmt: "2026-10-14 00:37" is UTC.
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def parse_predictions(body: Any, datum: str) -> list[TidePoint]:
    """A datagetter answer -> points (raises on an ``error`` body, even at HTTP 200)."""
    if not isinstance(body, dict):
        raise PermanentProviderError("noaa_coops: unexpected response (not a JSON object)")
    if "error" in body:
        error = body.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        raise PermanentProviderError(f"noaa_coops: {str(message)[:300]}")
    rows = body.get("predictions")
    if not isinstance(rows, list):
        raise PermanentProviderError("noaa_coops: response has no 'predictions' list")
    out: list[TidePoint] = []
    for row in rows:
        try:
            t = _parse_time(str(row["t"]))
            raw = str(row.get("v", "")).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise PermanentProviderError(f"noaa_coops: unreadable prediction {row!r}") from exc
        if not raw:
            continue  # missing stays missing
        try:
            height = float(raw)
        except ValueError as exc:
            raise PermanentProviderError(f"noaa_coops: unreadable height {raw!r}") from exc
        if not math.isfinite(height) or abs(height) > 25:
            raise PermanentProviderError(f"noaa_coops: height {height} m out of range")
        kind = row.get("type")
        event: Literal["high", "low"] | None = None
        if kind in ("H", "HH"):
            event = "high"
        elif kind in ("L", "LL"):
            event = "low"
        elif kind is not None:
            raise PermanentProviderError(f"noaa_coops: unknown event type {kind!r}")
        out.append(TidePoint(t, height, datum, event))
    out.sort(key=lambda p: p.t)
    return out


def parse_station_list(body: Any) -> dict[str, CoopsStation]:
    """``stations.json?type=tidepredictions`` -> stations by id."""
    rows = body.get("stations") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise PermanentProviderError("noaa_coops: station list has no 'stations'")
    out: dict[str, CoopsStation] = {}
    for row in rows:
        try:
            code = str(row["id"])
            kind: StationKind = "harmonic" if row.get("type") == "R" else "subordinate"
            if row.get("type") not in ("R", "S"):
                continue
            out[code] = CoopsStation(
                code=code,
                name=str(row.get("name") or code),
                latitude=float(row["lat"]),
                longitude=float(row["lng"]),
                kind=kind,
                reference_code=str(row.get("reference_id") or "") or None,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_datums(body: Any) -> dict[str, float]:
    """``datums.json?units=metric`` -> metres above MLLW (e.g. {"MSL": 0.911, "MLLW": 0.0})."""
    if not isinstance(body, dict) or "datums" not in body:
        raise PermanentProviderError("noaa_coops: datums answer has no 'datums'")
    rows = body["datums"]
    if rows is None:
        return {}  # the station publishes no datums (many subordinate stations)
    if not isinstance(rows, list):
        raise PermanentProviderError("noaa_coops: unreadable datums")
    units = str(body.get("units") or "").lower()
    if units not in ("meters", "metres", "m"):
        raise PermanentProviderError(f"noaa_coops: datums in {units!r}, metres expected")
    values: dict[str, float] = {}
    for row in rows:
        try:
            values[str(row["name"])] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    if "MLLW" not in values:
        return {}
    base = values["MLLW"]
    # Ranges and intervals (GT, MN, DHQ, DLQ, HWI, LWI) are not levels.
    levels = ("MHHW", "MHW", "DTL", "MTL", "MSL", "MLW", "MLLW", "NAVD88", "STND")
    return {k: round(v - base, 3) for k, v in values.items() if k in levels}


def cosine_curve(
    events: Sequence[TidePoint], step: timedelta, start: datetime | None = None
) -> list[TidePoint]:
    """Curve points every ``step`` between consecutive high/low waters (see module doc),
    aligned to ``start`` (default: the first event), flagged ``interpolated``."""
    ordered = sorted((e for e in events if e.event is not None), key=lambda e: e.t)
    if len(ordered) < 2:
        return []
    datum = ordered[0].datum
    if any(e.datum != datum for e in ordered):
        raise ValueError("events of different datums cannot form one curve")
    origin = start or ordered[0].t
    seconds = step.total_seconds()
    k = math.ceil((ordered[0].t - origin).total_seconds() / seconds)
    t = origin + k * step
    out: list[TidePoint] = []
    i = 0
    while t <= ordered[-1].t:
        while i + 1 < len(ordered) - 1 and ordered[i + 1].t <= t:
            i += 1
        a, b = ordered[i], ordered[i + 1]
        span = (b.t - a.t).total_seconds()
        x = 0.0 if span <= 0 else min(1.0, max(0.0, (t - a.t).total_seconds() / span))
        height = a.height_m + (b.height_m - a.height_m) * (1.0 - math.cos(math.pi * x)) / 2.0
        out.append(TidePoint(t, round(height, 4), datum, None, interpolated=True))
        t += step
    return out


class CoopsClient:
    """Small client for the CO-OPS data and metadata APIs (no API key)."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.noaa_coops_base_url.rstrip("/")
        self.application = application_name(self.settings)
        # A client passed in belongs to the caller; one made here is closed by close().
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (tides)"},
        )
        self.backoff_seconds = backoff_seconds
        self.requests = 0
        self.bytes_downloaded = 0

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _get_json(self, path: str, params: dict[str, str]) -> Any:
        self.requests += 1
        response = request_with_retries(
            self.client,
            "GET",
            f"{self.base_url}/{path}",
            provider=PROVIDER,
            backoff_seconds=self.backoff_seconds,
            params=params,
        )
        if response.status_code != 200:
            raise PermanentProviderError(f"noaa_coops: HTTP {response.status_code} for {path}")
        self.bytes_downloaded += len(response.content)
        try:
            return response.json()
        except ValueError as exc:
            raise PermanentProviderError(f"noaa_coops: {path} did not answer JSON") from exc

    def predictions(
        self,
        station: str,
        start: datetime,
        end: datetime,
        interval: Interval,
        datum: str,
        *,
        kind: StationKind | None = None,
    ) -> list[TidePoint]:
        """Predicted heights (metres above ``datum``) in [start, end], UTC.

        ``kind`` (when known) lets a subordinate station's impossible request fail here:
        only ``interval="hilo"`` with ``datum="MLLW"``.
        """
        _check_station(station)
        if interval not in INTERVALS:
            raise ValueError(f"unknown CO-OPS interval {interval!r}")
        if datum not in DATUMS:
            raise ValueError(f"unknown CO-OPS datum {datum!r}")
        if kind == "subordinate" and (interval != "hilo" or datum != "MLLW"):
            raise PermanentProviderError(
                f"noaa_coops: {station} is a subordinate station: high/low (hilo) "
                "predictions relative to MLLW only"
            )
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > timedelta(days=MAX_DAYS.get(interval, MAX_DAYS_DEFAULT)):
            raise ValueError(f"window too long for interval {interval!r}")
        params = {
            "product": "predictions",
            "application": self.application,
            "station": station,
            "begin_date": start.strftime("%Y%m%d %H:%M"),
            "end_date": end.strftime("%Y%m%d %H:%M"),
            "datum": datum,
            "time_zone": "gmt",
            "units": "metric",
            "interval": interval,
            "format": "json",
        }
        points = parse_predictions(self._get_json(DATAGETTER, params), datum)
        if interval == "hilo" and any(p.event is None for p in points):
            raise PermanentProviderError("noaa_coops: hilo answer without event types")
        return [p for p in points if start <= p.t <= end]

    def stations(self) -> dict[str, CoopsStation]:
        """Every tide-prediction station (cached for a day per process)."""
        now = time.monotonic()
        with _station_lock:
            cached = _station_cache.get(self.base_url)
            if cached is not None and now - cached[0] < STATION_LIST_TTL_S:
                return cached[1]
        body = self._get_json(STATIONS_PATH, {"type": "tidepredictions"})
        stations = parse_station_list(body)
        with _station_lock:
            _station_cache[self.base_url] = (now, stations)
        return stations

    def datums(self, station: str) -> dict[str, float]:
        """The station's datums in metres above MLLW ({} when it publishes none)."""
        _check_station(station)
        body = self._get_json(
            f"mdapi/prod/webapi/stations/{station}/datums.json", {"units": "metric"}
        )
        return parse_datums(body)


def clear_station_cache() -> None:
    with _station_lock:
        _station_cache.clear()
