"""Australian wave buoys from the AODN cloud-optimised Parquet datasets (public S3, CC BY 4.0).

Datasets (``s3://aodn-cloud-optimised``, verified 2026-10-10):

* ``wave_buoy_delayed_qc.parquet/``  delayed-mode, quality controlled (National Wave
  Archive). Coverage ends months to years before today (Cape Naturaliste 02: 2004-04 to
  2021-12; Tweed Heads: 1995-01 to 2022-12; Apollo Bay: 2019-10 to 2026-06).
* ``wave_buoy_realtime_nonqc.parquet/``  near-real-time, *not* quality controlled
  (most sites from 2024-01).

Both are Hive-partitioned ``site_name=<escaped>/timestamp=<epoch s>/polygon=<wkb hex>/``.
Site names are percent-escaped in the key (a literal ``%20``), so HTTPS object URLs
encode it again as ``%2520`` (research notes, "Forecast validation methodology", AODN).
The ``timestamp`` partition is the start of a calendar month (observed for every site
listed); to be safe against coarser partitions we read every partition that starts up
to 32 days before the requested window and filter rows by time.

Variables used (attributes read from ``_common_metadata``):

* ``WSSH`` spectral significant wave height -> ``hs_kind = spectral_hm0``; ``WHTH``
  (time-domain H1/3) only when WSSH is missing -> ``h_third``. The Queensland (DES-QLD)
  and WA (DOT-WA) realtime feeds publish WHTH only.
* ``WPPE`` spectral peak period (Tp). ``WPPE == 0`` is a fill value (every other
  half-hour in the Tweed Offshore realtime feed); the peak direction ``WPDI`` of such a
  row is then also undefined and dropped.
* ``WPDI`` peak direction and ``SSWMD`` mean direction. Their CF standard names are
  ``sea_surface_wave_from_direction[...]`` with ``reference_datum = true north``, i.e.
  nautical FROM; if a future file declared a ``to_direction`` we add 180 degrees.
* ``WPFM`` (Tm01, first moment), else ``WPSM`` (Tm02), else ``WPMH`` (time-domain mean
  zero up-crossing period, ``tm_kind = tz``).
* QC: ``WAVE_quality_control`` uses the IOC/UNESCO 2013 scheme declared in the data,
  ``flag_values [1, 2, 3, 4, 9]`` = ``good not_evaluated questionable bad missing``. Note
  that 2 means *not evaluated* here (not "probably good"). Delayed mode maps 1 -> good,
  2 -> not_qc, 3/4 -> bad (rejection marker), 9 -> skipped. ``WATCH_CIRCLE_flag`` (buoy
  position QC) 4 -> bad, 3 -> good downgraded to probably_good. Realtime rows are always
  ``not_qc`` (the dataset is declared non-QC); realtime rows flagged 3/4/9 are dropped.

The datasets contain no wind. Licence CC BY 4.0; acknowledgement text from the source
NetCDF ``acknowledgement`` attribute (see docs/observations.md).
"""

from __future__ import annotations

import ast
import io
import logging
import re
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

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
    from_epoch_ns,
    to_float,
    validate_record,
)
from app.services.observations.s3 import list_objects, object_url

logger = logging.getLogger(__name__)

PROVIDER = "aodn"
DELAYED = "wave_buoy_delayed_qc"
REALTIME = "wave_buoy_realtime_nonqc"
DATASETS = (DELAYED, REALTIME)
PARTITION_LOOKBACK = timedelta(days=32)
_TIMESTAMP_RE = re.compile(r"/timestamp=(-?\d+)/")

COLUMNS = (
    "TIME",
    "WSSH",
    "WHTH",
    "WPPE",
    "WPFM",
    "WPSM",
    "WPMH",
    "WPDI",
    "SSWMD",
    "WAVE_quality_control",
    "WATCH_CIRCLE_flag",
    "LATITUDE",
    "LONGITUDE",
    "water_depth",
    "wmo_id",
    "filename",
)

