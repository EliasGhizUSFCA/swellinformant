"""NOAA NDBC buoys (www.ndbc.noaa.gov; US Government work, public domain).

Verified live on 2026-10-12 against the real files of stations 46026 (NDBC 3-m discus,
SCOOP payload), 51201 and 46224 (CDIP Datawell Waveriders relayed by NDBC) and others;
see docs/observations.md. NDBC documents cited below: "Measurement Descriptions and
Units" (https://www.ndbc.noaa.gov/faq/measdes.shtml), "How are significant wave height,
dominant period, average period, and wave steepness calculated?"
(https://www.ndbc.noaa.gov/faq/wavecalc.shtml), "Acquisition Time"
(https://www.ndbc.noaa.gov/faq/acq.shtml), the wave glossary
(https://www.ndbc.noaa.gov/waveobs.shtml) and the NDBC Web Data Guide (June 2023,
https://www.ndbc.noaa.gov/docs/ndbc_web_data_guide.pdf).

Files (every name checked live; station ids in file names are lower case):

* realtime, last 45 days: ``{base}/data/realtime2/{ID}.txt`` (standard meteorological) and
  ``{ID}.spec`` (detailed wave summary). Missing values are ``MM``; in ``.spec`` also
  ``-99``, and ``NO`` for "no swell could be determined" (wave glossary).
* complete years: ``{base}/data/historical/stdmet/{id}h{YYYY}.txt.gz`` (gzip; about 9
  times smaller than the decompressed ``view_text_file.php`` answer, so the .gz is read).
* months of the current year, quality controlled (Web Data Guide, section 4):
  ``{base}/data/stdmet/{Mon}/{id}{m}{YYYY}.txt.gz`` with ``m`` = 1..9 for January to
  September (seen live) and ``a``, ``b``, ``c`` for October to December (NDBC's naming,
  NOT seen live: those directories were empty on 2026-10-12, so a wrong guess only shows
  as a 404 warning). The newest month is served uncompressed as
  ``{base}/data/stdmet/{Mon}/{id}.txt`` (seen live: ``Sep/51201.txt`` on 2026-10-12), so a
  404 on the .gz name is retried under that name. Early in a year, before the previous
  year's yearly file is published, its months are read the same way.

Missing values in the yearly and monthly files are runs of 9s with a PER-COLUMN meaning
(measdes: "a variable number of 9's ... depending on the data type"): WVHT/DPD/APD
99.00, MWD/WDIR 999, WSPD/GST 99.0, PRES 9999.0, ATMP/WTMP/DEWP 999.0, VIS 99.0, TIDE
99.00. A blanket mask would delete valid values (a WDIR of 99 degrees is a real wind
direction), so sentinels are matched per column. Older yearly files use a 2-digit year
and no minute column (``WD``/``BAR`` names); both are handled.

Times: "Both Realtime and Historical files show times in UTC only" (measdes). For CDIP
buoys the time is the end of CDIP's 1600 s sample (51201: NDBC time = CDIP
``waveTime`` sample start + 26 min, 881 of 881 matches checked live). For NDBC buoys
with 20-minute wave acquisitions the archived files and ``.spec`` give the wave time
(:10 and :40 at 46026), but realtime ``.txt`` prints each wave report on the
10-minute meteorological rows next to that time, 10 min before to 10 min after ("wave
information is displayed on the same lines as meteorological data", acq.shtml), often
with DPD on only one of the copies: :func:`merge_spec` folds those copies into one
record at the ``.spec`` time. (At the base commit every copy was stored, 2 records per
report at 46026.)

Meanings:

* WVHT is the spectral significant wave height Hm0 = 4·sqrt(m0), m0 summed over the
  non-directional spectrum (wavecalc.shtml: "WVHT = 4 × sqrt(m0) ... variance is
  calculated using the nondirectional wave spectrum"); measdes describes it as
  "approximately equal to the average of the highest one-third", which is the
  traditional meaning, not how it is computed. Stored as ``hs_kind = spectral_hm0``.
  Live check: WVHT of 51201 equals CDIP's ``waveHs`` exactly.
* DPD is the dominant (peak) period, 1/fp of the spectral band with the maximum density
  (``tp_s``). Realtime ``.txt`` prints it in whole seconds, the archives to 0.01 s.
* APD depends on who processes the buoy (wavecalc.shtml): sqrt(m0/m2) (Tm02) for NDBC's
  own systems, m0/m1 (Tm01) for Scripps (CDIP) buoys, a zero-crossing period for UK,
  Irish and French buoys shown by NDBC. The catalog states it per station
  (``apd_kind``: ``tm02`` or ``tm01``); without it the period is stored as ``apd``
  (undefined). Live check: APD of 51201 equals CDIP ``waveTa`` (first moment) within
  0.005 s and differs from CDIP ``waveTz`` (second moment) by 0.53 s (median).
* MWD is "the direction from which the waves at the dominant period (DPD) are coming",
  degrees true (measdes), i.e. a peak direction FROM (``peak_direction_deg``). Live
  check: equal to CDIP ``waveDp`` within 0.5 degree at 51201.
* WDIR/WSPD: wind FROM (degrees true) / m/s averaged over 8 minutes at the anemometer
  height (not adjusted to 10 m), stored in km/h.
* ``.spec``: SwH/SwP/SwD swell and WWH wind-sea split; SwD is a 16-point compass name
  (22.5 degree resolution) of the direction the swell comes FROM, stored as the sector
  centre. Realtime heights are rounded to 0.1 m; ``.txt`` and ``.spec`` round ties
  differently (0.95 m becomes 1.0 and 0.9), so they are matched with a 0.1 m tolerance.

QC: realtime data have passed automated checks only and carry no per-value flags
(``not_qc``); the yearly and monthly files are NDBC's quality-controlled archive
(``probably_good``, no per-value flags either). Positions: ``activestations.xml``
(current deployment of every active station) replaces the catalog position on ingest;
it holds no water depth (the station pages do).
"""

