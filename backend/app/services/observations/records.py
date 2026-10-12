"""Normalised observation records, value validation and QC precedence.

Conventions (shared by every provider adapter):

* times are UTC, rounded to the nearest minute (wave parameters describe a 17-30 minute
  burst, and two copies of one AODN record differ by a few seconds: 00:00:07.6 vs 00:00);
* heights in metres with an explicit ``hs_kind``: ``spectral_hm0`` (4·sqrt(m0)) or
  ``h_third`` (time-domain mean of the highest third). They differ by several per cent, so
  verification must not mix them silently;
* periods in seconds: ``tp_s`` is always the spectral peak period; ``tm_s`` carries a mean
  period whose definition is in ``tm_kind`` ("tm01", "tm02", "tz" time-domain zero
  up-crossing, "apd" an NDBC average period whose definition is not known for the
  station; see app.services.observations.ndbc);
* directions are nautical degrees the waves / wind come FROM, in [0, 360);
* wind speed in km/h (m/s x 3.6).

QC precedence (``QC_RANK``): ``good`` (delayed-mode, expert-reviewed) > ``probably_good``
(passed the provider's automated checks, or good values with a questionable position) >
``bad`` > ``not_qc``. A ``bad`` record is a *rejection marker*: delayed-mode QC said the
measurement at that time is wrong, so its values are never stored (they are all NULL) but
the row stops a realtime copy of the same measurement from standing. ``not_qc`` can never
replace anything else.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from typing import Any

QC_RANK = {"good": 4, "probably_good": 3, "bad": 2, "not_qc": 1}

# Two records of one station closer than this are the same measurement burst (AODN
# realtime at :00/:30 vs delayed-mode at :11/:41, Apollo Bay realtime at :55 vs :00).
# Must stay below half the shortest sampling interval in use (30 min).
DUPLICATE_WINDOW = timedelta(minutes=15)

# Physical plausibility limits. Heights and peak periods also respect the CHECK
# constraints of buoy_observations (hs_m in [0, 30), tp_s in (0, 40)). An Hs of exactly 0
# at an open-ocean buoy is a fill value, so heights must be > 0.
HEIGHT_RANGE = (0.0, 30.0)
PERIOD_RANGE = (0.0, 40.0)
WIND_KMH_RANGE = (0.0, 400.0)

MEASURED_FIELDS = (
    "hs_m",
    "tp_s",
    "tm_s",
    "peak_direction_deg",
    "mean_direction_deg",
    "swell_hs_m",
    "swell_tp_s",
    "swell_direction_deg",
    "windsea_hs_m",
    "wind_speed_kmh",
    "wind_direction_deg",
)
HEIGHT_FIELDS = ("hs_m", "swell_hs_m", "windsea_hs_m")
PERIOD_FIELDS = ("tp_s", "tm_s", "swell_tp_s")
DIRECTION_FIELDS = (
    "peak_direction_deg",
    "mean_direction_deg",
    "swell_direction_deg",
    "wind_direction_deg",
)


@dataclass
class ObservationRecord:
    """One buoy measurement, already in project units and conventions."""

    observed_at: datetime
    qc_flag: str
    dataset: str
    hs_m: float | None = None
    hs_kind: str = "unknown"
    tp_s: float | None = None
    tm_s: float | None = None
    tm_kind: str | None = None
    peak_direction_deg: float | None = None
    mean_direction_deg: float | None = None
    swell_hs_m: float | None = None
    swell_tp_s: float | None = None
    swell_direction_deg: float | None = None
    windsea_hs_m: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    # Source file, used only to break ties deterministically (not stored).
    source: str = field(default="", compare=False)

    @property
    def rank(self) -> int:
        return QC_RANK.get(self.qc_flag, 0)

    def value_count(self) -> int:
        return sum(getattr(self, f) is not None for f in MEASURED_FIELDS)

    def has_wave_data(self) -> bool:
        return self.hs_m is not None or self.tp_s is not None

    def as_row(self, station_id: int) -> dict[str, Any]:
        row = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "source"}
        row["station_id"] = station_id
        return row


@dataclass
class ParseStats:
    """What a parser read, kept and dropped (reported, never silently discarded)."""

    rows_read: int = 0
    rows_kept: int = 0
    # Records the provider's QC rejected (delayed mode: stored as 'bad' markers).
    qc_rejected: int = 0
    # Rows without any usable wave value (missing, fill values or out of range).
    no_wave_data: int = 0
    # Field-level values dropped because they were outside physical / CHECK ranges.
    invalid_values: Counter[str] = field(default_factory=Counter)
    # Field-level values the provider's per-value QC flagged (dropped).
    flagged_values: Counter[str] = field(default_factory=Counter)
    # Rows that repeated a measurement already read (merged into it, not stored twice).
    duplicates_merged: int = 0

    def merge(self, other: ParseStats) -> None:
        self.rows_read += other.rows_read
        self.rows_kept += other.rows_kept
        self.qc_rejected += other.qc_rejected
        self.no_wave_data += other.no_wave_data
        self.invalid_values.update(other.invalid_values)
        self.flagged_values.update(other.flagged_values)
        self.duplicates_merged += other.duplicates_merged

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "rows_kept": self.rows_kept,
            "qc_rejected": self.qc_rejected,
            "no_wave_data": self.no_wave_data,
            "invalid_values": dict(self.invalid_values),
            "flagged_values": dict(self.flagged_values),
            "duplicates_merged": self.duplicates_merged,
        }


@dataclass(frozen=True)
class StationRef:
    """What an adapter needs to know about a catalog station."""

    code: str
    provider_id: str
    latitude: float
    longitude: float
    metadata: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass
class StationFetch:
    """One station's result from a provider adapter (partial results are normal)."""

    records: list[ObservationRecord] = field(default_factory=list)
    stats: ParseStats = field(default_factory=ParseStats)
    files: int = 0
    errors: list[str] = field(default_factory=list)
    # Latest position / depth the data itself reported (lat, lon, depth, time, dataset).
    last_position: dict[str, Any] | None = None
    # Catalog fields the provider's own metadata says should change (e.g. NDBC positions).
    station_updates: dict[str, Any] = field(default_factory=dict)

    def note_position(self, position: dict[str, Any] | None) -> None:
        if position is None:
            return
        if self.last_position is None or position["observed_at"] >= self.last_position.get(
            "observed_at", ""
        ):
            self.last_position = position