# Flag meaning -> stored qc_flag (None = no measurement: skip the row).
MEANING_TO_QC: dict[str, str | None] = {
    "good": "good",
    "probably_good": "probably_good",
    "not_evaluated": "not_qc",
    "no_qc_performed": "not_qc",
    "unknown": "not_qc",
    "questionable": "bad",
    "suspect": "bad",
    "probably_bad": "bad",
    "bad": "bad",
    "missing": None,
}
# Declared in both datasets' _common_metadata (checked 2026-10-10); used if it cannot be read.
DEFAULT_FLAGS = {1: "good", 2: "not_evaluated", 3: "questionable", 4: "bad", 9: "missing"}


@dataclass
class Conventions:
    """Dataset attributes that change how values are read (from ``_common_metadata``)."""

    wave_flags: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_FLAGS))
    position_flags: dict[int, str] = field(default_factory=lambda: dict(DEFAULT_FLAGS))
    # Variable -> "from" or "to".
    direction: dict[str, str] = field(default_factory=lambda: {"WPDI": "from", "SSWMD": "from"})
    source: str = "defaults"


def partition_prefix(dataset: str, site_name: str) -> str:
    """Key prefix of one site, with the site name escaped the way the dataset stores it."""
    return f"{dataset}.parquet/site_name={quote(site_name, safe='')}/"


def partition_start(key: str) -> datetime | None:
    m = _TIMESTAMP_RE.search(key)
    return datetime.fromtimestamp(int(m.group(1)), tz=UTC) if m else None


def select_partitions(keys: Iterable[str], start: datetime, end: datetime) -> list[str]:
    """Data files whose partition may hold rows in [start, end]."""
    out = []
    for key in keys:
        if not key.endswith(".parquet"):
            continue
        ts = partition_start(key)
        if ts is not None and start - PARTITION_LOOKBACK < ts <= end:
            out.append(key)
    return sorted(out)


def _flag_table(meta: dict[bytes, bytes]) -> dict[int, str] | None:
    values = meta.get(b"flag_values")
    meanings = meta.get(b"flag_meanings")
    if not values or not meanings:
        return None
    try:
        parsed = ast.literal_eval(values.decode())
        codes = [int(v) for v in (parsed if isinstance(parsed, list | tuple) else [parsed])]
    except (ValueError, SyntaxError):
        return None
    names = meanings.decode().split()
    if len(codes) != len(names):
        return None
    return dict(zip(codes, names, strict=True))


def conventions_from_schema(schema: pa.Schema) -> Conventions:
    """Read QC flag meanings and direction conventions from a dataset schema."""
    conv = Conventions(source="_common_metadata")
    names = set(schema.names)
    for column, attr in (
        ("WAVE_quality_control", "wave_flags"),
        ("WATCH_CIRCLE_flag", "position_flags"),
    ):
        if column in names:
            table = _flag_table(schema.field(column).metadata or {})
            if table:
                setattr(conv, attr, table)
    for column in ("WPDI", "SSWMD"):
        if column not in names:
            continue
        std = (schema.field(column).metadata or {}).get(b"standard_name", b"").decode()
        if "to_direction" in std:
            conv.direction[column] = "to"
        elif "from_direction" in std:
            conv.direction[column] = "from"
    return conv


def _column(table: pa.Table, name: str) -> list[Any]:
    if name not in table.column_names:
        return [None] * table.num_rows
    col = table.column(name)
    if name == "TIME":
        if not pa.types.is_timestamp(col.type):
            raise PermanentProviderError(f"aodn: TIME column has type {col.type}")
        col = col.cast(pa.timestamp("ns")).cast(pa.int64())
    return list(col.to_pylist())


def _height(v: float | None) -> float | None:
    return v if v is not None and 0.0 < v < 30.0 else None


def _direction(v: float | None, convention: str) -> float | None:
    if v is None:
        return None
    return (v + 180.0) % 360.0 if convention == "to" else v


