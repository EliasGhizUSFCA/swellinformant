"""Windy Point Forecast API v2: licensed, key-based, official API (design D3).

Requests (https://api.windy.com/point-forecast/docs, read 2026-10-27): one ``POST
https://api.windy.com/api/point-forecast/v2`` per point and model with the JSON body
``{lat, lon, model, parameters, levels, key}``; Windy has no multi-location request. Per
spot that is ONE ``gfs`` request (``wind``, ``windGust``, ``pressure``) at the break and ONE
``gfsWave`` request (``waves``, ``windWaves``, ``swell1``, ``swell2``) at the offshore
forecast point: 100 requests for a full refresh of 50 spots, 50 when :meth:`fetch` is asked
for one component. Windy rounds coordinates to 2 decimals ("the maximum possible error
equals ~1km"); they are sent rounded, so a point and its sentinel compare exactly.

Response: ``{"ts": [ms since 1970], "units": {...}, "<parameter>-surface": [...]}``. A null
means "the forecast model has no value for the given time"; a missing key that the model
"has no forecast for the given combination whatsoever": both are stored as None, never 0
and never filled in. Steps are a fixed 3 h (Windy staff, 2026): stored as they come, with
``extra.native_step_seconds``, never interpolated to hourly (the Open-Meteo GFS family
stores its 3-hourly hours after 120 h the same way).

Normalization: the same :class:`~app.services.forecasts.base.ForecastRecord` structure and
units as Open-Meteo (every FORECAST_FIELDS key present, UTC valid times, km/h, m, s, hPa,
directions FROM in [0, 360)):

* wind: ``wind_u`` / ``wind_v`` (u > 0 blows from the west towards the east, v > 0 from the
  south towards the north) -> speed in km/h and the direction it blows FROM
  (:func:`app.services.geo.wind_from_uv`); a calm wind has no direction;
* ``gust`` -> km/h; ``extra.gust_kind = "unknown"`` (Windy does not define its gust);
* ``pressure`` Pa -> hPa in ``pressure_msl_hpa``. Windy documents it only as "Air pressure"
  at the surface level, without saying whether it is reduced to sea level, so the records
  carry ``extra.pressure_kind = "surface_unspecified"``;
* waves, wind waves (``wwaves_*``), swell 1 and 2: height m, period s, direction FROM ("The
  direction defines where the waves come from (0 = North, 90 = East ...)"). A partition
  without a height (None or 0) is None, never a 0 m swell; there is no third swell and no
  sea level (both None). Windy does not say which period definition it returns, so
  ``period_kind`` stays None;
* units are read from ``units``: a series in a unit this module does not know is left out
  (None, with a warning), never converted by guesswork.

Run identity. The response has no model-run, reference-time or update-time field and Windy
has no metadata endpoint or webhook. :meth:`WindyProvider.current_run` therefore requests one
sentinel point per model (the Pipeline spot: its break for ``gfs``, its forecast point for
``gfsWave``) at most every WINDY_CHECK_MINUTES inside the expected availability window of
each 6-hourly GFS cycle (4x less often outside it) and compares a fingerprint, the sha256 of
``ts`` and the first 24 values of every requested series, with the last one, kept in Redis
(no database read on an idle check). A changed fingerprint is a new run of that component.
Its cycle is ESTIMATED: the newest 00/06/12/18Z cycle whose expected availability has
passed, never older than the previous run (:func:`estimate_cycle`); ``RunInfo.estimated``
is set so it is labelled "estimated" wherever it is shown. Component keys
``gfsWave~<fp>:<YYYYMMDDHH>`` and ``gfs~<fp>:<YYYYMMDDHH>`` (run key ``<wave>|<atmosphere>``).
A fetch checks the sentinel again after its last spot (the sentinel spot is fetched last,
otherwise it costs one more request per component): data that changed during the fetch are
never stored under the old key.

Licence and keys. A key is not a licence: Windy's terms forbid storing the data, derived
databases and redistribution, so Windy is used only with WINDY_STORAGE_AUTHORIZED=true
(settings validator, registry; docs/WINDY_API_SETUP.md). A TESTING key's responses carry
the warning "The trial API version is for development purposes only. This data is randomly
shuffled and slightly modified." Every response is checked: testing data raise
:class:`WindyTestingKeyError` (reason ``windy_testing_key``) and are never stored or
published. An invalid key (HTTP 400 ``{"message": "Invalid API key"}``, or 401/403) raises
:class:`WindyCredentialsError`; HTTP 429, a "limit" message or the WINDY_DAILY_REQUEST_LIMIT
counter (:func:`app.services.forecasts.quota.consume_windy_request`) :class:`WindyQuotaError`.
Each of these, and a Windy that keeps failing transiently, is recorded in the provider
health (:mod:`app.services.forecasts.provider_manager`) before it is raised, so the next
refresh falls back to Open-Meteo (design D4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import httpx
import redis

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.core.redis import get_redis
from app.services.forecasts import provider_manager
from app.services.forecasts.base import (
    ATMOSPHERE,
    COMPONENTS,
    EXTRA_GUST_KIND,
    EXTRA_NATIVE_STEP_S,
    EXTRA_WAVE_MODEL,
    FORECAST_FIELDS,
    WAVE,
    FetchResult,
    FetchStats,
    ForecastProvider,
    ForecastRecord,
    ModelRunRef,
    RunInfo,
    SourceInfo,
    SpotPoint,
    bearing,
    http_client,
    nominal_gfs_cycle,
)
from app.services.forecasts.quota import (
    QuotaWindowExhaustedError,
    consume_windy_request,
    seconds_until_next_utc_day,
)
from app.services.geo import wind_from_uv
from app.services.http import (
    RETRYABLE_STATUS,
    HttpTally,
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)

logger = logging.getLogger(__name__)

URL = "https://api.windy.com/api/point-forecast/v2"
PROVIDER = "windy"  # provider name for the HTTP helper, the health records and SourceInfo
GFS = "gfs"
GFS_WAVE = "gfsWave"
# The model of each component, and what each one is asked for in ONE request per point.
MODELS: Mapping[str, str] = MappingProxyType({ATMOSPHERE: GFS, WAVE: GFS_WAVE})
PARAMETERS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {GFS: ("wind", "windGust", "pressure"), GFS_WAVE: ("waves", "windWaves", "swell1", "swell2")}
)
LEVELS = ("surface",)
METADATA_SOURCE = "windy_fingerprint"
NATIVE_STEP_S = 3 * 3600  # "API offer 3 hours intervals/steps" (Windy staff, 2026)

# Response keys (documented names; ``windWaves`` answers ``wwaves_*``).
WAVE_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "waves_height-surface": "sig_wave_height_m",
        "waves_period-surface": "wave_period_s",
        "waves_direction-surface": "wave_direction_deg",
        "swell1_height-surface": "primary_swell_height_m",
        "swell1_period-surface": "primary_swell_period_s",
        "swell1_direction-surface": "primary_swell_direction_deg",
        "swell2_height-surface": "secondary_swell_height_m",
        "swell2_period-surface": "secondary_swell_period_s",
        "swell2_direction-surface": "secondary_swell_direction_deg",
        "wwaves_height-surface": "wind_wave_height_m",
        "wwaves_period-surface": "wind_wave_period_s",
        "wwaves_direction-surface": "wind_wave_direction_deg",
    }
)
WIND_U, WIND_V, GUST, PRESSURE = (
    "wind_u-surface",
    "wind_v-surface",
    "gust-surface",
    "pressure-surface",
)
ATMOS_KEYS = (WIND_U, WIND_V, GUST, PRESSURE)
SERIES_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {GFS: ATMOS_KEYS, GFS_WAVE: tuple(WAVE_KEYS)}
)
PARTITIONS = ("primary_swell", "secondary_swell", "tertiary_swell", "wind_wave")

# Units: factor to the stored unit. A series in an unknown unit is dropped. The defaults
# (for a response whose ``units`` omits a key) are the units of Windy's own examples.
SPEED_UNITS = {"m*s-1": 3.6, "m/s": 3.6, "m s-1": 3.6, "ms-1": 3.6, "km*h-1": 1.0, "km/h": 1.0}
PRESSURE_UNITS = {"pa": 0.01, "hpa": 1.0}
LENGTH_UNITS = {"m": 1.0}
PERIOD_UNITS = {"s": 1.0}
DEFAULT_UNITS = {"speed": "m*s-1", "pressure": "Pa", "length": "m", "period": "s"}
EXTRA_PRESSURE_KIND = "pressure_kind"
PRESSURE_KIND = "surface_unspecified"

# Testing (trial) keys: the documented warning of their responses (Windy staff example,
# community.windy.com, 2024-08-30) and the words that identify it.
TESTING_WARNING = (
    "The trial API version is for development purposes only. This data is randomly shuffled "
    "and slightly modified."
)
_TESTING_MARKERS = ("trial", "randomly shuffled", "slightly modified", "development purposes")
TESTING_KEY_REASON = "windy_testing_key"
CREDENTIALS_REASON = "windy_credentials"
QUOTA_REASON = "windy_quota"

# The sentinel point of each model: the Pipeline spot (data/surf_spots/spots.json, the first
# spot; a unit test keeps these equal to its coordinates rounded as Windy rounds them). Its
# break is the gfs point, its offshore forecast point the gfsWave point.
SENTINEL_SLUG = "pipeline"
SENTINEL_POINTS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {GFS: (21.67, -158.05), GFS_WAVE: (21.8, -158.18)}
)
FINGERPRINT_VALUES = 24
# Expected availability of a GFS cycle at Windy (ESTIMATES: Windy publishes no timing; NOAA's
# GFS and GFS-Wave output is complete about 3.5-5 h after the cycle and Windy's ingest delay
# is unmeasured, docs/WINDY_API_SETUP.md): a cycle is expected from EXPECTED_LAG_HOURS after
# its time, the sentinel is checked every WINDY_CHECK_MINUTES for WINDOW_HOURS from then and
# OUTSIDE_WINDOW_FACTOR times less often otherwise; no run can appear before
# EARLIEST_LAG_HOURS.
EXPECTED_LAG_HOURS = 4.0
WINDOW_HOURS = 4.0
OUTSIDE_WINDOW_FACTOR = 4
EARLIEST_LAG_HOURS = 3.0
CYCLE = timedelta(hours=6)
# Redis: one JSON document per model with the fingerprints seen (newest MAX_HISTORY).
STATE_KEY = "windy:run-state:v1:{model}"
STATE_TTL_SECONDS = 7 * 86400
MAX_HISTORY = 8
_KEY_PART = re.compile(
    r"^(?P<model>[A-Za-z]+)~(?P<fp>[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}):(?P<cycle>\d{10})$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- errors
class WindyError(PermanentProviderError):
    """A Windy failure that no retry in this refresh fixes; ``reason`` names it."""

    reason = "windy_error"


class WindyCredentialsError(WindyError):
    """The key is invalid, expired or not a Point Forecast key (HTTP 400 "Invalid API key",
    401 or 403)."""

    reason = CREDENTIALS_REASON


class WindyTestingKeyError(WindyError):
    """The response comes from a TESTING (trial) key: "randomly shuffled and slightly
    modified" data, never stored or published (design D3)."""

    reason = TESTING_KEY_REASON


