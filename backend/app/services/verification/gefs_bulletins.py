"""Archive GEFS-Wave ensemble statistics at observation stations (``point_forecasts``).

Source ``gefswave_station`` (family ``ncep_gefs``): the station bulletins of NCEP's Global
Wave Ensemble System, one tar file per cycle with one text file per station::

    {NOAA_GEFS_BASE_URL}/gefs.YYYYMMDD/HH/wave/station/gefs.wave.tHHz.bull_tar
        -> gefs.wave.{ID}.bull

Layout, from downloaded files (tests/unit/test_gefs_bulletins.py uses real ones):

* header ``Location : 51201 (21.671N -158.117W)`` and ``Cycle : 20261011 t00z UTC``. The
  longitude is the SIGNED longitude east (-158.117 = 158.117 W); the trailing letter is
  always ``W``, also for eastern stations (52200, Guam: ``144.788W`` = 144.788 E), so the
  sign is used and the letter ignored. The latitude carries a correct N/S letter;
* one row per valid time, 3-hourly to 240 h and 6-hourly to 384 h:
  ``| DD HH | Hs avg | Hs spr | Tp avg | Tp spr | U10avg | U10spr | P(Hs>1 m) ... P(Hs>9 m) |``
  with ``avg`` the ensemble mean and ``spr`` the spread, i.e. the standard deviation of
  the members (legend of the file); Hs in m, Tp (peak period) in s, U10 in m/s.

Files of cycles before 2026-02-26 00Z (first seen with spreads: 2026-02-26; still
averages-only on 2026-02-25, checked by file size and content on 2026-10-12) print only
three numbers per row under the same twelve-column header. Which quantities those three
are cannot be read from the file without guessing, so such files are refused
(:class:`GefsBulletinFormatError`), never stored.

What is stored per (station, cycle, valid time): ``hs_m`` = Hs avg (ensemble MEAN, not a
member and not the deterministic run), ``hs_spread_m`` = Hs spr, ``tp_s`` = Tp avg (the
mean of the members' peak periods), ``wind_speed_kmh`` = U10avg x 3.6; spreads of Tp and
U10 and the exceedance probabilities go to ``extra``. There are no directions. The rows
are statistics of the ensemble at the buoy and are used to validate the app's
ensemble-based range (docs/ml-evaluation.md section 12); they are never pooled with the
deterministic ``gfswave_bulletin`` rows (other source code, other family). The station id
is the catalog's ``bulletin_id`` (the same WMO-style id the GFS-Wave bulletins use); a
station without a file in the tar is counted, not an error.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import BuoyStation, PointForecast
from app.services.geo import haversine_km
from app.services.http import PermanentProviderError, ProviderError, request_with_retries
from app.services.observations.stations import bulletin_stations
from app.services.verification.point_forecasts import (
    _BULLETIN_ID_RE,
    _DAY_HOUR_RE,
    _next_valid_time,
    cycles_between,
)

logger = logging.getLogger(__name__)

SOURCE_CODE = "gefswave_station"
FAMILY = "ncep_gefs"
DEFAULT_BASE_URL = "https://noaa-gefs-pds.s3.amazonaws.com"
# First cycle whose station bulletins print the spread columns (see the module docstring).
FIRST_SPREAD_CYCLE = datetime(2026, 2, 26, 0, tzinfo=UTC)
MAX_TAR_BYTES = 20_000_000  # the 2026-10-11 00Z tar is 3.7 MB
COLUMNS = (
    "hs_avg",
    "hs_spr",
    "tp_avg",
    "tp_spr",
    "u10_avg",
    "u10_spr",
    "p_hs_gt_1m",
    "p_hs_gt_2m",
    "p_hs_gt_3m",
    "p_hs_gt_5_5m",
    "p_hs_gt_7m",
    "p_hs_gt_9m",
)
KMH_PER_MS = 3.6

_LOCATION_RE = re.compile(
    r"Location\s*:\s*(\S+)\s*\(\s*(-?\d+(?:\.\d+)?)([NS])\s+(-?\d+(?:\.\d+)?)[EW]?\s*\)"
)
_CYCLE_RE = re.compile(r"Cycle\s*:\s*(\d{8})\s+t(\d{2})z\s*UTC")
_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*$")


class GefsBulletinFormatError(PermanentProviderError):
    """Not a GEFS-Wave station bulletin with ensemble spreads in the expected layout."""


@dataclass(frozen=True)
class GefsRow:
    valid_time: datetime
    lead_hours: float
    values: dict[str, float]


@dataclass
class GefsBulletin:
    station_id: str
    latitude: float
    longitude: float
    cycle_time: datetime
    rows: list[GefsRow] = field(default_factory=list)
    malformed: int = 0


def parse_location(text: str) -> tuple[str, float, float]:
    m = _LOCATION_RE.search(text)
    if not m:
        raise GefsBulletinFormatError("bulletin has no 'Location : ID (lat lon)' header")
    lat = abs(float(m.group(2))) * (1 if m.group(3) == "N" else -1)
    lon = float(m.group(4))  # signed degrees east; the letter is not reliable
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 360.0):
        raise GefsBulletinFormatError(f"implausible position {lat}, {lon}")
    return m.group(1), lat, ((lon + 180.0) % 360.0) - 180.0


def parse_cycle(text: str) -> datetime:
    m = _CYCLE_RE.search(text)
    if not m:
        raise GefsBulletinFormatError("bulletin has no 'Cycle : YYYYMMDD tHHz UTC' header")
    day = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC)
    return day + timedelta(hours=int(m.group(2)))


def parse_gefs_bulletin(text: str, expected_cycle: datetime | None = None) -> GefsBulletin:
    """Parse one ``gefs.wave.{ID}.bull``; refuses files without the spread columns."""
    if "Global Wave Ensemble" not in text:
        raise GefsBulletinFormatError("not a GEFS-Wave (Global Wave Ensemble) bulletin")
    station_id, lat, lon = parse_location(text)
    cycle = parse_cycle(text)
    if expected_cycle is not None and cycle != expected_cycle:
        raise GefsBulletinFormatError(
            f"bulletin is for cycle {cycle:%Y%m%d %H}Z, not {expected_cycle:%Y%m%d %H}Z"
        )
    bulletin = GefsBulletin(station_id, lat, lon, cycle)
    previous = cycle
    short_rows = 0
    for line in text.splitlines():
        cells = line.strip().split("|")
        if len(cells) < 3 or cells[0] != "":
            continue
        dh = _DAY_HOUR_RE.match(cells[1])
        if not dh:
            continue  # header rows ("day", "hour")
        numbers = [c for c in cells[2:] if c.strip()]
        if len(numbers) != len(COLUMNS):
            short_rows += 1
            continue
        parsed = [_NUMBER_RE.match(c) for c in numbers]
        if not all(parsed):
            bulletin.malformed += 1
            continue
        valid = _next_valid_time(previous, int(dh.group(1)), int(dh.group(2)))
        previous = valid
        values = {name: float(p.group(1)) for name, p in zip(COLUMNS, parsed, strict=True) if p}
        bulletin.rows.append(GefsRow(valid, (valid - cycle).total_seconds() / 3600.0, values))
    if short_rows and not bulletin.rows:
        raise GefsBulletinFormatError(
            f"bulletin {station_id} {cycle:%Y%m%d%H}: {short_rows} rows without the "
            f"{len(COLUMNS)} columns of the header (averages-only layout of cycles before "
            f"{FIRST_SPREAD_CYCLE:%Y-%m-%d}); not stored"
        )
    bulletin.malformed += short_rows
    if not bulletin.rows:
        raise GefsBulletinFormatError(f"bulletin {station_id} has no data rows")
    return bulletin


def tar_url(base_url: str, cycle: datetime) -> str:
    hh = f"{cycle.hour:02d}"
    return f"{base_url.rstrip('/')}/gefs.{cycle:%Y%m%d}/{hh}/wave/station/gefs.wave.t{hh}z.bull_tar"


def member_name(station_id: str) -> str:
    if not _BULLETIN_ID_RE.fullmatch(station_id):
        raise ValueError(f"invalid bulletin id {station_id!r}")
    return f"gefs.wave.{station_id}.bull"


def read_members(data: bytes, names: Iterable[str]) -> dict[str, str]:
    """Text of the named regular files in a tar (read in memory; nothing is extracted).

    Only members whose name is exactly one of ``names`` are read, so the paths inside the
    archive never touch the file system.
    """
    wanted = set(names)
    out: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar:
            if member.name in wanted and member.isreg() and member.size < 1_000_000:
                fh = tar.extractfile(member)
                if fh is not None:
                    out[member.name] = fh.read().decode("ascii", errors="replace")
    return out


def point_forecast_rows(
    bulletin: GefsBulletin, station: BuoyStation, max_lead_hours: float
) -> list[dict[str, Any]]:
    distance = round(
        haversine_km(station.latitude, station.longitude, bulletin.latitude, bulletin.longitude), 2
    )
    rows = []
    for r in bulletin.rows:
        if r.lead_hours < 0 or r.lead_hours > max_lead_hours:
            continue
        v = r.values
        rows.append(
            {
                "station_id": station.id,
                "source_code": SOURCE_CODE,
                "family": FAMILY,
                "cycle_time": bulletin.cycle_time,
                "valid_time": r.valid_time,
                "lead_hours": r.lead_hours,
                "hs_m": v["hs_avg"],
                "tp_s": v["tp_avg"],
                "direction_deg": None,
                "swell1_hs_m": None,
                "swell1_tp_s": None,
                "swell1_direction_deg": None,
                "wind_speed_kmh": round(v["u10_avg"] * KMH_PER_MS, 2),
                "wind_direction_deg": None,
                "hs_spread_m": v["hs_spr"],
                "model_latitude": bulletin.latitude,
                "model_longitude": bulletin.longitude,
                "distance_km": distance,
                "extra": {
                    "bulletin_id": bulletin.station_id,
                    "hs_kind": "spectral_hm0",
                    "statistic": "ensemble mean; hs_spread_m = standard deviation of members",
                    "period_kind": "tp_ensemble_mean",
                    "tp_spread_s": v["tp_spr"],
                    "wind_spread_kmh": round(v["u10_spr"] * KMH_PER_MS, 2),
                    "p_hs_gt": {
                        "1.0": v["p_hs_gt_1m"],
                        "2.0": v["p_hs_gt_2m"],
                        "3.0": v["p_hs_gt_3m"],
                        "5.5": v["p_hs_gt_5_5m"],
                        "7.0": v["p_hs_gt_7m"],
                        "9.0": v["p_hs_gt_9m"],
                    },
                },
            }
        )
    return rows


def collect_range(
    db: Session,
    start: datetime,
    end: datetime,
    cycles: Iterable[int] = (0,),
    stations: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    base_url: str = DEFAULT_BASE_URL,
    backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    """Archive GEFS-Wave station statistics for every (station, cycle) in range.

    One tar download per cycle serves every station. Idempotent: cycles already stored
    for all requested stations are not downloaded again, and inserts ignore the unique
    constraint. Cycles before :data:`FIRST_SPREAD_CYCLE` are skipped (no spreads).
    """
    settings = settings or get_settings()
    station_rows = bulletin_stations(db, stations)
    targets = [(s, str(s.metadata_["bulletin_id"])) for s in station_rows]
    cycle_list = [c for c in cycles_between(start, end, cycles) if c >= FIRST_SPREAD_CYCLE]
    result: dict[str, Any] = {
        "source": SOURCE_CODE,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cycles": len(cycle_list),
        "stations": [s.code for s in station_rows],
        "already_stored": 0,
        "tars_fetched": 0,
        "bytes": 0,
        "missing": 0,
        "not_in_tar": 0,
        "errors": 0,
        "rows_inserted": 0,
        "malformed_rows": 0,
        "error_samples": [],
    }
    if not targets or not cycle_list:
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
    own_client = client is None
    http = client or httpx.Client(
        timeout=settings.forecast_http_timeout_seconds,
        headers={"User-Agent": "SwellTravelAgent/1.0 (verification)"},
    )
    max_lead = settings.forecast_days * 24.0
    per_station: dict[str, int] = {}
    try:
        for cycle in cycle_list:
            todo = [(s, bid) for s, bid in targets if (s.id, cycle) not in stored]
            result["already_stored"] += len(targets) - len(todo)
            if not todo:
                continue
            _collect_cycle(
                http, base_url, cycle, todo, db, max_lead, result, per_station, backoff_seconds
            )
    finally:
        if own_client:
            http.close()
    result["rows_by_station"] = per_station
    result["error_samples"] = result["error_samples"][:10]
    return result


def _collect_cycle(
    http: httpx.Client,
    base_url: str,
    cycle: datetime,
    todo: list[tuple[BuoyStation, str]],
    db: Session,
    max_lead: float,
    result: dict[str, Any],
    per_station: dict[str, int],
    backoff_seconds: float,
) -> None:
    url = tar_url(base_url, cycle)
    try:
        response = request_with_retries(
            http, "GET", url, provider=SOURCE_CODE, attempts=4, backoff_seconds=backoff_seconds
        )
    except ProviderError as exc:
        result["errors"] += 1
        result["error_samples"].append(f"{cycle:%Y%m%d%H}: {exc}")
        return
    if response.status_code == 404:
        result["missing"] += 1
        return
    if response.status_code != 200:
        result["errors"] += 1
        result["error_samples"].append(f"{cycle:%Y%m%d%H}: HTTP {response.status_code}")
        return
    data = response.content
    if len(data) > MAX_TAR_BYTES:
        result["errors"] += 1
        result["error_samples"].append(f"{cycle:%Y%m%d%H}: tar of {len(data)} bytes refused")
        return
    result["tars_fetched"] += 1
    result["bytes"] += len(data)
    names = {member_name(bid): (s, bid) for s, bid in todo}
    try:
        texts = read_members(data, names)
    except tarfile.TarError as exc:
        result["errors"] += 1
        result["error_samples"].append(f"{cycle:%Y%m%d%H}: unreadable tar ({exc})")
        return
    for name, (station, _bid) in names.items():
        text = texts.get(name)
        if text is None:
            result["not_in_tar"] += 1
            continue
        try:
            bulletin = parse_gefs_bulletin(text, expected_cycle=cycle)
        except GefsBulletinFormatError as exc:
            result["errors"] += 1
            result["error_samples"].append(f"{station.code} {cycle:%Y%m%d%H}: {exc}")
            continue
        result["malformed_rows"] += bulletin.malformed
        rows = point_forecast_rows(bulletin, station, max_lead)
        if not rows:
            continue
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