def parse_parquet(
    content: bytes,
    *,
    dataset: str,
    start: datetime,
    end: datetime,
    conventions: Conventions,
    stats: ParseStats,
) -> tuple[list[ObservationRecord], dict[str, Any] | None]:
    """Records in [start, end] from one AODN partition file, plus its latest position."""
    try:
        pf = pq.ParquetFile(io.BytesIO(content))
        wanted = [c for c in COLUMNS if c in pf.schema_arrow.names]
        table = pf.read(columns=wanted)
    except (pa.ArrowException, OSError) as exc:
        raise PermanentProviderError(f"aodn: unreadable parquet file: {exc}") from exc
    cols = {name: _column(table, name) for name in COLUMNS}
    delayed = dataset == DELAYED
    records: list[ObservationRecord] = []
    position: dict[str, Any] | None = None
    for i, ns in enumerate(cols["TIME"]):
        if ns is None:
            continue
        t = from_epoch_ns(int(ns))
        if not start <= t <= end:
            continue
        stats.rows_read += 1
        flag = to_float(cols["WAVE_quality_control"][i])
        meaning = conventions.wave_flags.get(int(flag)) if flag is not None else None
        mapped = MEANING_TO_QC.get(meaning, "not_qc") if meaning else "not_qc"
        if meaning and MEANING_TO_QC.get(meaning, "not_qc") is None:
            stats.no_wave_data += 1
            continue
        source = str(cols["filename"][i] or "")
        if delayed:
            qc = mapped or "not_qc"
            wc = to_float(cols["WATCH_CIRCLE_flag"][i])
            wc_meaning = conventions.position_flags.get(int(wc)) if wc is not None else None
            if wc_meaning in ("bad", "probably_bad"):
                qc = "bad"  # buoy off station: the sea state was measured elsewhere
            elif wc_meaning in ("questionable", "suspect") and qc == "good":
                qc = "probably_good"
        else:
            if mapped == "bad":
                stats.qc_rejected += 1
                continue
            qc = "not_qc"
        if qc == "bad":
            stats.qc_rejected += 1
            records.append(ObservationRecord(t, "bad", dataset, source=source))
            continue
        rec = _values(cols, i, t, qc, dataset, source, conventions, stats)
        valid = validate_record(rec, stats)
        if valid is None:
            continue
        records.append(valid)
        lat, lon = to_float(cols["LATITUDE"][i]), to_float(cols["LONGITUDE"][i])
        if lat is not None and lon is not None:
            depth = to_float(cols["water_depth"][i])
            position = {
                "latitude": lat,
                "longitude": lon,
                "water_depth_m": depth,
                "observed_at": t.isoformat(),
                "dataset": dataset,
                "wmo_id": cols["wmo_id"][i] or None,
            }
    stats.rows_kept += len(records)
    return records, position


def _values(
    cols: dict[str, list[Any]],
    i: int,
    t: datetime,
    qc: str,
    dataset: str,
    source: str,
    conventions: Conventions,
    stats: ParseStats,
) -> ObservationRecord:
    rec = ObservationRecord(t, qc, dataset, source=source)
    wssh, whth = to_float(cols["WSSH"][i]), to_float(cols["WHTH"][i])
    if wssh is not None and _height(wssh) is None:
        stats.invalid_values["WSSH"] += 1
    if _height(wssh) is not None:
        rec.hs_m, rec.hs_kind = wssh, "spectral_hm0"
    elif _height(whth) is not None:
        rec.hs_m, rec.hs_kind = whth, "h_third"
    elif whth is not None:
        stats.invalid_values["WHTH"] += 1
    tp = to_float(cols["WPPE"][i])
    pdir = to_float(cols["WPDI"][i])
    if tp is not None and tp > 0.0:
        rec.tp_s = tp
        rec.peak_direction_deg = _direction(pdir, conventions.direction.get("WPDI", "from"))
    else:
        if tp is not None:
            stats.invalid_values["WPPE"] += 1
        if pdir is not None:
            stats.invalid_values["WPDI"] += 1
    rec.mean_direction_deg = _direction(
        to_float(cols["SSWMD"][i]), conventions.direction.get("SSWMD", "from")
    )
    for column, kind in (("WPFM", "tm01"), ("WPSM", "tm02"), ("WPMH", "tz")):
        tm = to_float(cols[column][i])
        if tm is not None and tm > 0.0:
            rec.tm_s, rec.tm_kind = tm, kind
            break
    return rec