class WindyQuotaError(QuotaWindowExhaustedError):
    """Windy's daily request limit (HTTP 429 or a "limit" message) or our own
    WINDY_DAILY_REQUEST_LIMIT counter is used up until the next 00:00 UTC."""

    reason = QUOTA_REASON


# Errors that end a whole fetch at once: every further request would fail the same way.
STOP_ERRORS: tuple[type[ProviderError], ...] = (WindyError, QuotaWindowExhaustedError)


# --------------------------------------------------------------------------- helpers
def is_testing_warning(text: object) -> bool:
    """True when a response's ``warning`` is the testing-key (trial) notice."""
    low = str(text or "").lower()
    return any(marker in low for marker in _TESTING_MARKERS)


def ms_to_utc(ms: float) -> datetime:
    """A Windy ``ts`` value (milliseconds since 1970-01-01 UTC) as an aware UTC datetime."""
    return _EPOCH + timedelta(milliseconds=int(round(float(ms))))


def rounded_point(lat: float, lon: float) -> tuple[float, float]:
    """A coordinate as Windy uses it: 2 decimals, longitude in [-180, 180)."""
    return round(lat, 2), round(((lon + 180.0) % 360.0) - 180.0, 2)


def request_body(model: str, lat: float, lon: float) -> dict[str, Any]:
    """The documented request body for one point and model (without the key)."""
    rlat, rlon = rounded_point(lat, lon)
    return {
        "lat": rlat,
        "lon": rlon,
        "model": model,
        "parameters": list(PARAMETERS[model]),
        "levels": list(LEVELS),
    }


