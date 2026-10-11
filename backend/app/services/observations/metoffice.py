"""UK Met Office marine observations (public S3 bucket, CC BY-SA 4.0).

Layout (research notes, "Forecast validation methodology", UK buoys; re-verified 2026-10-10
and 2026-10-11 by listing ``met-office-marine-observations-data``):
``{issue}_{start}_{end}/{station}_buoy_{uuid}.csv`` with ``YYYYMMDDHHMM`` UTC stamps. Each
folder covers one hour (start HH:00, end HH:59, issued about 75 minutes after the start)
and each buoy file holds that hour's single row; the bucket keeps a rolling ~8 days
(192-198 folders), which is what the notes call "rolling 168 h". Non-buoy files
(``*_automatic_weather_station_*``) are ignored. If an hour were ever re-issued, the
newest issue wins.

Files are pipe-delimited with a header row. Every measured column ``x`` has a companion
``x_qc`` holding JSON such as ``{"good": []}`` or ``{"suspect": ["1 case(s) of ..."]}``
(automated real-time checks). Values whose QC verdict is not "good" are dropped and
counted. Rows that passed are stored as ``probably_good``: the feed has automated checks
only, no delayed-mode review.

Column mapping:

* ``significant_wave_height_hm_0`` -> hs (``spectral_hm0``); ``..._h_third`` only when
  hm_0 is missing (``h_third``). ``..._hm_0_swell`` / ``..._hm_0_windsea`` -> swell /
  wind-sea heights.
* ``wave_peak_period`` -> Tp; ``wave_peak_period_swell`` and
  ``wave_peak_direction_swell`` -> swell partition; ``wave_mean_period_tm_02`` -> Tm02.
* ``wave_peak_direction`` / ``wave_mean_direction`` -> FROM directions.
* ``wind_speed_marine`` (m/s) x 3.6 -> km/h; ``wind_direction_marine`` FROM.

Units are not stated in the files. Two checks against GFS-Wave station output for
2026-10-10 00-12Z (12 buoy/model pairs, K1, K2, K5, K7, Brittany, Channel): the wind
speed ratio buoy / model (m/s) was 0.92-1.34 (median about 1.05; knots would give about
1.9), so wind is in m/s; wave peak directions differed from the ``.cbull`` "from"
directions by a median of 11 degrees (max 44, none near 180), so directions are FROM.
Wind is measured at the buoy's anemometer height and is not adjusted to 10 m.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
    round_to_minute,
    to_float,
    validate_record,
)
from app.services.observations.s3 import list_objects, object_url

logger = logging.getLogger(__name__)

PROVIDER = "metoffice"
DATASET = "metoffice_marine_hourly"
WIND_SPEED_TO_KMH = 3.6  # files carry m/s (see module docstring)
_FOLDER_RE = re.compile(r"^(\d{12})_(\d{12})_(\d{12})$")
_BUOY_RE = re.compile(r"^(?P<station>[a-z0-9_]+?)_buoy_[0-9a-fA-F-]+\.csv$")
# Folder names start with the issue time, which is ~75 min after the hour it covers.
LISTING_MARGIN = timedelta(hours=6)

# Stored field -> CSV column.
COLUMN_MAP = {
    "tp_s": "wave_peak_period",
    "tm_s": "wave_mean_period_tm_02",
    "peak_direction_deg": "wave_peak_direction",
    "mean_direction_deg": "wave_mean_direction",
    "swell_hs_m": "significant_wave_height_hm_0_swell",
    "swell_tp_s": "wave_peak_period_swell",
    "swell_direction_deg": "wave_peak_direction_swell",
    "windsea_hs_m": "significant_wave_height_hm_0_windsea",
    "wind_speed_kmh": "wind_speed_marine",
    "wind_direction_deg": "wind_direction_marine",
}
HS_COLUMNS = (
    ("significant_wave_height_hm_0", "spectral_hm0"),
    ("significant_wave_height_h_third", "h_third"),
)


@dataclass(frozen=True)
class IssueFolder:
    name: str
    issued_at: datetime
    start: datetime
    end: datetime  # inclusive (HH:59)


def _stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=UTC)


def parse_folder(name: str) -> IssueFolder | None:
    m = _FOLDER_RE.match(name.strip("/"))
    if not m:
        return None
    try:
        return IssueFolder(name.strip("/"), *(_stamp(g) for g in m.groups()))
    except ValueError:
        return None


def station_from_filename(filename: str) -> str | None:
    """'k5_buoy_<uuid>.csv' -> 'k5'; None for anything that is not a buoy file."""
    m = _BUOY_RE.match(filename)
    return m.group("station") if m else None


def qc_verdict(raw: str | None) -> str | None:
    """'good', 'flagged' (any other verdict) or None (no QC information)."""
    if raw is None or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "flagged"
    if isinstance(data, dict) and data and set(data) == {"good"}:
        return "good"
    return "flagged"


def parse_csv(
    text: str, *, start: datetime, end: datetime, stats: ParseStats, source: str = ""
) -> tuple[list[ObservationRecord], dict[str, Any] | None]:
    """Records in [start, end] from one buoy file, plus its latest reported position."""
    reader = csv.DictReader(io.StringIO(text), delimiter="|", quoting=csv.QUOTE_NONE)
    if not reader.fieldnames or "timestep" not in reader.fieldnames:
        raise PermanentProviderError("metoffice: file has no 'timestep' header")
    records: list[ObservationRecord] = []
    position: dict[str, Any] | None = None
    for row in reader:
        try:
            t = round_to_minute(datetime.fromisoformat(row["timestep"].replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            stats.invalid_values["timestep"] += 1
            continue
        if not start <= t <= end:
            continue
        stats.rows_read += 1
        rec = ObservationRecord(t, "probably_good", DATASET, source=source)
        verdicts: list[str | None] = []
        for column, kind in HS_COLUMNS:
            value, verdict = _checked(row, column, stats)
            if value is not None:
                rec.hs_m, rec.hs_kind = value, kind
                verdicts.append(verdict)
                break
        for name, column in COLUMN_MAP.items():
            value, verdict = _checked(row, column, stats)
            if value is None:
                continue
            if name == "wind_speed_kmh":
                value *= WIND_SPEED_TO_KMH
            setattr(rec, name, value)
            if name == "tp_s" and rec.hs_m is None:
                verdicts.append(verdict)
        if rec.tm_s is not None:
            rec.tm_kind = "tm02"
        # Row QC follows the primary variable: no QC information -> not_qc.
        if verdicts and verdicts[0] is None:
            rec.qc_flag = "not_qc"
        valid = validate_record(rec, stats)
        if valid is None:
            continue
        records.append(valid)
        lat, lon = to_float(row.get("latitude")), to_float(row.get("longitude"))
        if lat is not None and lon is not None:
            position = {
                "latitude": lat,
                "longitude": lon,
                "observed_at": t.isoformat(),
                "dataset": DATASET,
            }
    stats.rows_kept += len(records)
    return records, position


def _checked(
    row: dict[str, str], column: str, stats: ParseStats
) -> tuple[float | None, str | None]:
    value = to_float(row.get(column))
    if value is None:
        return None, None
    verdict = qc_verdict(row.get(f"{column}_qc"))
    if verdict == "flagged":
        stats.flagged_values[column] += 1
        return None, verdict
    return value, verdict


class MetOfficeProvider:
    name = PROVIDER

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.metoffice_base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (observations)"},
        )
        self.backoff_seconds = backoff_seconds
        self.workers = min(8, self.settings.grib_download_workers)
        self.requests = 0

    def select_files(
        self, keys: list[str], wanted: set[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[IssueFolder, str]]]:
        """Newest issue per (station, hour window) overlapping [start, end]."""
        best: dict[tuple[str, datetime, datetime], tuple[IssueFolder, str]] = {}
        for key in keys:
            folder_name, _, filename = key.partition("/")
            folder = parse_folder(folder_name)
            station = station_from_filename(filename)
            if folder is None or station not in wanted:
                continue
            if folder.end < start or folder.start > end:
                continue
            slot = (station, folder.start, folder.end)
            if slot not in best or folder.issued_at > best[slot][0].issued_at:
                best[slot] = (folder, key)
        out: dict[str, list[tuple[IssueFolder, str]]] = {}
        for (station, _, _), item in sorted(best.items()):
            out.setdefault(station, []).append(item)
        return out

    def _get_text(self, key: str) -> str:
        self.requests += 1
        url = object_url(self.base_url, key)
        response = request_with_retries(
            self.client, "GET", url, provider=PROVIDER, backoff_seconds=self.backoff_seconds
        )
        if response.status_code != 200:
            raise PermanentProviderError(f"metoffice: HTTP {response.status_code} for {key}")
        return response.text

    def fetch(self, stations: list[StationRef], start: datetime, end: datetime) -> ProviderFetch:
        result = ProviderFetch()
        by_slug = {s.provider_id: s for s in stations}
        listing = list_objects(
            self.client,
            self.base_url,
            "",
            provider=PROVIDER,
            start_after=(start - LISTING_MARGIN).strftime("%Y%m%d%H%M"),
            backoff_seconds=self.backoff_seconds,
        )
        self.requests += listing.requests
        selected = self.select_files([o.key for o in listing.objects], set(by_slug), start, end)
        jobs = [(slug, folder, key) for slug, items in selected.items() for folder, key in items]

        def download(job: tuple[str, IssueFolder, str]) -> tuple[str, IssueFolder, str, Any]:
            slug, folder, key = job
            try:
                return slug, folder, key, self._get_text(key)
            except ProviderError as exc:
                return slug, folder, key, exc

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            downloaded = list(pool.map(download, jobs))
        transient = 0
        newest: dict[tuple[str, datetime], datetime] = {}
        for slug, folder, key, payload in downloaded:
            out = result.station(by_slug[slug].code)
            if isinstance(payload, Exception):
                transient += int(getattr(payload, "transient", False))
                out.errors.append(f"{key}: {payload}")
                continue
            try:
                records, position = parse_csv(
                    payload, start=start, end=end, stats=out.stats, source=folder.name
                )
            except ProviderError as exc:
                out.errors.append(f"{key}: {exc}")
                continue
            out.files += 1
            for rec in records:
                # The same hour in two issues: keep the newest issue's record.
                slot = (slug, rec.observed_at)
                if slot in newest and newest[slot] >= folder.issued_at:
                    continue
                if slot in newest:
                    out.records = [r for r in out.records if r.observed_at != rec.observed_at]
                newest[slot] = folder.issued_at
                out.records.append(rec)
            out.note_position(position)
        for station in stations:
            result.station(station.code)
        result.requests = self.requests
        if jobs and transient == len(jobs):
            raise TransientProviderError(f"metoffice: all {len(jobs)} downloads failed")
        return result