class AodnProvider:
    """Fetches the realtime and delayed-mode records of catalog stations.

    ``datasets`` restricts which of :data:`DATASETS` are read (default both). A backfill
    that must contain quality-controlled data only (e.g. a verification backtest) passes
    ``(DELAYED,)``: otherwise the realtime rows of the same window (``not_qc``) fill the
    gaps that delayed-mode QC left.
    """

    name = PROVIDER

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        backoff_seconds: float = 1.0,
        datasets: Iterable[str] | None = None,
    ) -> None:
        chosen = tuple(datasets) if datasets is not None else DATASETS
        unknown = sorted(set(chosen) - set(DATASETS))
        if unknown or not chosen:
            raise ValueError(f"aodn: unknown datasets {unknown}; choose from {list(DATASETS)}")
        self.datasets = chosen
        self.settings = settings or get_settings()
        self.base_url = self.settings.aodn_base_url.rstrip("/")
        self.client = client or httpx.Client(
            timeout=self.settings.forecast_http_timeout_seconds,
            headers={"User-Agent": "SwellTravelAgent/1.0 (observations)"},
        )
        self.backoff_seconds = backoff_seconds
        self.workers = min(8, self.settings.grib_download_workers)
        self._conventions: dict[str, Conventions] = {}
        self.requests = 0
        # _get runs in pool threads; += on an attribute is not atomic.
        self._count_lock = threading.Lock()

    def _get(self, url: str) -> bytes:
        with self._count_lock:
            self.requests += 1
        response = request_with_retries(
            self.client, "GET", url, provider=PROVIDER, backoff_seconds=self.backoff_seconds
        )
        if response.status_code != 200:
            raise PermanentProviderError(f"aodn: HTTP {response.status_code} for {url}")
        return response.content

    def conventions(self, dataset: str, warnings: list[str]) -> Conventions:
        if dataset not in self._conventions:
            try:
                raw = self._get(object_url(self.base_url, f"{dataset}.parquet/_common_metadata"))
                schema = pq.read_schema(io.BytesIO(raw))
                self._conventions[dataset] = conventions_from_schema(schema)
            except (ProviderError, pa.ArrowException, OSError) as exc:
                warnings.append(f"{dataset}: _common_metadata unreadable ({exc}); using defaults")
                self._conventions[dataset] = Conventions()
        return self._conventions[dataset]

    def _site_keys(self, dataset: str, site: str, start: datetime, end: datetime) -> list[str]:
        listing = list_objects(
            self.client,
            self.base_url,
            partition_prefix(dataset, site),
            provider=PROVIDER,
            backoff_seconds=self.backoff_seconds,
        )
        self.requests += listing.requests
        return select_partitions((o.key for o in listing.objects), start, end)

    def fetch(self, stations: list[StationRef], start: datetime, end: datetime) -> ProviderFetch:
        result = ProviderFetch()
        transient_failures = 0
        for station in stations:
            out = result.station(station.code)
            sites = station.metadata.get("aodn") or {}
            jobs: list[tuple[str, str]] = []
            for dataset, names in (
                (DELAYED, sites.get("delayed_sites", [])),
                (REALTIME, sites.get("realtime_sites", [])),
            ):
                if dataset not in self.datasets:
                    continue
                for site in names:
                    try:
                        jobs += [(dataset, k) for k in self._site_keys(dataset, site, start, end)]
                    except ProviderError as exc:
                        transient_failures += int(exc.transient)
                        out.errors.append(f"{dataset}/{site}: listing failed: {exc}")
            if not jobs:
                continue

            def download(job: tuple[str, str]) -> tuple[str, str, bytes | Exception]:
                dataset, key = job
                try:
                    return dataset, key, self._get(object_url(self.base_url, key))
                except ProviderError as exc:
                    return dataset, key, exc

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                downloaded = list(pool.map(download, jobs))
            for dataset, key, payload in downloaded:
                if isinstance(payload, Exception):
                    transient_failures += int(getattr(payload, "transient", False))
                    out.errors.append(f"{key.rsplit('/', 1)[-1]}: {payload}")
                    continue
                conv = self.conventions(dataset, result.warnings)
                try:
                    records, position = parse_parquet(
                        payload,
                        dataset=dataset,
                        start=start,
                        end=end,
                        conventions=conv,
                        stats=out.stats,
                    )
                except ProviderError as exc:
                    out.errors.append(f"{key.rsplit('/', 1)[-1]}: {exc}")
                    continue
                out.files += 1
                out.records.extend(records)
                out.note_position(position)
        result.requests = self.requests
        if stations and transient_failures and not any(s.files for s in result.stations.values()):
            raise TransientProviderError(
                f"aodn: no file could be read ({transient_failures} transient failures)"
            )
        return result