def point_of(spot: SpotPoint, component: str) -> tuple[float, float]:
    """Where a component is requested: waves at the offshore forecast point, wind at the
    break (as for every other provider)."""
    if component == WAVE:
        return spot.forecast_latitude, spot.forecast_longitude
    return spot.latitude, spot.longitude


def is_sentinel(model: str, lat: float, lon: float) -> bool:
    return rounded_point(lat, lon) == SENTINEL_POINTS[model]


def fingerprint(model: str, data: Mapping[str, Any]) -> str:
    """sha256 of ``ts`` and the first FINGERPRINT_VALUES values of every requested series
    (a missing series hashes as null): changes when Windy serves another run."""
    digest = hashlib.sha256()
    times = [int(round(float(t))) for t in data.get("ts") or []]
    digest.update(json.dumps(times).encode())
    for key in SERIES_KEYS[model]:
        series = data.get(key)
        head = None
        if isinstance(series, list):
            head = [None if v is None else round(float(v), 4) for v in series[:FINGERPRINT_VALUES]]
        digest.update(json.dumps([key, head]).encode())
    return digest.hexdigest()


def short_fingerprint(full: str) -> str:
    """The first 48 bits of a fingerprint as ``xxxx-xxxx-xxxx``: hex groups of four can never
    form the 10-digit ``YYYYMMDDHH`` stamp the sync reads cycles from."""
    return f"{full[0:4]}-{full[4:8]}-{full[8:12]}"


def key_part(model: str, full_fingerprint: str, cycle: datetime) -> str:
    """A component run key, e.g. ``gfsWave~3fa2-c1d0-9b7e:2026102712``."""
    return f"{model}~{short_fingerprint(full_fingerprint)}:{cycle.astimezone(UTC):%Y%m%d%H}"


def parse_key_part(key: str | None) -> tuple[str, str, datetime] | None:
    """(model, short fingerprint, estimated cycle) of a component run key, or None."""
    match = _KEY_PART.match(key or "")
    if match is None:
        return None
    cycle = datetime.strptime(match["cycle"], "%Y%m%d%H").replace(tzinfo=UTC)
    return match["model"], match["fp"], cycle