@dataclass
class ProviderFetch:
    stations: dict[str, StationFetch] = field(default_factory=dict)
    requests: int = 0
    warnings: list[str] = field(default_factory=list)
    # Response bytes as transferred (compressed files counted compressed); 0 when the
    # adapter does not count them.
    bytes_downloaded: int = 0

    def station(self, code: str) -> StationFetch:
        return self.stations.setdefault(code, StationFetch())


def round_to_minute(t: datetime) -> datetime:
    """UTC datetime rounded to the nearest minute (naive input is taken as UTC)."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    t = t.astimezone(UTC)
    base = t.replace(second=0, microsecond=0)
    if t - base >= timedelta(seconds=30):
        base += timedelta(minutes=1)
    return base


def from_epoch_ns(ns: int) -> datetime:
    return round_to_minute(datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000))


def to_float(value: Any) -> float | None:
    """Float or None for missing, empty or NaN values."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _in_open_range(value: float, lo: float, hi: float) -> bool:
    return lo < value < hi


def validate_record(record: ObservationRecord, stats: ParseStats) -> ObservationRecord | None:
    """Drop (and count) implausible values; None if no wave value is left.

    Directions are normalised to [0, 360) (360 -> 0); anything outside [0, 360] is invalid.
    A 'bad' rejection marker carries no values and is always kept.
    """
    if record.qc_flag == "bad":
        for name in MEASURED_FIELDS:
            setattr(record, name, None)
        record.hs_kind, record.tm_kind = "unknown", None
        return record
    for name in HEIGHT_FIELDS:
        v = getattr(record, name)
        if v is not None and not _in_open_range(v, *HEIGHT_RANGE):
            stats.invalid_values[name] += 1
            setattr(record, name, None)
    for name in PERIOD_FIELDS:
        v = getattr(record, name)
        if v is not None and not _in_open_range(v, *PERIOD_RANGE):
            stats.invalid_values[name] += 1
            setattr(record, name, None)
    for name in DIRECTION_FIELDS:
        v = getattr(record, name)
        if v is None:
            continue
        if not 0.0 <= v <= 360.0:
            stats.invalid_values[name] += 1
            setattr(record, name, None)
        else:
            setattr(record, name, v % 360.0)
    w = record.wind_speed_kmh
    if w is not None and not WIND_KMH_RANGE[0] <= w < WIND_KMH_RANGE[1]:
        stats.invalid_values["wind_speed_kmh"] += 1
        record.wind_speed_kmh = None
    if record.hs_m is None:
        record.hs_kind = "unknown"
    if record.tm_s is None:
        record.tm_kind = None
    if not record.has_wave_data():
        stats.no_wave_data += 1
        return None
    return record


def _better(a: ObservationRecord, b: ObservationRecord) -> ObservationRecord:
    """Pick between two records of the same time: QC rank, then completeness, then source."""
    return max((a, b), key=lambda r: (r.rank, r.value_count(), r.source))


def resolve_batch(records: list[ObservationRecord]) -> tuple[list[ObservationRecord], int]:
    """Collapse one station's incoming records: exact duplicates keep the better record and
    a record within ``DUPLICATE_WINDOW`` of a higher-ranked one is superseded.

    Returns (records sorted by time, number of records dropped).
    """
    by_time: dict[datetime, ObservationRecord] = {}
    for r in records:
        cur = by_time.get(r.observed_at)
        by_time[r.observed_at] = r if cur is None else _better(cur, r)
    ordered = sorted(by_time.values(), key=lambda r: r.observed_at)
    kept: list[ObservationRecord] = []
    for i, r in enumerate(ordered):
        superseded = False
        # Neighbours within the window on either side (the list is time-sorted).
        for j in range(i - 1, -1, -1):
            if r.observed_at - ordered[j].observed_at >= DUPLICATE_WINDOW:
                break
            if ordered[j].rank > r.rank:
                superseded = True
                break
        if not superseded:
            for j in range(i + 1, len(ordered)):
                if ordered[j].observed_at - r.observed_at >= DUPLICATE_WINDOW:
                    break
                if ordered[j].rank > r.rank:
                    superseded = True
                    break
        if not superseded:
            kept.append(r)
    return kept, len(records) - len(kept)
