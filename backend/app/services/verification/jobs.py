"""Scheduled verification: pair new forecasts, then refresh rolling accuracy metrics.

``run_verification`` is the body of the Celery task ``verification.run`` (daily, when
VERIFICATION_ENABLED). It pairs new point forecasts with observations
(app.services.verification.pairing) and recomputes ``forecast_accuracy_metrics`` over
rolling 30- and 90-day windows of valid time ending at UTC midnight of ``now`` (so a
re-run on the same day upserts the same rows), for every combination of

* scope: each station (``scope_key`` = station code), each region
  (``buoy_stations.metadata.region``, else ``"all"``) and global (``"all"``);
* source code;
* model version: ``"raw"`` and, when one is ACTIVE, the corrected forecasts of that
  model (``"<name>@v<N>"``), recomputed from features and written to
  ``prediction_validation``. Only cycles after the model's training data end are
  scored, so these metrics are out of sample;
* variable: ``hs`` (pairs with spectral Hm0 observations), ``hs_h13`` (time-domain
  H1/3) and ``hs_unknown`` (undefined height), kept apart; ``tp`` (peak periods);
  ``dir`` (circular statistics in degrees: bias = circular mean error, rmse/mae of the
  wrapped errors; SI/HH/CC do not apply);
* lead bucket (app.services.verification.metrics; ``-1`` = all leads pooled).

References: persistence = the observation at or before the cycle time (<= 1 h old, same
height kind); climatology = the station's monthly mean of observations BEFORE the start
of the 90-day window (at least MIN_CLIMATOLOGY_OBS per month), i.e. out of sample.
Rows need at least ``MIN_PAIRS_FOR_METRICS`` pairs. ``extra`` holds the cycle-block
bootstrap CIs (``JOB_BOOTSTRAP_N`` replicates, 90 %), NRMSE, event scores for
Hs >= 2 m and >= 3 m, reference sample sizes and, for ensemble sources with a spread,
the normal CRPS and the coverage/width of the mean ± 1.645·spread interval.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    BuoyObservation,
    BuoyStation,
    ForecastAccuracyMetric,
    PointForecast,
    PredictionValidation,
)
from app.services.ml.apply import Correction, load_active_correction
from app.services.ml.baselines import MIN_CLIMATOLOGY_OBS
from app.services.ml.dataset import build_dataset
from app.services.ml.registry import DEFAULT_MODEL_NAME
from app.services.verification import metrics as vm
from app.services.verification.pairing import (
    PERSISTENCE_MAX_AGE,
    RAW_MODEL_VERSION,
    load_observations,
    pair_forecasts,
    persistence_observations,
    replace_pairs,
    utc_times,
)

logger = logging.getLogger(__name__)

WINDOWS_DAYS: tuple[int, ...] = (30, 90)
MIN_PAIRS_FOR_METRICS = 10
JOB_BOOTSTRAP_N = 200
CI_CONFIDENCE = 0.90
EVENT_THRESHOLDS_M = (2.0, 3.0)
Z90 = 1.6448536269514722
HS_VARIABLE_BY_KIND = {"spectral_hm0": "hs", "h_third": "hs_h13", "unknown": "hs_unknown"}
# Point forecasts can be issued up to this long before their valid time.
MAX_LEAD = timedelta(days=16)
UPSERT_CHUNK = 500


def run_verification(db: Session, now: datetime | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.verification_enabled:
        return {"skipped": "VERIFICATION_ENABLED is false"}
    now = now or utcnow()
    pairing = pair_forecasts(db, now=now)
    metrics = compute_accuracy_metrics(db, now=now)
    return {"pairing": pairing.as_dict(), "metrics": metrics}


def period_end_for(now: datetime) -> datetime:
    """UTC midnight at or before ``now`` (end of every rolling window)."""
    t = now.astimezone(UTC)
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------------------ loading
def _load_raw_pairs(db: Session, start: datetime, end: datetime) -> pd.DataFrame:
    pv, pf, bs = PredictionValidation, PointForecast, BuoyStation
    stmt = (
        select(
            pv.station_id,
            bs.code,
            bs.metadata_,
            pv.source_code,
            pv.model_version,
            pv.cycle_time,
            pv.valid_time,
            pv.lead_hours,
            pv.variable,
            pv.forecast_value,
            pv.observed_value,
            pv.observed_kind,
            pf.hs_spread_m,
        )
        .join(bs, bs.id == pv.station_id)
        .outerjoin(
            pf,
            and_(
                pf.station_id == pv.station_id,
                pf.source_code == pv.source_code,
                pf.cycle_time == pv.cycle_time,
                pf.valid_time == pv.valid_time,
            ),
        )
        .where(
            pv.model_version == RAW_MODEL_VERSION,
            pv.valid_time >= start,
            pv.valid_time < end,
        )
    )
    cols = [
        "station_id",
        "station_code",
        "metadata",
        "source_code",
        "model_version",
        "cycle_time",
        "valid_time",
        "lead_hours",
        "variable",
        "forecast_value",
        "observed_value",
        "observed_kind",
        "hs_spread_m",
    ]
    df = pd.DataFrame.from_records([tuple(r) for r in db.execute(stmt)], columns=cols)
    if df.empty:
        return df
    df["cycle_time"] = utc_times(df["cycle_time"])
    df["valid_time"] = utc_times(df["valid_time"])
    df["region"] = [str((m or {}).get("region") or "all") for m in df["metadata"]]
    df["metric_variable"] = [
        HS_VARIABLE_BY_KIND.get(k, "hs_unknown") if v == "hs" else v
        for v, k in zip(df["variable"], df["observed_kind"], strict=True)
    ]
    return df.drop(columns=["metadata"])


def _climatology(db: Session, before: datetime) -> dict[tuple[int, str, int], float]:
    """(station_id, hs_kind, month) -> mean observed Hs, observations before ``before``."""
    month = func.extract("month", BuoyObservation.observed_at)
    stmt = (
        select(
            BuoyObservation.station_id,
            BuoyObservation.hs_kind,
            month,
            func.avg(BuoyObservation.hs_m),
            func.count(BuoyObservation.hs_m),
        )
        .where(
            BuoyObservation.observed_at < before,
            BuoyObservation.qc_flag != "bad",
            BuoyObservation.hs_m.is_not(None),
        )
        .group_by(BuoyObservation.station_id, BuoyObservation.hs_kind, month)
    )
    return {
        (int(sid), str(kind), int(m)): float(mean)
        for sid, kind, m, mean, count in db.execute(stmt)
        if int(count) >= MIN_CLIMATOLOGY_OBS
    }


def _add_references(
    db: Session, df: pd.DataFrame, climatology: dict[tuple[int, str, int], float]
) -> pd.DataFrame:
    df = df.copy()
    df["ref_persistence"] = np.nan
    df["ref_climatology"] = np.nan
    hs_rows = df["variable"] == "hs"
    if not hs_rows.any():
        return df
    obs = load_observations(
        db,
        sorted({int(s) for s in df.loc[hs_rows, "station_id"]}),
        (df.loc[hs_rows, "cycle_time"].min() - pd.Timedelta(PERSISTENCE_MAX_AGE)).to_pydatetime(),
        df.loc[hs_rows, "cycle_time"].max().to_pydatetime(),
    )
    for kind in df.loc[hs_rows, "observed_kind"].unique():
        mask = hs_rows & (df["observed_kind"] == kind)
        persisted = persistence_observations(df[mask], obs, hs_kinds=(str(kind),))
        df.loc[mask, "ref_persistence"] = pd.to_numeric(persisted["obs_hs_m"], errors="coerce")
    months = df["valid_time"].dt.month.to_numpy()
    df["ref_climatology"] = [
        climatology.get((int(s), str(k), int(m)), np.nan) if v == "hs" else np.nan
        for s, k, m, v in zip(
            df["station_id"], df["observed_kind"], months, df["variable"], strict=True
        )
    ]
    return df


def _corrected_pairs(
    db: Session, correction: Correction, start: datetime, end: datetime
) -> pd.DataFrame:
    """Corrected Hs at training stations for cycles after the training data end."""
    cycle_from = start - MAX_LEAD
    if correction.training_end is not None:
        cycle_from = max(cycle_from, correction.training_end)
    ds = build_dataset(
        db,
        sources=sorted(correction.sources) or None,
        station_codes=sorted(correction.station_codes),
        start=cycle_from,
        end=end,
    )
    frame = ds.frame
    if frame.empty:
        return pd.DataFrame()
    keep = (frame["valid_time"] >= start) & (frame["valid_time"] < end)
    if correction.training_end is not None:
        keep &= frame["cycle_time"] > pd.Timestamp(correction.training_end)
    frame = frame[keep].reset_index(drop=True)
    if frame.empty:
        return pd.DataFrame()
    corrected, applied = correction.correct_frame(frame)
    frame = frame[applied].reset_index(drop=True)
    corrected = corrected[applied]
    stations: dict[int, dict[str, Any]] = {
        int(sid): dict(meta or {})
        for sid, meta in db.execute(select(BuoyStation.id, BuoyStation.metadata_))
    }
    return pd.DataFrame(
        {
            "station_id": frame["station_id"].astype(int),
            "station_code": frame["station_code"],
            "source_code": frame["source_code"],
            "model_version": correction.label,
            "cycle_time": frame["cycle_time"],
            "valid_time": frame["valid_time"],
            "lead_hours": frame["lead_hours"],
            "variable": "hs",
            "forecast_value": corrected,
            "observed_value": frame["obs_hs_m"],
            "observed_kind": frame["obs_hs_kind"].astype(str),
            "hs_spread_m": np.nan,
            "region": [
                str((stations.get(int(s)) or {}).get("region") or "all")
                for s in frame["station_id"]
            ],
            "metric_variable": [
                HS_VARIABLE_BY_KIND.get(str(k), "hs_unknown") for k in frame["obs_hs_kind"]
            ],
            "ref_persistence": frame["persistence_hs_m"].to_numpy(dtype=float),
        }
    )


def _store_corrected_pairs(db: Session, pairs: pd.DataFrame, label: str) -> int:
    if pairs.empty:
        return 0
    rows = [
        {
            "station_id": int(r.station_id),
            "source_code": r.source_code,
            "model_version": label,
            "cycle_time": r.cycle_time.to_pydatetime(),
            "valid_time": r.valid_time.to_pydatetime(),
            "lead_hours": float(r.lead_hours),
            "variable": "hs",
            "forecast_value": float(r.forecast_value),
            "observed_value": float(r.observed_value),
            "error": float(r.forecast_value - r.observed_value),
            "observed_kind": r.observed_kind,
        }
        for r in pairs.itertuples(index=False)
    ]
    keys = [(r["station_id"], r["source_code"], r["cycle_time"], r["valid_time"]) for r in rows]
    replace_pairs(db, keys, rows, model_version=label, variables=("hs",))
    return len(rows)


# ------------------------------------------------------------------------------ metrics


def group_metrics(
    sub: pd.DataFrame, variable: str, *, n_boot: int = JOB_BOOTSTRAP_N
) -> dict[str, Any] | None:
    """Metric columns + extra for one group (None when too few pairs)."""
    f = sub["forecast_value"].to_numpy(dtype=float)
    o = sub["observed_value"].to_numpy(dtype=float)
    n = vm.n_pairs(f, o)
    if n < MIN_PAIRS_FOR_METRICS:
        return None
    blocks = sub["cycle_time"].astype("int64").to_numpy()
    if variable == "dir":
        scores = vm.direction_scores(f, o)
        return {
            "n": n,
            "bias": vm.finite_or_none(scores["bias"]),
            "mae": vm.finite_or_none(scores["mae"]),
            "rmse": vm.finite_or_none(scores["rmse"]),
            "scatter_index": None,
            "hh": None,
            "correlation": None,
            "skill_vs_persistence": None,
            "skill_vs_climatology": None,
            "extra": {"units": "deg", "statistics": "circular (wrapped errors)"},
        }
    scores = vm.continuous_scores(f, o)
    kwargs = {"n_boot": n_boot, "confidence": CI_CONFIDENCE}
    extra: dict[str, Any] = {
        "nrmse": vm.finite_or_none(scores["nrmse"]),
        "rmse_ci": vm.rmse_ci(f, o, blocks, **kwargs).as_dict(),
        "bias_ci": vm.bias_ci(f, o, blocks, **kwargs).as_dict(),
        "cycles": int(sub["cycle_time"].nunique()),
        "units": "m" if variable.startswith("hs") else "s",
    }
    out: dict[str, Any] = {
        "n": n,
        "bias": vm.finite_or_none(scores["bias"]),
        "mae": vm.finite_or_none(scores["mae"]),
        "rmse": vm.finite_or_none(scores["rmse"]),
        "scatter_index": vm.finite_or_none(scores["si"]),
        "hh": vm.finite_or_none(scores["hh"]),
        "correlation": vm.finite_or_none(scores["cc"]),
        "skill_vs_persistence": None,
        "skill_vs_climatology": None,
    }
    if variable.startswith("hs"):
        for ref in ("persistence", "climatology"):
            r = (
                sub[f"ref_{ref}"].to_numpy(dtype=float)
                if f"ref_{ref}" in sub
                else np.full(n, np.nan)
            )
            n_ref = int(vm.finite_pairs(f, o, r)[0].size)
            extra[f"n_{ref}"] = n_ref
            if n_ref >= MIN_PAIRS_FOR_METRICS:
                out[f"skill_vs_{ref}"] = vm.finite_or_none(vm.skill_score(f, o, r))
        extra["events"] = {f"{t:g}": vm.event_scores(f, o, t) for t in EVENT_THRESHOLDS_M}
        spread = sub["hs_spread_m"].to_numpy(dtype=float) if "hs_spread_m" in sub else None
        if spread is not None:
            ff, oo, ss = vm.finite_pairs(f, o, spread)
            if ff.size >= MIN_PAIRS_FOR_METRICS:
                extra["ensemble"] = {
                    "n": int(ff.size),
                    "crps_normal": vm.finite_or_none(float(np.mean(vm.crps_normal(ff, ss, oo)))),
                    "coverage_90": vm.finite_or_none(
                        vm.interval_coverage(ff - Z90 * ss, ff + Z90 * ss, oo)
                    ),
                    "width_90": vm.finite_or_none(
                        vm.mean_interval_width(ff - Z90 * ss, ff + Z90 * ss)
                    ),
                }
    out["extra"] = _jsonable(extra)
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def compute_accuracy_metrics(
    db: Session,
    *,
    now: datetime | None = None,
    windows_days: Sequence[int] = WINDOWS_DAYS,
    n_boot: int = JOB_BOOTSTRAP_N,
    model_name: str = DEFAULT_MODEL_NAME,
) -> dict[str, Any]:
    """Recompute and upsert forecast_accuracy_metrics for the rolling windows."""
    now = now or utcnow()
    period_end = period_end_for(now)
    longest = max(windows_days)
    load_start = period_end - timedelta(days=longest)
    summary: dict[str, Any] = {
        "period_end": period_end.isoformat(),
        "windows_days": list(windows_days),
        "rows_upserted": 0,
        "corrected_model": None,
        "corrected_pairs": 0,
        "warnings": [],
    }
    frames = []
    # Out-of-sample climatology: observations before the longest window only.
    clim = _climatology(db, load_start)
    raw = _load_raw_pairs(db, load_start, period_end)
    if not raw.empty:
        frames.append(_add_references(db, raw, clim))
    correction = load_active_correction(db, model_name)
    if correction is not None:
        summary["corrected_model"] = correction.label
        corrected = _corrected_pairs(db, correction, load_start, period_end)
        if not corrected.empty:
            months = corrected["valid_time"].dt.month.to_numpy()
            corrected["ref_climatology"] = [
                clim.get((int(s), str(k), int(m)), np.nan)
                for s, k, m in zip(
                    corrected["station_id"], corrected["observed_kind"], months, strict=True
                )
            ]
            summary["corrected_pairs"] = _store_corrected_pairs(db, corrected, correction.label)
            db.commit()
            frames.append(corrected)
        else:
            summary["warnings"].append(
                f"{correction.label}: no out-of-sample forecasts in the window"
            )
    if not frames:
        return summary
    data = pd.concat(frames, ignore_index=True)
    rows: list[dict[str, Any]] = []
    computed_at = utcnow()
    scopes = (("station", "station_code"), ("region", "region"), ("global", None))
    for days in windows_days:
        start = period_end - timedelta(days=days)
        window = data[(data["valid_time"] >= start) & (data["valid_time"] < period_end)]
        if window.empty:
            continue
        for scope, key_col in scopes:
            keys = window[key_col] if key_col else pd.Series("all", index=window.index)
            grouped = window.assign(_key=keys).groupby(
                ["_key", "source_code", "model_version", "metric_variable"], sort=True
            )
            for (key, source, model_version, variable), group in grouped:
                leads = group["lead_hours"].to_numpy(dtype=float)
                for bucket in (vm.ALL_LEADS_BUCKET, *vm.LEAD_BUCKETS_H):
                    sub = group[vm.in_lead_bucket(leads, bucket)]
                    values = group_metrics(sub, str(variable), n_boot=n_boot)
                    if values is None:
                        continue
                    values["extra"]["window_days"] = days
                    rows.append(
                        {
                            "scope": scope,
                            "scope_key": str(key)[:80],
                            "source_code": str(source),
                            "model_version": str(model_version),
                            "variable": str(variable),
                            "lead_bucket_h": int(bucket),
                            "period_start": start,
                            "period_end": period_end,
                            "computed_at": computed_at,
                            **values,
                        }
                    )
    summary["rows_upserted"] = upsert_metrics(db, rows)
    db.commit()
    logger.info("Verification metrics: %s", summary)
    return summary


METRIC_FIELDS = (
    "n",
    "bias",
    "mae",
    "rmse",
    "scatter_index",
    "hh",
    "correlation",
    "skill_vs_persistence",
    "skill_vs_climatology",
    "extra",
    "computed_at",
)


def upsert_metrics(db: Session, rows: Sequence[dict[str, Any]]) -> int:
    """Insert or update forecast_accuracy_metrics rows on their unique key (no commit)."""
    for i in range(0, len(rows), UPSERT_CHUNK):
        stmt = insert(ForecastAccuracyMetric).values(list(rows[i : i + UPSERT_CHUNK]))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_forecast_accuracy_metrics_key",
            set_={f: getattr(stmt.excluded, f) for f in METRIC_FIELDS},
        )
        db.execute(stmt)
    return len(rows)
