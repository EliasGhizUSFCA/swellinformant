"""NOAA NDBC buoys (www.ndbc.noaa.gov; public domain). NOT verified live.

www.ndbc.noaa.gov is unreachable from the sandbox this was built in (the proxy rejects the
CONNECT), so these parsers were written against NDBC's documented text formats and tested
with synthetic fixtures only (research notes, "Forecast validation methodology", NDBC).

Files:

* realtime (last ~45 days): ``{base}/data/realtime2/{ID}.txt`` (standard meteorological)
  and ``{base}/data/realtime2/{ID}.spec`` (spectral summary: NDBC's own swell / wind-sea
  split). Missing values are ``MM``.
* history (complete years): ``{base}/view_text_file.php?filename={id}h{YYYY}.txt.gz
  &dir=data/historical/stdmet/`` (served decompressed). Missing values are runs of 9s with
  a PER-COLUMN meaning: WVHT/DPD/APD 99.00, MWD/WDIR 999, WSPD/GST 99.0, PRES 9999.0,
  ATMP/WTMP/DEWP 999.0, VIS 99.0, TIDE 99.00. A blanket mask would delete valid values
  (a WDIR of 99 degrees is a real wind direction), so sentinels are matched per column.
  Older yearly files use a 2-digit year and no minute column; both are handled.
  The current year's months older than ~45 days live in monthly files, which are not
  implemented (reported as a coverage warning).

Meanings (NDBC measurement descriptions): WVHT significant wave height (m), stored as
``spectral_hm0`` because NDBC derives it from the non-directional spectrum (4·sqrt(m0));
DPD dominant (peak) period; APD average period (``tm_kind = apd``); MWD the direction FROM
which the waves at DPD come (so it is a *peak* direction); WDIR/WSPD wind FROM / m/s at
the anemometer height (not adjusted to 10 m). ``.spec`` SwD/WWD are 16-point compass
names (22.5 degree resolution), converted to the sector centre.

QC: NDBC replaces values that fail its automated checks by MM before publishing realtime
data, but there are no per-value flags, so realtime rows are ``not_qc``; the yearly
historical files have been through NDBC's final QC and are stored as ``probably_good``
(no per-value flags either). Positions: ``activestations.xml`` is read when reachable to
replace the catalog position (which comes from the GFS-Wave bulletin header of the same
WMO id until then).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)
from app.services.observations.records import (
    ObservationRecord,
    ParseStats,
    ProviderFetch,
    StationRef,
    validate_record,
)

logger = logging.getLogger(__name__)

PROVIDER = "ndbc"
REALTIME_DATASET = "ndbc_realtime2"
HISTORICAL_DATASET = "ndbc_historical_stdmet"
REALTIME_DAYS = 44  # realtime2 files hold ~45 days

STDMET_SENTINELS: dict[str, float] = {
    "WDIR": 999.0,
    "WSPD": 99.0,
    "GST": 99.0,
    "WVHT": 99.0,
    "DPD": 99.0,
    "APD": 99.0,
    "MWD": 999.0,
    "PRES": 9999.0,
    "ATMP": 999.0,
    "WTMP": 999.0,
    "DEWP": 999.0,
    "VIS": 99.0,
    "PTDY": 99.0,
    "TIDE": 99.0,
}
COMPASS = {
    name: i * 22.5
    for i, name in enumerate(
        [
            "N",
            "NNE",
            "NE",
            "ENE",
            "E",
            "ESE",
            "SE",
            "SSE",
            "S",
            "SSW",
            "SW",
            "WSW",
            "W",
            "WNW",
            "NW",
            "NNW",
        ]
    )
}
_STATION_TAG = re.compile(r"<station\s+([^>]*?)/?>", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _header(lines: list[str]) -> tuple[list[str], int]:
    """Column names (leading '#' removed) and the index of the first data line."""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.lstrip("#").split()
        if tokens and tokens[0] in ("YY", "YYYY"):
            j = i + 1
            # The realtime / post-2007 files carry a second '#' line with units.
            while j < len(lines) and lines[j].lstrip().startswith("#"):
                j += 1
            return tokens, j
    raise PermanentProviderError("ndbc: no '#YY MM DD hh' header line")


def _time(values: dict[str, str]) -> datetime:
    year = int(values.get("YY") or values["YYYY"])
    if year < 100:
        year += 1900  # pre-1999 yearly files
    return datetime(
        year,
        int(values["MM"]),
        int(values["DD"]),
        int(values["hh"]),
        int(values.get("mm", "0")),
        tzinfo=UTC,
    )


def _value(column: str, raw: str | None) -> float | None:
    if raw is None or raw == "MM":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    sentinel = STDMET_SENTINELS.get(column)
    return None if sentinel is not None and v == sentinel else v


# Column names used by yearly files before 2007.
COLUMN_ALIASES = {"WD": "WDIR", "BAR": "PRES"}


def _rows(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    names, first = _header(lines)
    names = [COLUMN_ALIASES.get(n, n) for n in names]
    out = []
    for line in lines[first:]:
        parts = line.split()
        if len(parts) != len(names):
            continue
        out.append(dict(zip(names, parts, strict=True)))
    return out


def parse_stdmet(
    text: str, *, dataset: str, start: datetime, end: datetime, stats: ParseStats
) -> list[ObservationRecord]:
    """Standard meteorological file (realtime2 ``.txt`` or historical yearly ``h`` file)."""
    qc = "probably_good" if dataset == HISTORICAL_DATASET else "not_qc"
    records = []
    for values in _rows(text):
        try:
            t = _time(values)
        except (KeyError, ValueError):
            stats.invalid_values["time"] += 1
            continue
        if not start <= t <= end:
            continue
        stats.rows_read += 1
        rec = ObservationRecord(t, qc, dataset, source=dataset)
        rec.hs_m = _value("WVHT", values.get("WVHT"))
        rec.hs_kind = "spectral_hm0" if rec.hs_m is not None else "unknown"
        rec.tp_s = _value("DPD", values.get("DPD"))
        rec.tm_s = _value("APD", values.get("APD"))
        rec.tm_kind = "apd" if rec.tm_s is not None else None
        rec.peak_direction_deg = _value("MWD", values.get("MWD"))
        wspd = _value("WSPD", values.get("WSPD"))
        rec.wind_speed_kmh = None if wspd is None else wspd * 3.6
        rec.wind_direction_deg = _value("WDIR", values.get("WDIR"))
        if not rec.has_wave_data():
            # Wind-only rows (10-minute met reports between wave reports) are not stored.
            stats.no_wave_data += 1
            continue
        valid = validate_record(rec, stats)
        if valid is not None:
            records.append(valid)
    stats.rows_kept += len(records)
    return records


def parse_spec(text: str, *, start: datetime, end: datetime) -> dict[datetime, dict[str, Any]]:
    """Realtime ``.spec`` summary: swell / wind-sea partition per time (FROM directions)."""
    out: dict[datetime, dict[str, Any]] = {}
    for values in _rows(text):
        try:
            t = _time(values)
        except (KeyError, ValueError):
            continue
        if not start <= t <= end:
            continue
        out[t] = {
            "hs_m": _value("WVHT", values.get("WVHT")),
            "swell_hs_m": _value("SwH", values.get("SwH")),
            "swell_tp_s": _value("SwP", values.get("SwP")),
            "swell_direction_deg": COMPASS.get(values.get("SwD", "")),
            "windsea_hs_m": _value("WWH", values.get("WWH")),
            "tm_s": _value("APD", values.get("APD")),
            "peak_direction_deg": _value("MWD", values.get("MWD")),
        }
    return out


def merge_spec(
    records: list[ObservationRecord],
    spec: dict[datetime, dict[str, Any]],
    stats: ParseStats,
) -> list[ObservationRecord]:
    """Add the swell / wind-sea split to stdmet records; spec-only times become records."""
    by_time = {r.observed_at: r for r in records}
    for t, values in spec.items():
        rec = by_time.get(t)
        if rec is None:
            stats.rows_read += 1
            rec = ObservationRecord(t, "not_qc", REALTIME_DATASET, source="spec")
            rec.hs_m = values["hs_m"]
            rec.hs_kind = "spectral_hm0" if rec.hs_m is not None else "unknown"
            rec.tm_s = values["tm_s"]
            rec.tm_kind = "apd" if rec.tm_s is not None else None
            rec.peak_direction_deg = values["peak_direction_deg"]
            if not rec.has_wave_data():
                stats.no_wave_data += 1
                continue
            stats.rows_kept += 1
        for name in ("swell_hs_m", "swell_tp_s", "swell_direction_deg", "windsea_hs_m"):
            setattr(rec, name, values[name])
        valid = validate_record(rec, stats)
        if valid is not None:
            by_time[t] = valid
        else:
            by_time.pop(t, None)
    return sorted(by_time.values(), key=lambda r: r.observed_at)


def parse_active_stations(xml_text: str) -> dict[str, dict[str, Any]]:
    """``activestations.xml`` -> {station id: {latitude, longitude, name, owner, type}}."""
    out: dict[str, dict[str, Any]] = {}
    for m in _STATION_TAG.finditer(xml_text):
        attrs = dict(_ATTR.findall(m.group(1)))
        try:
            sid = attrs["id"].upper()
            lat, lon = float(attrs["lat"]), float(attrs["lon"])
        except (KeyError, ValueError):
            continue
        out[sid] = {
            "latitude": lat,
            "longitude": lon,
            "name": attrs.get("name"),
            "owner": attrs.get("owner"),
            "type": attrs.get("type"),
        }
    return out


class NdbcProvider:
    name = PROVIDER

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        backoff_seconds: float = 1.0,
        now: datetime | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.ndbc_base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (observations)"},
        )
        self.backoff_seconds = backoff_seconds
        self.now = now
        self.requests = 0

    def _get(self, path: str, params: dict[str, str] | None = None) -> str | None:
        """Text of a file; None when NDBC answers 404 (station has no such file)."""
        self.requests += 1
        response = request_with_retries(
            self.client,
            "GET",
            f"{self.base_url}/{path}",
            provider=PROVIDER,
            backoff_seconds=self.backoff_seconds,
            params=params or {},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PermanentProviderError(f"ndbc: HTTP {response.status_code} for {path}")
        return response.text

    def _station_records(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        stats: ParseStats,
        warnings: list[str],
    ) -> tuple[list[ObservationRecord], int]:
        now = self.now or datetime.now(UTC)
        records: list[ObservationRecord] = []
        files = 0
        realtime_from = now - timedelta(days=REALTIME_DAYS)
        if end >= realtime_from:
            text = self._get(f"data/realtime2/{station_id}.txt")
            if text is not None:
                files += 1
                records = parse_stdmet(
                    text, dataset=REALTIME_DATASET, start=start, end=end, stats=stats
                )
            spec_text = self._get(f"data/realtime2/{station_id}.spec")
            if spec_text is not None:
                files += 1
                records = merge_spec(records, parse_spec(spec_text, start=start, end=end), stats)
        history_end = min(end, realtime_from)
        last_year = min(history_end.year, now.year - 1) if start < history_end else start.year - 1
        for year in range(start.year, last_year + 1):
            text = self._get(
                "view_text_file.php",
                {
                    "filename": f"{station_id.lower()}h{year}.txt.gz",
                    "dir": "data/historical/stdmet/",
                },
            )
            if text is None:
                continue
            files += 1
            records += parse_stdmet(
                text, dataset=HISTORICAL_DATASET, start=start, end=end, stats=stats
            )
        gap_start = max(start, datetime(now.year, 1, 1, tzinfo=UTC))
        if gap_start < min(end, realtime_from):
            warnings.append(
                f"ndbc {station_id}: {gap_start:%Y-%m-%d} to {min(end, realtime_from):%Y-%m-%d}"
                " is only in NDBC monthly files, which are not implemented"
            )
        return records, files

    def fetch(self, stations: list[StationRef], start: datetime, end: datetime) -> ProviderFetch:
        result = ProviderFetch()
        positions: dict[str, dict[str, Any]] = {}
        try:
            xml = self._get("activestations.xml")
            positions = parse_active_stations(xml or "")
        except ProviderError as exc:
            result.warnings.append(f"activestations.xml unavailable: {exc}")
        consecutive_transient = 0
        first_error = ""
        fetched = 0
        for station in stations:
            out = result.station(station.code)
            sid = station.provider_id.upper()
            if sid in positions:
                p = positions[sid]
                out.station_updates = {
                    "latitude": p["latitude"],
                    "longitude": p["longitude"],
                    "position_source": "ndbc_activestations",
                    **({"name": f"NDBC {sid} {p['name']}"} if p.get("name") else {}),
                }
            if consecutive_transient >= 2:
                # Two stations in a row failed transiently: the host is down or blocked;
                # don't spend the retry budget on every remaining station.
                out.errors.append("skipped: NDBC host unreachable")
                continue
            try:
                records, files = self._station_records(sid, start, end, out.stats, result.warnings)
            except ProviderError as exc:
                consecutive_transient = consecutive_transient + 1 if exc.transient else 0
                first_error = first_error or str(exc)
                out.errors.append(str(exc))
                continue
            consecutive_transient = 0
            fetched += 1
            out.records, out.files = records, files
        result.requests = self.requests
        if stations and not fetched and first_error and consecutive_transient:
            raise TransientProviderError(f"ndbc: no station could be fetched ({first_error})")
        return result
