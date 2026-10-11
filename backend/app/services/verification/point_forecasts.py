"""Archive model forecasts at observation stations (``point_forecasts``).

Source ``gfswave_bulletin`` (family ``ncep_gfs``): NOAA GFS-Wave (WAVEWATCH III) station
bulletins, one text file per station and cycle on the NODD bucket::

    {NOAA_GFS_BASE_URL}/gfs.YYYYMMDD/HH/wave/station/bulls.tHHz/gfswave.{ID}.bull

Available from the 2021-03-22 18Z cycle onward (GFS v16; earlier cycles return 404).
Format, from a downloaded file (e.g. gfswave.56006.bull, 2026-10-10 00Z) and WW3's
``w3bullmd.F90`` (research notes, "GFS-Wave ... file layouts"):

* header ``Location : 56006 (33.36S 114.78E)`` (the model output point) and
  ``Cycle : 20261010  0 UTC``;
* one table row per hour, 0-384 h: ``| day hour | Hst n x | Hs Tp dir | ... |`` with Hst
  the total significant wave height (m), ``n`` the number of wave fields with Hs > 0.05 m
  in the 2-D spectrum and ``x`` the number with Hs > 0.15 m not shown, then up to six
  partitions (Hs m, peak period s, mean direction deg). ``*`` before a partition marks a
  probable local wind sea;
* ``.bull`` directions are *going-to*. The ``.cbull`` companion of the same cycle prints
  ``dir + 180`` labelled "from" (Hs in feet); tests/unit/test_point_forecasts.py checks this
  on real files. We store FROM directions: (dir + 180) mod 360.

What is stored per (station, cycle, valid time): ``hs_m`` = Hst; ``swell1_*`` = the first
partition *as listed* (it can be the wind sea: ``extra.swell1_is_wind_sea``); every
partition in ``extra.partitions``. The bulletin has no total-sea peak period, no total
direction and no wind, so ``tp_s``, ``direction_deg`` and the wind columns stay NULL.
``model_latitude/longitude`` come from the header and ``distance_km`` is the distance from
the station to that model point. Leads are hourly to 120 h and 3-hourly after that, up to
``FORECAST_DAYS`` days.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import BuoyStation, PointForecast
from app.services.forecasts.base import nominal_gfs_cycle
from app.services.geo import haversine_km
from app.services.http import (
    PermanentProviderError,
    ProviderError,
    TransientProviderError,
    request_with_retries,
)
from app.services.observations.stations import bulletin_stations

logger = logging.getLogger(__name__)

SOURCE_CODE = "gfswave_bulletin"
FAMILY = "ncep_gfs"
DEFAULT_CYCLES = (0, 12)
MAX_WORKERS = 8
HOURLY_UNTIL_HOURS = 120
STEP_AFTER_HOURS = 3
RECENT_LOOKBACK_HOURS = 48
FIRST_CYCLE = datetime(2021, 3, 22, 18, tzinfo=UTC)

_LOCATION_RE = re.compile(
    r"Location\s*:\s*(\S+)\s*\(\s*(\d+(?:\.\d+)?)([NS])\s+(\d+(?:\.\d+)?)([EW])\s*\)"
)
_CYCLE_RE = re.compile(r"Cycle\s*:\s*(\d{8})\s+(\d{1,2})\s*UTC")
_DAY_HOUR_RE = re.compile(r"^\s*(\d{1,2})\s+(\d{1,2})\s*$")
_TOTAL_RE = re.compile(r"^\s*(\d+\.\d+)\s+(\d+)\s*(\d+)?\s*$")
_PART_RE = re.compile(r"^\s*(\*)?\s*(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+)\s*$")
# Bulletin ids are WMO-style station ids (56006, 51201, 62163); they go into the URL path,
# so anything else is refused rather than requested.
_BULLETIN_ID_RE = re.compile(r"[A-Za-z0-9]{3,10}")


class BulletinFormatError(PermanentProviderError):
    """The text is not a GFS-Wave station bulletin in the expected layout."""


@dataclass(frozen=True)
class Partition:
    hs_m: float
    tp_s: float
    direction_deg: float  # FROM (bulletin going-to + 180)
    wind_sea: bool


@dataclass(frozen=True)
class BulletinRow:
    valid_time: datetime
    lead_hours: float
    hs_m: float
    n_fields: int
    n_hidden: int
    partitions: tuple[Partition, ...]


@dataclass
class Bulletin:
    station_id: str
    latitude: float
    longitude: float
    cycle_time: datetime
    rows: list[BulletinRow] = field(default_factory=list)
    # Rows / partition cells that did not match the layout (reported, not guessed).
    malformed: int = 0


def parse_location(text: str) -> tuple[str, float, float]:
    m = _LOCATION_RE.search(text)
    if not m:
        raise BulletinFormatError("bulletin has no 'Location : ID (lat lon)' header")
    lat = float(m.group(2)) * (1 if m.group(3) == "N" else -1)
    lon = float(m.group(4)) * (1 if m.group(5) == "E" else -1)
    return m.group(1), lat, lon


def parse_cycle(text: str) -> datetime:
    m = _CYCLE_RE.search(text)
    if not m:
        raise BulletinFormatError("bulletin has no 'Cycle : YYYYMMDD HH UTC' header")
    day = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC)
    return day + timedelta(hours=int(m.group(2)))


def _next_valid_time(previous: datetime, day: int, hour: int) -> datetime:
    """First time >= previous with this day-of-month and hour (rows only give DD HH)."""
    year, month = previous.year, previous.month
    for _ in range(3):
        try:
            candidate = datetime(year, month, day, hour, tzinfo=UTC)
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= previous:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    raise BulletinFormatError(f"cannot place day {day} hour {hour} after {previous:%Y-%m-%d %H}")


def going_to_to_from(direction: float) -> float:
    return (direction + 180.0) % 360.0


def parse_bulletin(text: str, expected_cycle: datetime | None = None) -> Bulletin:
    station_id, lat, lon = parse_location(text)
    cycle = parse_cycle(text)
    if expected_cycle is not None and cycle != expected_cycle:
        raise BulletinFormatError(
            f"bulletin is for cycle {cycle:%Y%m%d %H}Z, not {expected_cycle:%Y%m%d %H}Z"
        )
    bulletin = Bulletin(station_id, lat, lon, cycle)
    previous = cycle
    for line in text.splitlines():
        cells = line.strip().split("|")
        if len(cells) < 4 or cells[0] != "":
            continue
        dh = _DAY_HOUR_RE.match(cells[1])
        if not dh:
            continue  # header rows ("day &", "hour")
        total = _TOTAL_RE.match(cells[2])
        if not total:
            bulletin.malformed += 1
            continue
        valid = _next_valid_time(previous, int(dh.group(1)), int(dh.group(2)))
        previous = valid
        partitions = []
        for cell in cells[3:]:
            if not cell.strip():
                continue
            p = _PART_RE.match(cell)
            if not p:
                bulletin.malformed += 1
                break
            partitions.append(
                Partition(
                    hs_m=float(p.group(2)),
                    tp_s=float(p.group(3)),
                    direction_deg=going_to_to_from(float(p.group(4))),
                    wind_sea=p.group(1) == "*",
                )
            )
        bulletin.rows.append(
            BulletinRow(
                valid_time=valid,
                lead_hours=(valid - cycle).total_seconds() / 3600.0,
                hs_m=float(total.group(1)),
                n_fields=int(total.group(2)),
                n_hidden=int(total.group(3) or 0),
                partitions=tuple(partitions),
            )
        )
    if not bulletin.rows:
        raise BulletinFormatError(f"bulletin {station_id} has no data rows")
    return bulletin


@dataclass(frozen=True)
class CompactRow:
    """A ``.cbull`` row: Hs in whole feet, periods in whole seconds, directions FROM."""

    valid_time: datetime
    hs_ft: int
    partitions: tuple[tuple[int, int, int], ...]  # (Hs ft, Tp s, direction from)


def parse_cbull(text: str) -> tuple[str, datetime, list[CompactRow]]:
    """Parse the compact bulletin (used to cross-check the ``.bull`` conventions)."""
    station_id, _, _ = parse_location(text)
    cycle = parse_cycle(text)
    rows: list[CompactRow] = []
    previous = cycle
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or not re.fullmatch(r"\d{4}", tokens[0]) or (len(tokens) - 2) % 3:
            continue
        valid = _next_valid_time(previous, int(tokens[0][:2]), int(tokens[0][2:]))
        previous = valid
        values = [int(t) for t in tokens[1:]]
        parts = tuple((values[i], values[i + 1], values[i + 2]) for i in range(1, len(values), 3))
        rows.append(CompactRow(valid, values[0], parts))
    return station_id, cycle, rows


def bulletin_url(base_url: str, station_id: str, cycle: datetime) -> str:
    if not _BULLETIN_ID_RE.fullmatch(station_id):
        raise ValueError(f"invalid GFS-Wave bulletin id {station_id!r}")
    hh = f"{cycle.hour:02d}"
    return (
        f"{base_url.rstrip('/')}/gfs.{cycle:%Y%m%d}/{hh}/wave/station/bulls.t{hh}z/"
        f"gfswave.{station_id}.bull"
    )


def keep_lead(lead_hours: float, max_lead_hours: float) -> bool:
    if lead_hours < 0 or lead_hours > max_lead_hours:
        return False
    if lead_hours <= HOURLY_UNTIL_HOURS:
        return True
    return int(round(lead_hours)) % STEP_AFTER_HOURS == 0


def point_forecast_rows(
    bulletin: Bulletin, station: BuoyStation, max_lead_hours: float
) -> list[dict[str, Any]]:
    distance = round(
        haversine_km(station.latitude, station.longitude, bulletin.latitude, bulletin.longitude), 2
    )
    rows = []
    for r in bulletin.rows:
        if not keep_lead(r.lead_hours, max_lead_hours):
            continue
        first = r.partitions[0] if r.partitions else None
        rows.append(
            {
                "station_id": station.id,
                "source_code": SOURCE_CODE,
                "family": FAMILY,
                "cycle_time": bulletin.cycle_time,
                "valid_time": r.valid_time,
                "lead_hours": r.lead_hours,
                "hs_m": r.hs_m,
                "tp_s": None,  # no total-sea peak period in the bulletin
                "direction_deg": None,
                "swell1_hs_m": first.hs_m if first else None,
                "swell1_tp_s": first.tp_s if first else None,
                "swell1_direction_deg": first.direction_deg if first else None,
                "wind_speed_kmh": None,
                "wind_direction_deg": None,
                "hs_spread_m": None,
                "model_latitude": bulletin.latitude,
                "model_longitude": bulletin.longitude,
                "distance_km": distance,
                "extra": {
                    "bulletin_id": bulletin.station_id,
                    "hs_kind": "spectral_hm0",
                    "partition_period_kind": "tp",
                    "partition_direction_kind": "mean_from",
                    "swell1_is_wind_sea": bool(first and first.wind_sea),
                    "n_fields": r.n_fields,
                    "n_hidden": r.n_hidden,
                    "partitions": [
                        {
                            "hs_m": p.hs_m,
                            "tp_s": p.tp_s,
                            "direction_deg": p.direction_deg,
                            "wind_sea": p.wind_sea,
                        }
                        for p in r.partitions
                    ],
                },
            }
        )
    return rows


def cycles_between(start: datetime, end: datetime, cycles: Iterable[int]) -> list[datetime]:
    hours = sorted({int(h) for h in cycles})
    if any(h not in (0, 6, 12, 18) for h in hours):
        raise ValueError("cycles must be among 0, 6, 12, 18")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    out = []
    day: date = start.date()
    while day <= end.date():
        for h in hours:
            t = datetime(day.year, day.month, day.day, h, tzinfo=UTC)
            if start <= t <= end and t >= FIRST_CYCLE:
                out.append(t)
        day += timedelta(days=1)
    return out


@dataclass
class _Fetched:
    station_code: str
    cycle: datetime
    text: str | None = None
    missing: bool = False
    error: str | None = None
    transient: bool = False


def _fetch(
    client: httpx.Client, url: str, code: str, cycle: datetime, backoff_seconds: float
) -> _Fetched:
    try:
        response = request_with_retries(
            client,
            "GET",
            url,
            provider="gfswave_bulletin",
            attempts=4,
            backoff_seconds=backoff_seconds,
        )
    except ProviderError as exc:
        return _Fetched(code, cycle, error=str(exc), transient=exc.transient)
    if response.status_code == 404:
        # Not published (yet), or before the archive starts. The NODD bucket allows
        # public listing, so a missing key is a 404 (checked 2026-10-11); a 403 means
        # access was refused and is reported as an error below, not hidden as "missing".
        return _Fetched(code, cycle, missing=True)
    if response.status_code != 200:
        return _Fetched(code, cycle, error=f"HTTP {response.status_code} for {url}")
    return _Fetched(code, cycle, text=response.text)


def collect_range(
    db: Session,
    start: datetime,
    end: datetime,
    cycles: Iterable[int] = DEFAULT_CYCLES,
    stations: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    """Archive GFS-Wave bulletins for every (station with a bulletin id, cycle) in range.

    Idempotent: (station, cycle) pairs already stored are skipped and inserts ignore the
    unique constraint. Missing files (not published, or before the archive starts) are
    counted, not fatal; other failures are counted with their first messages.
    """
    settings = settings or get_settings()
    station_rows = bulletin_stations(db, stations)
    cycle_list = cycles_between(start, end, cycles)
    result: dict[str, Any] = {
        "source": SOURCE_CODE,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cycles": len(cycle_list),
        "stations": [s.code for s in station_rows],
        "already_stored": 0,
        "fetched": 0,
        "missing": 0,
        "errors": 0,
        "transient_errors": 0,
        "rows_inserted": 0,
        # Table rows / partition cells that did not match the bulletin layout (skipped).
        "malformed_cells": 0,
        "error_samples": [],
    }
    if not station_rows or not cycle_list:
        return result
    stored = {
        (row[0], row[1])
        for row in db.execute(
            select(PointForecast.station_id, PointForecast.cycle_time)
            .where(
                PointForecast.source_code == SOURCE_CODE,
                PointForecast.cycle_time >= cycle_list[0],
                PointForecast.cycle_time <= cycle_list[-1],
            )
            .distinct()
        ).all()
    }
    by_code = {s.code: s for s in station_rows}
    jobs = []
    for cycle in cycle_list:
        for s in station_rows:
            if (s.id, cycle) in stored:
                result["already_stored"] += 1
                continue
            try:
                url = bulletin_url(
                    settings.noaa_gfs_base_url, str(s.metadata_["bulletin_id"]), cycle
                )
            except ValueError as exc:
                result["errors"] += 1
                result["error_samples"].append(f"{s.code} {cycle:%Y%m%d%H}: {exc}")
                continue
            jobs.append((url, s.code, cycle))
    own_client = client is None
    http = client or httpx.Client(
        timeout=settings.forecast_http_timeout_seconds,
        headers={"User-Agent": "SwellTravelAgent/1.0 (verification)"},
    )
    max_lead = settings.forecast_days * 24.0
    workers = max(1, min(MAX_WORKERS, settings.grib_download_workers))
    per_station: dict[str, int] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch_start in range(0, len(jobs), 64):
                batch = jobs[batch_start : batch_start + 64]
                fetched = pool.map(
                    lambda job: _fetch(http, job[0], job[1], job[2], backoff_seconds), batch
                )
                for item in fetched:
                    _store(db, item, by_code, max_lead, result, per_station)
    finally:
        if own_client:
            http.close()
    result["rows_by_station"] = per_station
    result["error_samples"] = result["error_samples"][:10]
    return result


def _store(
    db: Session,
    item: _Fetched,
    by_code: dict[str, BuoyStation],
    max_lead: float,
    result: dict[str, Any],
    per_station: dict[str, int],
) -> None:
    if item.missing:
        result["missing"] += 1
        return
    if item.error is not None or item.text is None:
        result["errors"] += 1
        result["transient_errors"] += int(item.transient)
        result["error_samples"].append(f"{item.station_code} {item.cycle:%Y%m%d%H}: {item.error}")
        return
    station = by_code[item.station_code]
    try:
        bulletin = parse_bulletin(item.text, expected_cycle=item.cycle)
    except BulletinFormatError as exc:
        result["errors"] += 1
        result["error_samples"].append(f"{item.station_code} {item.cycle:%Y%m%d%H}: {exc}")
        return
    result["fetched"] += 1
    if bulletin.malformed:
        result["malformed_cells"] += bulletin.malformed
        result["error_samples"].append(
            f"{item.station_code} {item.cycle:%Y%m%d%H}: {bulletin.malformed} malformed "
            "row(s)/cell(s) skipped"
        )
    rows = point_forecast_rows(bulletin, station, max_lead)
    if not rows:
        return
    stmt = (
        insert(PointForecast)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["station_id", "source_code", "cycle_time", "valid_time"]
        )
        .returning(PointForecast.id)
    )
    inserted = len(db.execute(stmt).all())
    db.commit()
    result["rows_inserted"] += inserted
    per_station[station.code] = per_station.get(station.code, 0) + inserted


def collect_recent(
    db: Session,
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    """Job entry point: archive the newest cycles not stored yet (last 48 h of cycles).

    Bulletins appear later than the GRIB files: the 2026-10-10 12Z ones were written at
    17:25 UTC (about 5.4 h after the cycle) and the 11 Oct 00Z ones were still missing at
    05:27 UTC. A cycle that is not published yet is counted as ``missing`` and picked up
    by the next run, because every run re-checks the last 48 h of cycles.
    """
    settings = get_settings()
    if not settings.verification_enabled:
        return {"skipped": "VERIFICATION_ENABLED is false"}
    now = (now or utcnow()).astimezone(UTC)
    latest = nominal_gfs_cycle(now, settings.noaa_availability_lag_hours)
    start = latest - timedelta(hours=RECENT_LOOKBACK_HOURS)
    sources: dict[str, Any] = {}
    for source in settings.point_forecast_source_list:
        if source != SOURCE_CODE:
            sources[source] = {"skipped": "unsupported point-forecast source"}
            continue
        outcome = collect_range(
            db, start, latest, DEFAULT_CYCLES, client=client, backoff_seconds=backoff_seconds
        )
        sources[source] = outcome
        nothing_read = not outcome["fetched"] and not outcome["already_stored"]
        if outcome["transient_errors"] and nothing_read:
            raise TransientProviderError(
                f"{SOURCE_CODE}: every download failed ({outcome['error_samples'][:1]})"
            )
        if outcome["errors"] and nothing_read:
            # e.g. HTTP 403 on every file (wrong NOAA_GFS_BASE_URL or a policy change):
            # fail the job instead of reporting success with zero bulletins.
            raise PermanentProviderError(
                f"{SOURCE_CODE}: no bulletin could be read ({outcome['error_samples'][:1]})"
            )
    return {"latest_cycle": latest.isoformat(), "sources": sources}