def expected_cycle(now: datetime) -> datetime:
    """The newest 00/06/12/18Z cycle whose expected availability at Windy has passed."""
    return nominal_gfs_cycle(now, EXPECTED_LAG_HOURS)


def estimate_cycle(now: datetime, previous: datetime | None) -> datetime:
    """The cycle of a NEW fingerprint seen at ``now`` (after ``previous``, the last one).

    The newest cycle whose expected availability has passed; when that is not newer than
    ``previous`` (the data changed before the next cycle was expected), the next cycle if it
    can exist already (EARLIEST_LAG_HOURS), else ``previous`` (the same run re-processed).
    Never older than ``previous``.
    """
    estimate = expected_cycle(now)
    if previous is None or estimate > previous:
        return estimate
    early = nominal_gfs_cycle(now, EARLIEST_LAG_HOURS)
    return max(previous, min(previous + CYCLE, early))


def in_availability_window(now: datetime) -> bool:
    """Whether ``now`` is inside some cycle's expected availability window."""
    t = now.astimezone(UTC)
    base = t.replace(hour=(t.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for cycle in (base, base - CYCLE):
        start = cycle + timedelta(hours=EXPECTED_LAG_HOURS)
        if start <= t < start + timedelta(hours=WINDOW_HOURS):
            return True
    return False


def check_interval(now: datetime, settings: Settings) -> timedelta:
    """How long a sentinel fingerprint is trusted before it is checked again."""
    minutes = settings.windy_check_minutes
    if not in_availability_window(now):
        minutes *= OUTSIDE_WINDOW_FACTOR
    return timedelta(minutes=minutes)


def _message(resp: httpx.Response) -> str:
    """The error text of a response (its JSON ``message``, else the body start), redacted."""
    text = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        message = body["message"]
        text = "; ".join(map(str, message)) if isinstance(message, list) else str(message)
    elif resp.content:
        text = resp.text[:200]
    return redact(text)[:200]


# --------------------------------------------------------------------------- run state
@dataclass(frozen=True)
class SeenRun:
    """A fingerprint the sentinel returned, with its estimated cycle and first sighting."""

    fingerprint: str
    cycle: datetime
    first_seen_at: datetime


@dataclass
class ModelRunState:
    """What the sentinel of one model has returned (stored in Redis as JSON)."""

    model: str
    latest: str | None = None
    runs: dict[str, SeenRun] = field(default_factory=dict)
    checked_at: datetime | None = None

    def latest_run(self) -> SeenRun | None:
        return self.runs.get(self.latest) if self.latest else None

    def observe(self, full: str, now: datetime) -> SeenRun:
        """Record a sentinel answer: a known fingerprint keeps its first label (an older one
        seen again stays older, so it is never new); a new one becomes the latest run."""
        self.checked_at = now
        if full in self.runs:
            return self.runs[full]
        previous = self.latest_run()
        seen = SeenRun(full, estimate_cycle(now, previous.cycle if previous else None), now)
        self.runs[full] = seen
        self.latest = full
        if len(self.runs) > MAX_HISTORY:
            for old in sorted(self.runs.values(), key=lambda r: r.first_seen_at)[:-MAX_HISTORY]:
                del self.runs[old.fingerprint]
        return seen

    def to_json(self) -> str:
        return json.dumps(
            {
                "model": self.model,
                "latest": self.latest,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "runs": {
                    fp: {"cycle": r.cycle.isoformat(), "first_seen_at": r.first_seen_at.isoformat()}
                    for fp, r in self.runs.items()
                },
            }
        )

    @classmethod
    def from_json(cls, model: str, raw: str) -> ModelRunState:
        data = json.loads(raw)
        runs = {
            str(fp): SeenRun(
                str(fp),
                datetime.fromisoformat(r["cycle"]),
                datetime.fromisoformat(r["first_seen_at"]),
            )
            for fp, r in (data.get("runs") or {}).items()
        }
        checked = data.get("checked_at")
        latest = data.get("latest")
        return cls(
            model=model,
            latest=str(latest) if latest in runs else None,
            runs=runs,
            checked_at=datetime.fromisoformat(checked) if checked else None,
        )


# Process memo: the state of the last check of this process, used while Redis is down.
_MEMO: dict[str, str] = {}


class RunStateStore:
    """The sentinel states in Redis (decoded client), with a process memo when Redis fails."""

    def key(self, model: str) -> str:
        return STATE_KEY.format(model=model)

    def load(self, model: str) -> ModelRunState | None:
        raw: str | None
        try:
            value = get_redis().get(self.key(model))
            raw = None if value is None else str(value)
        except redis.RedisError as exc:
            logger.warning("Windy run state unavailable (Redis: %s)", type(exc).__name__)
            raw = _MEMO.get(model)
        if raw is None:
            return None
        try:
            return ModelRunState.from_json(model, raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Windy run state of %s is unreadable; checking again", model)
            return None

    def save(self, state: ModelRunState) -> None:
        raw = state.to_json()
        _MEMO[state.model] = raw
        try:
            get_redis().set(self.key(state.model), raw, ex=STATE_TTL_SECONDS)
        except redis.RedisError as exc:
            logger.warning("Windy run state not saved (Redis: %s)", type(exc).__name__)


# --------------------------------------------------------------------------- records
def _unit_factor(units: Mapping[str, Any], key: str, kind: str) -> float | None:
    """Factor from the unit ``units`` gives for ``key`` (default: Windy's example unit) to
    the stored unit, or None for a unit this module does not know."""
    unit = units.get(key)
    text = str(unit if unit is not None else DEFAULT_UNITS[kind]).strip()
    if kind == "pressure":
        return PRESSURE_UNITS.get(text.lower())
    table = {"speed": SPEED_UNITS, "length": LENGTH_UNITS, "period": PERIOD_UNITS}[kind]
    return table.get(text)


def _direction_unit_ok(units: Mapping[str, Any], key: str) -> bool:
    """Directions are degrees; a unit that names something else drops the series."""
    text = str(units.get(key) or "").strip().lower()
    return not text or "deg" in text or "°" in text


def _kind_of(field_name: str) -> str:
    if field_name.endswith("_height_m"):
        return "length"
    if field_name.endswith("_period_s"):
        return "period"
    return "direction"


def _series(data: Mapping[str, Any], key: str, index: int) -> float | None:
    series = data.get(key)
    if not isinstance(series, list) or index >= len(series):
        return None
    value = series[index]
    return None if value is None else float(value)


def _times(data: Mapping[str, Any]) -> list[int]:
    return [int(round(float(t))) for t in data.get("ts") or []]


def wave_rows(data: Mapping[str, Any], warnings: list[str]) -> dict[int, dict[str, float | None]]:
    """{ms: wave values} of a gfsWave response (stored units; unknown units dropped)."""
    units = data.get("units") or {}
    factors: dict[str, float | None] = {}
    for key, name in WAVE_KEYS.items():
        kind = _kind_of(name)
        if kind == "direction":
            factors[key] = 1.0 if _direction_unit_ok(units, key) else None
        else:
            factors[key] = _unit_factor(units, key, kind)
        if factors[key] is None and key in data:
            warnings.append(f"Windy {key} in unknown unit {units.get(key)!r}: not stored")
    rows: dict[int, dict[str, float | None]] = {}
    for i, ms in enumerate(_times(data)):
        row: dict[str, float | None] = {}
        for key, name in WAVE_KEYS.items():
            factor = factors[key]
            value = _series(data, key, i)
            row[name] = None if value is None or factor is None else value * factor
        rows[ms] = row
    return rows


def atmosphere_rows(
    data: Mapping[str, Any], warnings: list[str]
) -> dict[int, dict[str, float | None]]:
    """{ms: wind speed (km/h) and direction FROM, gust (km/h), pressure (hPa)} of a gfs
    response."""
    units = data.get("units") or {}
    speed_u = _unit_factor(units, WIND_U, "speed")
    speed_v = _unit_factor(units, WIND_V, "speed")
    gust_f = _unit_factor(units, GUST, "speed")
    pres_f = _unit_factor(units, PRESSURE, "pressure")
    for key, factor in ((WIND_U, speed_u), (WIND_V, speed_v), (GUST, gust_f), (PRESSURE, pres_f)):
        if factor is None and key in data:
            warnings.append(f"Windy {key} in unknown unit {units.get(key)!r}: not stored")
    wind_ok = speed_u is not None and speed_u == speed_v
    rows: dict[int, dict[str, float | None]] = {}
    for i, ms in enumerate(_times(data)):
        u, v = _series(data, WIND_U, i), _series(data, WIND_V, i)
        gust, pres = _series(data, GUST, i), _series(data, PRESSURE, i)
        row: dict[str, float | None] = dict.fromkeys(
            ("wind_speed_kmh", "wind_direction_deg", "wind_gust_kmh", "pressure_msl_hpa")
        )
        if wind_ok and u is not None and v is not None and speed_u is not None:
            speed, direction = wind_from_uv(u, v)  # FROM; None when calm
            row["wind_speed_kmh"] = speed * speed_u
            row["wind_direction_deg"] = direction
        if gust is not None and gust_f is not None:
            row["wind_gust_kmh"] = gust * gust_f
        if pres is not None and pres_f is not None:
            row["pressure_msl_hpa"] = pres * pres_f
        rows[ms] = row
    return rows


def clean_values(values: dict[str, float | None]) -> dict[str, float | None]:
    """Absent partitions to None, non-positive heights and periods to None, directions to
    [0, 360) at 0.1 deg, a calm wind without a direction (the Open-Meteo rules)."""
    for prefix in PARTITIONS:
        height = values.get(f"{prefix}_height_m")
        if height is None or height <= 0:
            for suffix in ("height_m", "period_s", "direction_deg"):
                values[f"{prefix}_{suffix}"] = None
            continue
        period = values.get(f"{prefix}_period_s")
        if period is not None and period <= 0:
            values[f"{prefix}_period_s"] = None
    hs = values.get("sig_wave_height_m")
    if hs is not None and hs <= 0:
        values["sig_wave_height_m"] = None
    period = values.get("wave_period_s")
    if period is not None and period <= 0:
        values["wave_period_s"] = None
    for name in [k for k in values if k.endswith("direction_deg")]:
        values[name] = bearing(values[name])
    speed = values.get("wind_speed_kmh")
    if speed is not None and speed <= 0:
        values["wind_direction_deg"] = None  # calm: no direction (geo.wind_from_uv)
    return values


def _steps(times: list[int]) -> dict[int, int]:
    """Each time's spacing to the next one in seconds (the last: to the previous one)."""
    out: dict[int, int] = {}
    for i, ms in enumerate(times):
        if i + 1 < len(times):
            gap = times[i + 1] - ms
        elif i > 0:
            gap = ms - times[i - 1]
        else:
            gap = NATIVE_STEP_S * 1000
        out[ms] = max(1, round(gap / 1000))
    return out


def build_records(
    wave: Mapping[str, Any] | None,
    atmosphere: Mapping[str, Any] | None,
    *,
    components: frozenset[str],
    horizon_end: datetime | None,
    warnings: list[str],
) -> list[ForecastRecord]:
    """Normalized records of one spot from its gfsWave and gfs responses.

    With the wave component the wave times are the record times (as Open-Meteo) and the
    wind is joined on equal times; with the atmosphere alone, the gfs times. Every
    FORECAST_FIELDS key is present; values that are missing are None. Times after
    ``horizon_end`` (FORECAST_DAYS) are left out.
    """
    with_wave = WAVE in components
    waves = wave_rows(wave, warnings) if with_wave and wave is not None else {}
    winds = (
        atmosphere_rows(atmosphere, warnings)
        if ATMOSPHERE in components and atmosphere is not None
        else {}
    )
    base = waves if with_wave else winds
    times = sorted(base)
    steps = _steps(times)
    records: list[ForecastRecord] = []
    for ms in times:
        t = ms_to_utc(ms)
        if horizon_end is not None and t > horizon_end:
            continue
        own = base[ms]
        if all(v is None for v in own.values()):
            continue  # a masked cell (land, ice) or no value at this time
        values: dict[str, float | None] = dict.fromkeys(FORECAST_FIELDS)
        values.update(own)
        if with_wave:
            values.update(winds.get(ms, {}))
        values = clean_values(values)
        extra: dict[str, object] = {EXTRA_NATIVE_STEP_S: steps[ms]}
        if values.get("wind_gust_kmh") is not None:
            extra[EXTRA_GUST_KIND] = "unknown"  # Windy does not define its gust
        if values.get("pressure_msl_hpa") is not None:
            extra[EXTRA_PRESSURE_KIND] = PRESSURE_KIND
        records.append(ForecastRecord(valid_time=t, values=values, period_kind=None, extra=extra))
    if records and with_wave:
        records[0].extra[EXTRA_WAVE_MODEL] = GFS_WAVE
    return records


# --------------------------------------------------------------------------- provider
class WindyProvider(ForecastProvider):
    code = "windy_gfswave"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        backoff_seconds: float = 1.0,
        *,
        record_health: bool = True,
        state: RunStateStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.windy_api_key:
            raise PermanentProviderError("WINDY_API_KEY is not configured")
        self.client = client or http_client(self.settings.forecast_http_timeout_seconds)
        self.tally = HttpTally()
        self.tally.install(self.client)
        self.backoff_seconds = backoff_seconds
        # False for a health check: it records nothing (provider_manager.health_check).
        self.record_health = record_health
        self.state = state or RunStateStore()
        self.warnings: list[str] = []

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            code=self.code,
            name="Windy Point Forecast API (GFS + GFS-Wave)",
            provider=PROVIDER,
            wave_model=GFS_WAVE,
            atmosphere_model=GFS,
            is_demo=False,
            attribution=(
                "Contains data from the Windy database (Windy.com). Model data NOAA NCEP GFS "
                "and GFS-Wave (WAVEWATCH III), 3-hourly as Windy serves them."
            ),
            license_notes=(
                "Windy Point Forecast API (paid Professional key). Windy's terms forbid storing "
                "the data, derived databases and redistribution: used only with "
                "WINDY_STORAGE_AUTHORIZED=true under a written licence that allows storage, "
                "derived scoring and display (docs/WINDY_API_SETUP.md). Testing keys are "
                "refused. Attribution: the Windy logo and 'Contains data from the Windy "
                "database'."
            ),
            family="ncep_gfs",
        )

    # ---------------------------------------------------------------- health
    def _failure(self, kind: str, detail: str, retry_after_s: float | None = None) -> None:
        if self.record_health:
            provider_manager.record_provider_failure(
                PROVIDER, kind, detail, retry_after_s=retry_after_s
            )

    def _success(self) -> None:
        if self.record_health:
            provider_manager.record_provider_success(PROVIDER)

    # ---------------------------------------------------------------- requests
    def _charge(self) -> None:
        consume_windy_request(settings=self.settings)

    def _post(self, model: str, lat: float, lon: float) -> dict[str, Any]:
        """One point request; raises the STOP_ERRORS (recorded first) or ProviderError."""
        body = request_body(model, lat, lon)
        try:
            resp = request_with_retries(
                self.client,
                "POST",
                URL,
                provider=PROVIDER,
                backoff_seconds=self.backoff_seconds,
                retry_statuses=RETRYABLE_STATUS - {429},
                before_attempt=self._charge,
                json={**body, "key": self.settings.windy_api_key},
            )
        except QuotaWindowExhaustedError as exc:  # our WINDY_DAILY_REQUEST_LIMIT counter
            self._failure(provider_manager.QUOTA, str(exc), exc.retry_after_s)
            raise WindyQuotaError(str(exc), retry_after_s=exc.retry_after_s) from exc
        return self._read(resp, model)

    def _read(self, resp: httpx.Response, model: str) -> dict[str, Any]:
        status = resp.status_code
        if status != 200:
            message = _message(resp)
            low = message.lower()
            if status in (401, 403) or (status == 400 and "api key" in low and "limit" not in low):
                detail = f"Windy refused the key (HTTP {status}: {message or 'no message'})"
                self._failure(provider_manager.CREDENTIALS, detail)
                raise WindyCredentialsError(detail)
            if status == 429 or (400 <= status < 500 and "limit" in low):
                wait = seconds_until_next_utc_day(utcnow())
                detail = f"Windy request limit reached (HTTP {status}: {message or 'no message'})"
                self._failure(provider_manager.QUOTA, detail, wait)
                raise WindyQuotaError(detail, retry_after_s=wait)
            if status == 204:
                raise WindyError(
                    f"Windy {model} answered 204: the model does not feature the requested "
                    f"parameters {', '.join(PARAMETERS[model])}"
                )
            raise PermanentProviderError(f"Windy {model} HTTP {status}: {message}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TransientProviderError(f"Windy {model} returned unreadable JSON") from exc
        if not isinstance(data, dict):
            raise TransientProviderError(f"Windy {model} returned no forecast object")
        warning = data.get("warning")
        if warning and is_testing_warning(warning):
            notice = redact(str(warning))[:160]
            detail = (
                f"{TESTING_KEY_REASON}: Windy answered with TESTING-key data ('{notice}'): "
                "testing data are randomly shuffled and are never stored or published"
            )
            self._failure(provider_manager.TESTING_KEY, detail)
            raise WindyTestingKeyError(detail)
        if warning:
            self.warnings.append(f"Windy {model} warning: {redact(str(warning))[:200]}")
        if not isinstance(data.get("ts"), list):
            raise TransientProviderError(f"Windy {model} returned no time steps")
        return data

    def sentinel(self, model: str) -> dict[str, Any]:
        """One request for the model's sentinel point (the Pipeline spot)."""
        lat, lon = SENTINEL_POINTS[model]
        return self._post(model, lat, lon)

    # ---------------------------------------------------------------- run identity
    def current_run(self, now: datetime) -> RunInfo:
        """The newest Windy run of each component, from the sentinel fingerprints (Redis
        state; a sentinel request only when its check is due)."""
        now = now.astimezone(UTC)
        seen: dict[str, SeenRun] = {}
        probed = False
        for component in (WAVE, ATMOSPHERE):
            model = MODELS[component]
            state = self.state.load(model)
            latest = state.latest_run() if state is not None else None
            if (
                state is not None
                and latest is not None
                and state.checked_at is not None
                and now - state.checked_at < check_interval(now, self.settings)
            ):
                seen[component] = latest
                continue
            try:
                data = self.sentinel(model)
            except TransientProviderError as exc:
                self._failure(provider_manager.UNAVAILABLE, str(exc))
                raise
            state = state or ModelRunState(model)
            seen[component] = state.observe(fingerprint(model, data), now)
            self.state.save(state)
            probed = True
        if probed:
            self._success()
        return run_info(seen[WAVE], seen[ATMOSPHERE])

    # ---------------------------------------------------------------- fetch
    def fetch(
        self,
        spots: list[SpotPoint],
        run: RunInfo,
        components: frozenset[str] | None = None,
    ) -> FetchResult:
        """The wanted components of ``run`` for ``spots``: one gfsWave and/or one gfs
        request per spot, then the sentinel check (nothing is returned for a run that
        changed meanwhile)."""
        wanted = COMPONENTS if components is None else frozenset(components) & COMPONENTS
        result = FetchResult(components=wanted)
        if not wanted:
            return result
        expected: dict[str, tuple[str, datetime]] = {}
        for component in wanted:
            parsed = parse_key_part(run.component_keys.get(component))
            if parsed is None or parsed[0] != MODELS[component]:
                raise PermanentProviderError(
                    f"run {run.run_key!r} names no Windy {component} fingerprint: a Windy run "
                    "is identified by current_run()"
                )
            expected[component] = (parsed[1], parsed[2])
        before = self.tally.snapshot()
        self.warnings = []
        order = sorted(spots, key=lambda p: _is_sentinel_spot(p))  # the sentinel spot last
        responses: dict[str, dict[int, dict[str, Any]]] = {c: {} for c in wanted}
        failures: dict[int, dict[str, str]] = {}
        verified: set[str] = set()
        transient = 0
        for p in order:
            for component in sorted(wanted, key=lambda c: c != WAVE):  # waves first
                model = MODELS[component]
                lat, lon = point_of(p, component)
                try:
                    data = self._post(model, lat, lon)
                except STOP_ERRORS:
                    raise
                except ProviderError as exc:
                    transient += int(exc.transient)
                    failures.setdefault(p.spot_id, {})[component] = str(exc)
                    continue
                responses[component][p.spot_id] = data
                if is_sentinel(model, lat, lon):
                    self._verify(component, data, expected[component][0])
                    verified.add(component)
        answered = sum(len(r) for r in responses.values())
        if answered == 0 and transient:
            self._failure(
                provider_manager.UNAVAILABLE,
                f"every Windy request failed ({transient} transient failures)",
            )
        elif answered:
            for component in sorted(wanted - verified, key=lambda c: c != WAVE):
                self._verify(component, self.sentinel(MODELS[component]), expected[component][0])
            self._success()

        days = timedelta(days=self.settings.forecast_days)
        for p in spots:
            wave = responses[WAVE].get(p.spot_id) if WAVE in wanted else None
            atmos = responses[ATMOSPHERE].get(p.spot_id) if ATMOSPHERE in wanted else None
            reasons = failures.get(p.spot_id, {})
            lead = WAVE if WAVE in wanted else ATMOSPHERE
            if (wave if lead == WAVE else atmos) is None:
                result.errors[p.spot_id] = (
                    f"{'wave' if lead == WAVE else 'wind'} data ({MODELS[lead]}): "
                    + reasons.get(lead, "missing")
                )
                continue
            records = build_records(
                wave,
                atmos,
                components=wanted,
                horizon_end=expected[lead][1] + days,
                warnings=self.warnings,
            )
            if not records:
                result.errors[p.spot_id] = (
                    "no wave data at the forecast point (land or outside the gfsWave domain)"
                    if lead == WAVE
                    else "no wind data at the break"
                )
                continue
            result.records[p.spot_id] = records
            if lead == WAVE and ATMOSPHERE in wanted and atmos is None:
                reason = reasons.get(ATMOSPHERE, "missing")
                result.errors[p.spot_id] = f"incomplete: wind and pressure unavailable ({reason})"
        result.stats = FetchStats.between(before, self.tally.snapshot())
        result.request_count = result.stats.requests
        result.warnings = [
            f"Windy: {result.request_count} requests for {len(spots)} spots "
            f"({', '.join(MODELS[c] for c in sorted(wanted, key=lambda c: c != WAVE))}), "
            f"{result.stats.bytes_downloaded / 1e3:.1f} kB",
            *dict.fromkeys(self.warnings),
        ]
        return result

    def _verify(self, component: str, data: Mapping[str, Any], expected: str) -> None:
        """Raise unless the sentinel still returns the run being fetched (and record a new
        fingerprint, so the next check sees the new run without another request)."""
        model = MODELS[component]
        full = fingerprint(model, data)
        if short_fingerprint(full) == expected:
            return
        state = self.state.load(model) or ModelRunState(model)
        state.observe(full, utcnow())
        self.state.save(state)
        raise TransientProviderError(
            f"Windy published new {model} data during the fetch (the sentinel fingerprint "
            "changed): the responses may mix runs, so nothing is stored",
            retry_after_s=60.0,
        )


def _is_sentinel_spot(p: SpotPoint) -> bool:
    return any(is_sentinel(MODELS[c], *point_of(p, c)) for c in COMPONENTS)


def run_info(wave: SeenRun, atmosphere: SeenRun) -> RunInfo:
    """The RunInfo of a Windy wave run and atmosphere run (cycles estimated)."""
    wave_key = key_part(GFS_WAVE, wave.fingerprint, wave.cycle)
    atmos_key = key_part(GFS, atmosphere.fingerprint, atmosphere.cycle)
    return RunInfo(
        run_key=f"{wave_key}|{atmos_key}",
        issued_at=min(wave.cycle, atmosphere.cycle),
        wave_model_run_at=wave.cycle,
        atmosphere_model_run_at=atmosphere.cycle,
        metadata_source=METADATA_SOURCE,
        model_runs=(
            ModelRunRef(GFS_WAVE, wave.cycle, wave.first_seen_at, WAVE),
            ModelRunRef(GFS, atmosphere.cycle, atmosphere.first_seen_at, ATMOSPHERE),
        ),
        component_keys=MappingProxyType({WAVE: wave_key, ATMOSPHERE: atmos_key}),
        estimated=True,
    )
