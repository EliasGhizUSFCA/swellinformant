"""Pair archived point forecasts with buoy observations at the same station.

Each ``point_forecasts`` row (one station, source, cycle and valid time) is matched with
the observation of the same station nearest to its valid time, within ``tolerance``
(default 30 min), and the pair is written to ``prediction_validation``. Rules:

* Observations flagged ``qc_flag = 'bad'`` are never used. The nearest observation is
  chosen per variable among observations that actually report that variable.
* Like with like (research notes, validation section): model Hs is a spectral Hm0.
  Pairs against a spectral Hm0 observation get ``observed_kind = 'spectral_hm0'``;
  pairs against a time-domain H1/3 (``'h_third'``) or an undefined height
  (``'unknown'``) are kept with that kind so the metrics can report them separately.
  They are never pooled with the Hm0 pairs.
* ``tp`` pairs only when both sides are peak periods. ``buoy_observations.tp_s`` is the
  peak period by schema (mean periods live in ``tm_s``). The forecast's period kind comes
  from ``point_forecasts.extra['period_kind']`` or, failing that, from
  :data:`SOURCE_CONVENTIONS` (GFS-Wave/GEFS-Wave PERPW is Tp: ww3_grib writes 1/fp into
  PERPW, research notes, wave products section). Unknown kinds are skipped and counted.
* ``dir`` pairs compare the forecast direction with the observed direction of the same
  kind (``extra['direction_kind']`` = 'peak' or 'mean', or :data:`SOURCE_CONVENTIONS`).
  The error is the signed circular difference in (-180, 180] (both nautical FROM).

Pairing is incremental and idempotent: by default only forecasts whose valid time lies
in the look-back window and that have no raw ``hs`` pair yet are processed (late
observations are picked up on the next run); ``full=True`` reprocesses every forecast.
For each processed forecast its raw pairs are deleted and rewritten in one
transaction, so a re-run yields the same rows and an observation that was later flagged
bad drops out.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import and_, delete, exists, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import BuoyObservation, PointForecast, PredictionValidation
from app.services.forecasts.registry import ECMWF_IFS_CODE, GEFS_WAVE_CODE, NOAA_GFS_CODE
from app.services.verification.metrics import circular_difference

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE = timedelta(minutes=30)
DEFAULT_LOOKBACK = timedelta(days=14)
# Persistence: the latest observation at or before the cycle time, at most this old.
PERSISTENCE_MAX_AGE = timedelta(hours=1)
RAW_MODEL_VERSION = "raw"
PAIR_VARIABLES = ("hs", "tp", "dir")
WRITE_CHUNK = 2000
KEY_CHUNK = 500

# Documented conventions per source-code prefix: (period kind of tp_s, kind of
# direction_deg). Used only when the point forecast's ``extra`` does not say.
# * NOAA GFS-Wave / GEFS-Wave GRIB: PERPW = peak period, DIRPW = peak direction.
# * GEFSv12 wave reforecast station tables: fp (peak frequency) and th1p (direction at
#   the spectral peak) (research notes, validation section).
# * ECMWF open data: mwd is a mean direction; the period may be pp1d (Tp) or mwp
#   (Tm-1,0) depending on the ingester, so it must be declared in ``extra``.
SOURCE_CONVENTIONS: dict[str, tuple[str | None, str | None]] = {
    NOAA_GFS_CODE: ("tp", "peak"),
    GEFS_WAVE_CODE: ("tp", "peak"),
    "gefs_wave": ("tp", "peak"),
    "gefsv12_reforecast": ("tp", "peak"),
    ECMWF_IFS_CODE: (None, "mean"),
    "ecmwf_ifs_wave": (None, "mean"),
}


def forecast_conventions(
    source_code: str, extra: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """(period kind, direction kind) of a point forecast: ``extra`` first, then prefixes."""
    extra = extra or {}
    period = extra.get("period_kind")
    direction = extra.get("direction_kind")
    if period is None or direction is None:
        default: tuple[str | None, str | None] = (None, None)
        for prefix in sorted(SOURCE_CONVENTIONS, key=len, reverse=True):
            if source_code.startswith(prefix):
                default = SOURCE_CONVENTIONS[prefix]
                break
        period = period if period is not None else default[0]
        direction = direction if direction is not None else default[1]
    period_s = str(period).lower() if period is not None else None
    direction_s = str(direction).lower() if direction is not None else None
    return period_s, direction_s if direction_s in ("peak", "mean") else None


# ------------------------------------------------------------------------------ frames
FORECAST_COLUMNS = (
    "station_id",
    "source_code",
    "family",
    "cycle_time",
    "valid_time",
    "lead_hours",
    "hs_m",
    "tp_s",
    "direction_deg",
    "swell1_hs_m",
    "swell1_tp_s",
    "swell1_direction_deg",
    "wind_speed_kmh",
    "wind_direction_deg",
    "hs_spread_m",
    "extra",
)
OBSERVATION_COLUMNS = (
    "station_id",
    "observed_at",
    "hs_m",
    "hs_kind",
    "tp_s",
    "peak_direction_deg",
    "mean_direction_deg",
    "qc_flag",
)


TIME_UNIT = "us"


def utc_times(values: Any) -> pd.Series:
    """Timezone-aware UTC timestamps at one fixed resolution (microseconds).

    pandas infers the resolution from the data (whole seconds -> ``[s]``), and
    ``merge_asof`` refuses keys of different resolutions, so every time column used for
    matching goes through here.
    """
    converted = pd.to_datetime(values, utc=True)
    if isinstance(converted, pd.Series):
        return converted.dt.as_unit(TIME_UNIT)
    return pd.Series(pd.DatetimeIndex(converted).as_unit(TIME_UNIT))


def frame(rows: Iterable[Any], columns: Sequence[str]) -> pd.DataFrame:
    """DataFrame from SQLAlchemy rows with fixed columns (also when there are none)."""
    df = pd.DataFrame.from_records([tuple(r) for r in rows], columns=list(columns))
    for col in ("cycle_time", "valid_time", "observed_at"):
        if col in df.columns:
            df[col] = utc_times(df[col])
    return df


def load_point_forecasts(
    db: Session,
    *,
    station_ids: Sequence[int] | None = None,
    sources: Sequence[str] | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    cycle_from: datetime | None = None,
    cycle_to: datetime | None = None,
) -> pd.DataFrame:
    """Point forecasts as a DataFrame with :data:`FORECAST_COLUMNS`."""
    stmt = select(*(getattr(PointForecast, c) for c in FORECAST_COLUMNS))
    if station_ids is not None:
        stmt = stmt.where(PointForecast.station_id.in_(list(station_ids)))
    if sources:
        stmt = stmt.where(PointForecast.source_code.in_(list(sources)))
    if valid_from is not None:
        stmt = stmt.where(PointForecast.valid_time >= valid_from)
    if valid_to is not None:
        stmt = stmt.where(PointForecast.valid_time <= valid_to)
    if cycle_from is not None:
        stmt = stmt.where(PointForecast.cycle_time >= cycle_from)
    if cycle_to is not None:
        stmt = stmt.where(PointForecast.cycle_time <= cycle_to)
    stmt = stmt.order_by(PointForecast.station_id, PointForecast.valid_time)
    return frame(db.execute(stmt), FORECAST_COLUMNS)


def load_observations(
    db: Session, station_ids: Sequence[int], start: datetime, end: datetime
) -> pd.DataFrame:
    """Usable observations (``qc_flag != 'bad'``) of the stations in [start, end]."""
    if not station_ids:
        return frame([], OBSERVATION_COLUMNS)
    stmt = (
        select(*(getattr(BuoyObservation, c) for c in OBSERVATION_COLUMNS))
        .where(
            BuoyObservation.station_id.in_(list(station_ids)),
            BuoyObservation.observed_at >= start,
            BuoyObservation.observed_at <= end,
            BuoyObservation.qc_flag != "bad",
        )
        .order_by(BuoyObservation.station_id, BuoyObservation.observed_at)
    )
    return frame(db.execute(stmt), OBSERVATION_COLUMNS)


def match_observations(
    forecasts: pd.DataFrame,
    observations: pd.DataFrame,
    *,
    time_col: str,
    value_cols: Sequence[str],
    tolerance: timedelta,
    direction: str = "nearest",
) -> pd.DataFrame:
    """For every forecast row, the matching observation of the same station.

    ``direction='nearest'`` takes the observation closest to ``forecasts[time_col]``
    within ``tolerance``; on an exact tie pandas keeps the earlier observation.
    ``direction='backward'`` takes the latest observation at or before it (used for
    persistence: only information available at the cycle time). Only observations whose
    ``value_cols`` are all present are eligible. Returns a frame aligned with
    ``forecasts`` (same index) holding ``obs_observed_at`` and ``obs_<col>`` columns
    (NaN / NaT where nothing matched).
    """
    out_cols = ["obs_observed_at", *(f"obs_{c}" for c in value_cols)]
    if forecasts.empty:
        return pd.DataFrame(columns=out_cols, index=forecasts.index)
    obs = observations.dropna(subset=list(value_cols))
    obs = obs[["station_id", "observed_at", *value_cols]].rename(
        columns={"observed_at": "obs_observed_at", **{c: f"obs_{c}" for c in value_cols}}
    )
    left = forecasts[["station_id", time_col]].copy()
    left["_row"] = np.arange(len(left))
    left = left.sort_values(time_col, kind="stable")
    right = obs.sort_values("obs_observed_at", kind="stable")
    right["_key"] = utc_times(right["obs_observed_at"])
    left["_key"] = utc_times(left[time_col])
    left["station_id"] = left["station_id"].astype("int64")
    right["station_id"] = right["station_id"].astype("int64")
    merged = pd.merge_asof(
        left,
        right.drop(columns=["obs_observed_at"]).assign(obs_observed_at=right["_key"]),
        on="_key",
        by="station_id",
        direction=direction,
        tolerance=pd.Timedelta(tolerance),
        allow_exact_matches=True,
    )
    merged = merged.sort_values("_row")
    result = merged[out_cols].copy()
    result.index = forecasts.index
    return result


def persistence_observations(
    forecasts: pd.DataFrame,
    observations: pd.DataFrame,
    *,
    hs_kinds: Sequence[str] = ("spectral_hm0",),
    max_age: timedelta = PERSISTENCE_MAX_AGE,
) -> pd.DataFrame:
    """The observation at or before each row's ``cycle_time`` (within ``max_age``).

    Uses only ``observed_at <= cycle_time``: the persistence forecast and the
    analysis-error feature must be built from information available at issue time.
    Returns ``obs_observed_at`` and ``obs_hs_m`` aligned with ``forecasts``.
    """
    obs = observations[observations["hs_kind"].isin(list(hs_kinds))]
    return match_observations(
        forecasts,
        obs,
        time_col="cycle_time",
        value_cols=("hs_m",),
        tolerance=max_age,
        direction="backward",
    )


# ------------------------------------------------------------------------------ pairing
@dataclass
class PairingSummary:
    forecasts_scanned: int = 0
    stations: int = 0
    pairs_written: Counter[str] = field(default_factory=Counter)
    hs_by_kind: Counter[str] = field(default_factory=Counter)
    unmatched: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    # station id -> error message for stations whose pairing failed (rolled back).
    errors: dict[int, str] = field(default_factory=dict)

    def merge(self, other: PairingSummary) -> None:
        self.forecasts_scanned += other.forecasts_scanned
        self.stations += other.stations
        for mine, theirs in (
            (self.pairs_written, other.pairs_written),
            (self.hs_by_kind, other.hs_by_kind),
            (self.unmatched, other.unmatched),
            (self.skipped, other.skipped),
        ):
            mine.update(theirs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "forecasts_scanned": self.forecasts_scanned,
            "stations": self.stations,
            "pairs_written": dict(self.pairs_written),
            "hs_pairs_by_observed_kind": dict(self.hs_by_kind),
            "unmatched": dict(self.unmatched),
            "skipped": dict(self.skipped),
            "errors": {str(k): v for k, v in self.errors.items()},
        }


def _pending_forecasts(
    db: Session,
    *,
    now: datetime,
    lookback: timedelta | None,
    sources: Sequence[str] | None,
    station_ids: Sequence[int] | None,
    full: bool,
) -> pd.DataFrame:
    stmt = select(*(getattr(PointForecast, c) for c in FORECAST_COLUMNS)).where(
        PointForecast.valid_time <= now
    )
    if lookback is not None:
        stmt = stmt.where(PointForecast.valid_time >= now - lookback)
    if sources:
        stmt = stmt.where(PointForecast.source_code.in_(list(sources)))
    if station_ids is not None:
        stmt = stmt.where(PointForecast.station_id.in_(list(station_ids)))
    if not full:
        pv = PredictionValidation
        stmt = stmt.where(
            ~exists().where(
                and_(
                    pv.station_id == PointForecast.station_id,
                    pv.source_code == PointForecast.source_code,
                    pv.cycle_time == PointForecast.cycle_time,
                    pv.valid_time == PointForecast.valid_time,
                    pv.model_version == RAW_MODEL_VERSION,
                    pv.variable == "hs",
                )
            )
        )
    stmt = stmt.order_by(PointForecast.station_id, PointForecast.valid_time)
    return frame(db.execute(stmt), FORECAST_COLUMNS)


def build_pairs(
    forecasts: pd.DataFrame,
    observations: pd.DataFrame,
    *,
    tolerance: timedelta = DEFAULT_TOLERANCE,
    summary: PairingSummary | None = None,
) -> list[dict[str, Any]]:
    """Pure pairing step: prediction_validation rows (dicts) for the given frames."""
    summary = summary if summary is not None else PairingSummary()
    rows: list[dict[str, Any]] = []
    if forecasts.empty:
        return rows
    fc = forecasts.reset_index(drop=True)
    kinds = [
        forecast_conventions(s, e) for s, e in zip(fc["source_code"], fc["extra"], strict=True)
    ]
    fc["period_kind"] = [k[0] for k in kinds]
    fc["direction_kind"] = [k[1] for k in kinds]

    def emit(sub: pd.DataFrame, variable: str, f_col: str, o_col: str, kinds: Any) -> None:
        if sub.empty:
            return
        f = sub[f_col].to_numpy(dtype=float)
        o = sub[o_col].to_numpy(dtype=float)
        err = circular_difference(f, o) if variable == "dir" else f - o
        kind_values = kinds if not isinstance(kinds, str) else [kinds] * len(sub)
        for rec, fv, ov, ev, kind in zip(
            sub.itertuples(index=False), f, o, err, kind_values, strict=True
        ):
            rows.append(
                {
                    "station_id": int(rec.station_id),
                    "source_code": rec.source_code,
                    "model_version": RAW_MODEL_VERSION,
                    "cycle_time": rec.cycle_time.to_pydatetime(),
                    "valid_time": rec.valid_time.to_pydatetime(),
                    "lead_hours": float(rec.lead_hours),
                    "variable": variable,
                    "forecast_value": float(fv),
                    "observed_value": float(ov),
                    "error": float(ev),
                    "observed_kind": str(kind),
                }
            )
        summary.pairs_written[variable] += len(sub)

    # Hs: nearest observation reporting a height (any kind; the kind travels with it).
    hs_fc = fc[fc["hs_m"].notna()]
    hs_obs = match_observations(
        hs_fc,
        observations,
        time_col="valid_time",
        value_cols=("hs_m", "hs_kind"),
        tolerance=tolerance,
    )
    hs_join = pd.concat([hs_fc, hs_obs], axis=1)
    matched = hs_join[hs_join["obs_hs_m"].notna()]
    summary.unmatched["hs"] += int(len(hs_join) - len(matched))
    for kind, count in matched["obs_hs_kind"].value_counts().items():
        summary.hs_by_kind[str(kind)] += int(count)
    emit(matched, "hs", "hs_m", "obs_hs_m", matched["obs_hs_kind"].astype(str).tolist())

    # Tp: only peak period vs peak period.
    tp_fc = fc[fc["tp_s"].notna()]
    not_peak = tp_fc["period_kind"] != "tp"
    summary.skipped["tp_period_not_peak_or_unknown"] += int(not_peak.sum())
    tp_fc = tp_fc[~not_peak]
    tp_obs = match_observations(
        tp_fc, observations, time_col="valid_time", value_cols=("tp_s",), tolerance=tolerance
    )
    tp_join = pd.concat([tp_fc, tp_obs], axis=1)
    matched_tp = tp_join[tp_join["obs_tp_s"].notna()]
    summary.unmatched["tp"] += int(len(tp_join) - len(matched_tp))
    emit(matched_tp, "tp", "tp_s", "obs_tp_s", "tp")

    # Direction: same kind of direction on both sides.
    dir_fc = fc[fc["direction_deg"].notna()]
    unknown = dir_fc["direction_kind"].isna()
    summary.skipped["dir_kind_unknown"] += int(unknown.sum())
    for kind, obs_col in (("peak", "peak_direction_deg"), ("mean", "mean_direction_deg")):
        sub = dir_fc[dir_fc["direction_kind"] == kind]
        d_obs = match_observations(
            sub, observations, time_col="valid_time", value_cols=(obs_col,), tolerance=tolerance
        )
        joined = pd.concat([sub, d_obs], axis=1)
        matched_d = joined[joined[f"obs_{obs_col}"].notna()]
        summary.unmatched["dir"] += int(len(joined) - len(matched_d))
        emit(matched_d, "dir", "direction_deg", f"obs_{obs_col}", kind)
    return rows


def _forecast_keys(df: pd.DataFrame) -> list[tuple[int, str, datetime, datetime]]:
    return [
        (int(s), str(src), c.to_pydatetime(), v.to_pydatetime())
        for s, src, c, v in zip(
            df["station_id"], df["source_code"], df["cycle_time"], df["valid_time"], strict=True
        )
    ]


def replace_pairs(
    db: Session,
    keys: Sequence[tuple[int, str, datetime, datetime]],
    rows: Sequence[dict[str, Any]],
    *,
    model_version: str = RAW_MODEL_VERSION,
    variables: Sequence[str] = PAIR_VARIABLES,
) -> None:
    """Delete the pairs of ``keys`` for ``model_version`` and insert ``rows`` (no commit)."""
    pv = PredictionValidation
    for i in range(0, len(keys), KEY_CHUNK):
        chunk = list(keys[i : i + KEY_CHUNK])
        db.execute(
            delete(pv).where(
                pv.model_version == model_version,
                pv.variable.in_(list(variables)),
                tuple_(pv.station_id, pv.source_code, pv.cycle_time, pv.valid_time).in_(chunk),
            )
        )
    now = utcnow()
    for i in range(0, len(rows), WRITE_CHUNK):
        chunk_rows = [{**r, "computed_at": now} for r in rows[i : i + WRITE_CHUNK]]
        stmt = insert(pv).values(chunk_rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_prediction_validation_pair",
            set_={
                "lead_hours": stmt.excluded.lead_hours,
                "forecast_value": stmt.excluded.forecast_value,
                "observed_value": stmt.excluded.observed_value,
                "error": stmt.excluded.error,
                "observed_kind": stmt.excluded.observed_kind,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        db.execute(stmt)


def pair_forecasts(
    db: Session,
    *,
    now: datetime | None = None,
    lookback: timedelta | None = DEFAULT_LOOKBACK,
    full: bool = False,
    sources: Sequence[str] | None = None,
    station_ids: Sequence[int] | None = None,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> PairingSummary:
    """Pair pending point forecasts with observations and write prediction_validation.

    ``full=True`` ignores the look-back window and reprocesses forecasts that already
    have pairs (backfill after delayed-mode QC data arrive). Stations are loaded and
    committed one at a time, so memory stays bounded by one station's forecasts. A station
    whose pairing fails is rolled back and listed in ``summary.errors`` while the others
    continue (partial results are reported, not dropped); if every station fails, the
    first error is raised.
    """
    now = now or utcnow()
    summary = PairingSummary()
    first_error: Exception | None = None
    if station_ids is None:
        station_ids = list(
            db.scalars(
                select(PointForecast.station_id).distinct().order_by(PointForecast.station_id)
            )
        )
    for station_id in sorted({int(s) for s in station_ids}):
        group = _pending_forecasts(
            db,
            now=now,
            lookback=None if full else lookback,
            sources=sources,
            station_ids=[station_id],
            full=full,
        )
        if group.empty:
            continue
        station_summary = PairingSummary(forecasts_scanned=len(group), stations=1)
        try:
            start = group["valid_time"].min() - pd.Timedelta(tolerance)
            end = group["valid_time"].max() + pd.Timedelta(tolerance)
            obs = load_observations(db, [station_id], start.to_pydatetime(), end.to_pydatetime())
            rows = build_pairs(group, obs, tolerance=tolerance, summary=station_summary)
            replace_pairs(db, _forecast_keys(group), rows)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Pairing failed for station %s", station_id)
            summary.errors[station_id] = f"{type(exc).__name__}: {exc}"[:500]
            first_error = first_error or exc
            continue
        summary.merge(station_summary)
    if first_error is not None and summary.stations == 0:
        raise first_error
    logger.info("Pairing: %s", summary.as_dict())
    return summary