from __future__ import annotations

import bisect
import gzip
import logging
import re
import zlib
from dataclasses import dataclass, field, fields
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
MONTHLY_DATASET = "ndbc_monthly_stdmet"
QC_BY_DATASET = {
    REALTIME_DATASET: "not_qc",
    HISTORICAL_DATASET: "probably_good",
    MONTHLY_DATASET: "probably_good",
}
REALTIME_DAYS = 44  # realtime2 files hold ~45 days
# NDBC station ids are short alphanumerics (46026, 51201, KIKT1). Ids go into URL paths,
# so anything else is refused rather than requested.
STATION_ID_RE = re.compile(r"[A-Za-z0-9]{1,8}")
# APD definitions a catalog entry may declare (see the module docstring).
APD_KINDS = ("tm01", "tm02", "apd")
MONTH_DIRS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# File-name month codes: 1-9 seen live; a/b/c (October-December) are NDBC's convention
# but could not be observed (the directories were empty when this was verified).
MONTH_CODES = "123456789abc"

# Realtime .txt repeats a wave report on the 10-minute met rows next to the wave time:
# lags (.txt row - .spec time) of -10, 0 and +10 min (+-1) at 46026, 46053, 46054, 46086
# and 51101 on 2026-10-12, never more. Reports are 30 min apart, so a row within half of
# that of a report belongs to it. .txt and .spec heights can differ by one 0.1 m step.
COPY_WINDOW = timedelta(minutes=15)
COPY_HS_TOLERANCE_M = 0.1 + 1e-6
COPY_PERIOD_TOLERANCE_S = 0.05 + 1e-6
COPY_DIRECTION_TOLERANCE_DEG = 1.0

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
# .spec missing-value codes besides MM (wave glossary: "MM or -99 denote missing data").
SPEC_MISSING = frozenset({"MM", "-99", "-99.0", "-99.00", "-99.000"})
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
_WAVE_FIELDS = ("hs_m", "tp_s", "tm_s", "peak_direction_deg")


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


def _spec_value(column: str, raw: str | None) -> float | None:
    return None if raw is None or raw in SPEC_MISSING else _value(column, raw)


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


def _apd_kind(kind: str | None) -> str:
    return kind if kind in APD_KINDS else "apd"


def parse_stdmet(
    text: str,
    *,
    dataset: str,
    start: datetime,
    end: datetime,
    stats: ParseStats,
    apd_kind: str = "apd",
) -> list[ObservationRecord]:
    """Standard meteorological file (realtime2 ``.txt``, yearly or monthly archive).

    ``apd_kind`` is the station's APD definition (``tm02`` NDBC systems, ``tm01`` CDIP
    buoys, ``apd`` when not known). Wind-only rows are not stored.
    """
    qc = QC_BY_DATASET.get(dataset, "not_qc")
    tm_kind = _apd_kind(apd_kind)
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
        rec.tm_kind = tm_kind if rec.tm_s is not None else None
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
            "hs_m": _spec_value("WVHT", values.get("WVHT")),
            "swell_hs_m": _spec_value("SwH", values.get("SwH")),
            "swell_tp_s": _spec_value("SwP", values.get("SwP")),
            "swell_direction_deg": COMPASS.get(values.get("SwD", "")),
            "windsea_hs_m": _spec_value("WWH", values.get("WWH")),
            "tm_s": _spec_value("APD", values.get("APD")),
            "peak_direction_deg": _spec_value("MWD", values.get("MWD")),
        }
    return out


def _close(a: float | None, b: float | None, tolerance: float, *, circular: bool = False) -> bool:
    """True when either value is missing or both agree within ``tolerance``."""
    if a is None or b is None:
        return True
    d = abs(a - b)
    if circular:
        d = min(d, 360.0 - d)
    return d <= tolerance


def _same_report(rec: ObservationRecord, spec: dict[str, Any]) -> bool:
    """Is ``rec`` (a .txt row) a copy of the wave report summarised in ``spec``?"""
    if rec.hs_m is None or spec.get("hs_m") is None:
        return False
    return (
        abs(rec.hs_m - spec["hs_m"]) <= COPY_HS_TOLERANCE_M
        and _close(rec.tm_s, spec.get("tm_s"), COPY_PERIOD_TOLERANCE_S)
        and _close(
            rec.peak_direction_deg,
            spec.get("peak_direction_deg"),
            COPY_DIRECTION_TOLERANCE_DEG,
            circular=True,
        )
    )


def _identical_report(a: ObservationRecord, b: ObservationRecord) -> bool:
    """Two .txt rows carrying the same wave values (DPD may be missing on one)."""
    return (
        a.hs_m is not None
        and a.hs_m == b.hs_m
        and a.tm_s == b.tm_s
        and a.peak_direction_deg == b.peak_direction_deg
        and _close(a.tp_s, b.tp_s, 0.0)
    )


def _combine(observed_at: datetime, copies: list[ObservationRecord]) -> ObservationRecord:
    """One record at ``observed_at`` from copies ordered by preference (closest first):
    every value comes from the first copy that has it."""
    first = copies[0]
    out = ObservationRecord(observed_at, first.qc_flag, first.dataset, source=first.source)
    for f in fields(ObservationRecord):
        if f.name in ("observed_at", "qc_flag", "dataset", "source", "hs_kind", "tm_kind"):
            continue
        setattr(
            out,
            f.name,
            next((getattr(c, f.name) for c in copies if getattr(c, f.name) is not None), None),
        )
    out.hs_kind = "spectral_hm0" if out.hs_m is not None else "unknown"
    out.tm_kind = next((c.tm_kind for c in copies if c.tm_s is not None), None)
    return out


def _collapse_copies(
    records: list[ObservationRecord], stats: ParseStats
) -> list[ObservationRecord]:
    """Fold consecutive identical .txt wave rows within COPY_WINDOW into the earliest."""
    out: list[ObservationRecord] = []
    group: list[ObservationRecord] = []
    for rec in sorted(records, key=lambda r: r.observed_at):
        if (
            group
            and rec.observed_at - group[0].observed_at <= COPY_WINDOW
            and _identical_report(group[0], rec)
        ):
            group.append(rec)
            continue
        if group:
            out.append(_combine(group[0].observed_at, group))
        group = [rec]
    if group:
        out.append(_combine(group[0].observed_at, group))
    stats.duplicates_merged += len(records) - len(out)
    return out


def merge_spec(
    records: list[ObservationRecord],
    spec: dict[datetime, dict[str, Any]],
    stats: ParseStats,
    *,
    apd_kind: str = "apd",
) -> list[ObservationRecord]:
    """Realtime records from ``.txt`` rows and the ``.spec`` summary, one per wave report.

    A ``.txt`` wave row is a copy of the closest ``.spec`` report within COPY_WINDOW
    (either side) with the same height (within one 0.1 m rounding step), APD and MWD; all
    copies of a report become one record at the ``.spec`` time (the time the archived
    files use), each value taken from the closest copy that has it, plus the swell /
    wind-sea split.
    ``.txt`` rows that match no ``.spec`` report are still collapsed when consecutive
    rows repeat the same values; ``.spec`` reports without a ``.txt`` row become records
    of their own (no DPD). ``stats.rows_kept`` is corrected to the records returned.
    """
    tm_kind = _apd_kind(apd_kind)
    kept_before = len(records)
    spec_times = sorted(spec)
    copies: dict[datetime, list[tuple[timedelta, ObservationRecord]]] = {}
    unmatched: list[ObservationRecord] = []
    for rec in records:
        # .spec reports within COPY_WINDOW of the row that carry its values, closest first.
        lo = bisect.bisect_left(spec_times, rec.observed_at - COPY_WINDOW)
        hi = bisect.bisect_right(spec_times, rec.observed_at + COPY_WINDOW)
        candidates = [t for t in spec_times[lo:hi] if _same_report(rec, spec[t])]
        if not candidates:
            unmatched.append(rec)
            continue
        match = min(candidates, key=lambda t: (abs(rec.observed_at - t), t))
        copies.setdefault(match, []).append((abs(rec.observed_at - match), rec))
    out = _collapse_copies(unmatched, stats)
    for t in spec_times:
        values = spec[t]
        group = [
            rec for _, rec in sorted(copies.get(t, []), key=lambda c: (c[0], c[1].observed_at))
        ]
        if group:
            stats.duplicates_merged += len(group) - 1
            rec = _combine(t, group)
        else:
            stats.rows_read += 1
            rec = ObservationRecord(t, "not_qc", REALTIME_DATASET, source="spec")
            rec.hs_m = values["hs_m"]
            rec.hs_kind = "spectral_hm0" if rec.hs_m is not None else "unknown"
            rec.tm_s = values["tm_s"]
            rec.tm_kind = tm_kind if rec.tm_s is not None else None
            rec.peak_direction_deg = values["peak_direction_deg"]
            if not rec.has_wave_data():
                stats.no_wave_data += 1
                continue
        for name in ("swell_hs_m", "swell_tp_s", "swell_direction_deg", "windsea_hs_m"):
            setattr(rec, name, values[name])
        valid = validate_record(rec, stats)
        if valid is not None:
            out.append(valid)
    out.sort(key=lambda r: r.observed_at)
    # Two reports can still land on one time (a .txt-only row at a .spec time); keep one.
    by_time: dict[datetime, ObservationRecord] = {}
    for rec in out:
        prev = by_time.get(rec.observed_at)
        if prev is None or rec.value_count() > prev.value_count():
            by_time[rec.observed_at] = rec
    stats.duplicates_merged += len(out) - len(by_time)
    merged = sorted(by_time.values(), key=lambda r: r.observed_at)
    stats.rows_kept += len(merged) - kept_before
    return merged


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


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _next_month(year: int, month: int) -> datetime:
    return _month_start(year + month // 12, month % 12 + 1)


@dataclass
class _File:
    """One file of a station's plan: candidate paths tried in order (404 -> next)."""

    kind: str  # "txt" | "spec" | "year" | "month"
    paths: list[str]
    label: str
    dataset: str
    year: int = 0
    months: list[int] = field(default_factory=list)  # months to read if a yearly file is 404


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
        self.bytes_downloaded = 0

    def _get_bytes(self, path: str) -> bytes | None:
        """Body of a file; None when NDBC answers 404 (no such file)."""
        self.requests += 1
        response = request_with_retries(
            self.client,
            "GET",
            f"{self.base_url}/{path}",
            provider=PROVIDER,
            backoff_seconds=self.backoff_seconds,
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise PermanentProviderError(f"ndbc: HTTP {response.status_code} for {path}")
        body = response.content
        self.bytes_downloaded += len(body)
        return body

    def _get_text(self, path: str) -> str | None:
        body = self._get_bytes(path)
        if body is None:
            return None
        if path.endswith(".gz"):
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError, zlib.error) as exc:
                # Defensive: a 200 answer that is not gzip (an HTML error page, say) is
                # an error, not data. Every missing file seen live answered 404.
                raise PermanentProviderError(f"ndbc: {path} is not a gzip file ({exc})") from exc
        return body.decode("ascii", errors="replace")

    def _month_file(self, sid: str, year: int, month: int) -> _File:
        mon = MONTH_DIRS[month - 1]
        lower = sid.lower()
        return _File(
            "month",
            [
                f"data/stdmet/{mon}/{lower}{MONTH_CODES[month - 1]}{year}.txt.gz",
                f"data/stdmet/{mon}/{lower}.txt",
            ],
            f"{lower} {mon} {year} monthly",
            MONTHLY_DATASET,
            year,
        )

    def _months_needed(
        self, year: int, start: datetime, end: datetime, realtime_from: datetime, now: datetime
    ) -> list[int]:
        """Months of ``year`` overlapping [start, end] not fully inside the realtime file."""
        months = []
        for month in range(1, 13):
            lo, hi = _month_start(year, month), _next_month(year, month)
            if hi <= start or lo > end or lo > now:
                continue
            if lo >= realtime_from and end >= realtime_from:
                continue  # realtime2 covers the whole month
            months.append(month)
        return months

    def plan(self, station_id: str, start: datetime, end: datetime) -> list[_File]:
        """Files one station needs for [start, end], in fetch order."""
        if not STATION_ID_RE.fullmatch(station_id):
            raise PermanentProviderError(f"ndbc: invalid station id {station_id!r}")
        now = self.now or datetime.now(UTC)
        realtime_from = now - timedelta(days=REALTIME_DAYS)
        sid = station_id.upper()
        plan: list[_File] = []
        if end >= realtime_from:
            for kind in ("txt", "spec"):
                path = f"data/realtime2/{sid}.{kind}"
                plan.append(_File(kind, [path], path, REALTIME_DATASET))
        for year in range(start.year, min(end.year, now.year) + 1):
            if max(start, _month_start(year, 1)) >= realtime_from and end >= realtime_from:
                continue  # the window's part of this year is all in the realtime file
            if year < now.year:
                name = f"{sid.lower()}h{year}.txt.gz"
                plan.append(
                    _File(
                        "year",
                        [f"data/historical/stdmet/{name}"],
                        name,
                        HISTORICAL_DATASET,
                        year,
                        self._months_needed(year, start, end, realtime_from, now),
                    )
                )
            else:
                plan += [
                    self._month_file(sid, year, m)
                    for m in self._months_needed(year, start, end, realtime_from, now)
                ]
        return plan

    def _station_records(
        self,
        station_id: str,
        start: datetime,
        end: datetime,
        stats: ParseStats,
        warnings: list[str],
        errors: list[str] | None = None,
        *,
        apd_kind: str = "apd",
    ) -> tuple[list[ObservationRecord], int]:
        """Records of one station from its realtime, yearly and monthly files.

        A file that cannot be fetched or parsed is reported in ``errors`` and the station
        keeps what its other files gave (partial results are kept, not dropped). After a
        transient failure the station's remaining files are skipped (the host is probably
        down, so their retries would only burn time). The first failure is raised only
        when no file at all could be read. A file missing under every candidate name is a
        coverage warning, not an error. A yearly file that is missing (not published yet)
        is replaced by the monthly files of that year.
        """
        errors = errors if errors is not None else []
        queue = self.plan(station_id, start, end)
        realtime: list[ObservationRecord] = []
        archived: list[ObservationRecord] = []
        spec: dict[datetime, dict[str, Any]] | None = None
        files = 0
        failures: list[ProviderError] = []
        while queue:
            item = queue.pop(0)
            try:
                text = None
                for path in item.paths:
                    text = self._get_text(path)
                    if text is not None:
                        break
                if text is None:
                    if item.kind == "year" and item.months:
                        queue[0:0] = [
                            self._month_file(station_id.upper(), item.year, m) for m in item.months
                        ]
                    elif item.kind in ("year", "month"):
                        # Not an error (no data then, or not published yet), but the
                        # window is then not covered: say so.
                        warnings.append(f"ndbc {station_id}: {item.label} not found (404)")
                    continue
                if item.kind == "txt":
                    realtime = parse_stdmet(
                        text,
                        dataset=REALTIME_DATASET,
                        start=start,
                        end=end,
                        stats=stats,
                        apd_kind=apd_kind,
                    )
                elif item.kind == "spec":
                    spec = parse_spec(text, start=start, end=end)
                else:
                    archived += parse_stdmet(
                        text,
                        dataset=item.dataset,
                        start=start,
                        end=end,
                        stats=stats,
                        apd_kind=apd_kind,
                    )
                files += 1
            except ProviderError as exc:
                failures.append(exc)
                errors.append(f"{item.label}: {exc}")
                if exc.transient:
                    break
        realtime = merge_spec(realtime, spec or {}, stats, apd_kind=apd_kind)
        if not files and failures:
            raise next((f for f in failures if f.transient), failures[0])
        return realtime + archived, files

    def fetch(self, stations: list[StationRef], start: datetime, end: datetime) -> ProviderFetch:
        result = ProviderFetch()
        positions: dict[str, dict[str, Any]] = {}
        try:
            xml = self._get_text("activestations.xml")
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
            file_errors: list[str] = []
            try:
                records, files = self._station_records(
                    sid,
                    start,
                    end,
                    out.stats,
                    result.warnings,
                    file_errors,
                    apd_kind=str((station.metadata or {}).get("apd_kind") or "apd"),
                )
            except ProviderError as exc:
                consecutive_transient = consecutive_transient + 1 if exc.transient else 0
                first_error = first_error or str(exc)
                out.errors.append(str(exc))
                continue
            consecutive_transient = 0
            fetched += 1
            out.records, out.files = records, files
            out.errors.extend(file_errors)
        result.requests = self.requests
        result.bytes_downloaded = self.bytes_downloaded
        if stations and not fetched and first_error and consecutive_transient:
            raise TransientProviderError(f"ndbc: no station could be fetched ({first_error})")
        return result
