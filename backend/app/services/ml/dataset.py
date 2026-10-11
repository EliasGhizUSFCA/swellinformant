"""Training/evaluation dataset and leakage-safe chronological splits.

One row per (station, source, cycle_time, valid_time): the archived point forecast, its
features (app.services.ml.features, all known at the cycle time) and the observed Hs at
the valid time (nearest observation within 30 min, same matching as
app.services.verification.pairing). Only spectral Hm0 observations are used as targets
by default, because the model value is an Hm0 (like with like). Rows with a lead below
MIN_FORECAST_LEAD_H (3 h) are analysis-time rows whose target observation can be the
persistence feature itself; they are dropped (the lead-0 row is still used to compute
the analysis-error feature of the other leads of its cycle).

Splits are chronological. Errors are autocorrelated in time and one weather event
appears in many cycles, so between consecutive sets there is a gap of at least
``max lead + 3 days`` (research notes, ML section): every training row's valid time
(its observation) must lie before ``first test cycle - gap``. Rows inside the gap are
purged, not moved. :func:`assert_no_leakage` enforces this and is called on every split
before any model is fitted; :func:`assert_causal_features` checks that the observation
features were taken at or before the cycle time and every target observation after it.
No normalisation, decomposition or
imputation is applied to the full series: all of that happens inside the fitted
estimator pipelines, on training rows only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BuoyStation
from app.services.ml.features import (
    FEATURE_NAMES,
    HS_FLOOR_M,
    TARGET_NAME,
    build_feature_frame,
    floored_log,
    log_residual,
)
from app.services.verification.metrics import MIN_FORECAST_LEAD_H
from app.services.verification.pairing import (
    DEFAULT_TOLERANCE,
    PERSISTENCE_MAX_AGE,
    forecast_conventions,
    load_observations,
    load_point_forecasts,
    match_observations,
    persistence_observations,
    utc_times,
)

logger = logging.getLogger(__name__)

GAP_EXTRA = timedelta(days=3)
DEFAULT_VAL_FRACTION = 0.2
DEFAULT_TEST_FRACTION = 0.2
DEFAULT_HS_KINDS: tuple[str, ...] = ("spectral_hm0",)

ID_COLUMNS = (
    "station_id",
    "station_code",
    "source_code",
    "family",
    "cycle_time",
    "valid_time",
    "lead_hours",
)
OBS_COLUMNS = (
    "obs_hs_m",
    "obs_hs_kind",
    "obs_observed_at",
    "persistence_observed_at",
    "analysis_observed_at",
)


class LeakageError(RuntimeError):
    """A split or feature would let information from the evaluation period leak in."""


class InsufficientDataError(ValueError):
    """Not enough data for the requested split or evaluation."""


@dataclass
class Dataset:
    frame: pd.DataFrame
    summary: dict[str, Any] = field(default_factory=dict)


def required_gap(max_lead_hours: float) -> timedelta:
    """Minimum gap between consecutive chronological sets: max lead + 3 days."""
    return timedelta(hours=float(max_lead_hours)) + GAP_EXTRA


def _station_codes(db: Session) -> dict[int, tuple[str, dict[str, Any]]]:
    rows = db.execute(select(BuoyStation.id, BuoyStation.code, BuoyStation.metadata_)).all()
    return {int(r[0]): (str(r[1]), dict(r[2] or {})) for r in rows}


def build_dataset(
    db: Session,
    *,
    sources: Sequence[str] | None = None,
    station_codes: Sequence[str] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    hs_kinds: Sequence[str] = DEFAULT_HS_KINDS,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> Dataset:
    """Assemble the dataset from point_forecasts and buoy_observations.

    ``start``/``end`` bound the cycle time. Rows without a model Hs or without a
    matching observation (of an accepted ``hs_kind``) are dropped and counted.
    """
    stations = _station_codes(db)
    station_ids: list[int] | None = None
    if station_codes:
        wanted = set(station_codes)
        station_ids = [sid for sid, (code, _) in stations.items() if code in wanted]
    fc = load_point_forecasts(
        db, station_ids=station_ids, sources=sources, cycle_from=start, cycle_to=end
    )
    if fc.empty:
        return Dataset(_empty_frame(), _summary(_empty_frame(), 0, {}, hs_kinds, sources))
    obs = load_observations(
        db,
        sorted({int(s) for s in fc["station_id"]}),
        (fc["cycle_time"].min() - pd.Timedelta(PERSISTENCE_MAX_AGE)).to_pydatetime(),
        (fc["valid_time"].max() + pd.Timedelta(tolerance)).to_pydatetime(),
    )
    frame = assemble_frame(fc, obs, stations, hs_kinds=hs_kinds, tolerance=tolerance)
    dropped = {
        "no_model_hs": int(fc["hs_m"].isna().sum()),
    }
    with_hs = fc[fc["hs_m"].notna()]
    analysis_rows = int((with_hs["lead_hours"] < MIN_FORECAST_LEAD_H).sum())
    dropped["analysis_time_rows"] = analysis_rows
    dropped["no_matching_observation"] = int(len(with_hs) - analysis_rows - len(frame))
    return Dataset(frame, _summary(frame, len(fc), dropped, hs_kinds, sources))


def assemble_frame(
    fc: pd.DataFrame,
    obs: pd.DataFrame,
    stations: dict[int, tuple[str, dict[str, Any]]],
    *,
    hs_kinds: Sequence[str] = DEFAULT_HS_KINDS,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> pd.DataFrame:
    """Pure step of :func:`build_dataset`: features, persistence and target per row."""
    fc = fc[fc["hs_m"].notna()].reset_index(drop=True)
    if fc.empty:
        return _empty_frame()
    conv = [forecast_conventions(s, e) for s, e in zip(fc["source_code"], fc["extra"], strict=True)]
    fc["period_kind"] = [c[0] for c in conv]
    target_obs = obs[obs["hs_kind"].isin(list(hs_kinds))]
    matched = match_observations(
        fc,
        target_obs,
        time_col="valid_time",
        value_cols=("hs_m", "hs_kind"),
        tolerance=tolerance,
    )
    persist = persistence_observations(fc, obs, hs_kinds=hs_kinds)
    fc["persistence_hs_m"] = pd.to_numeric(persist["obs_hs_m"], errors="coerce")
    fc["persistence_observed_at"] = persist["obs_observed_at"]
    # Model analysis (lead 0) of the same cycle, for the analysis-error feature.
    lead0 = fc[fc["lead_hours"].abs() < 1e-6][["station_id", "source_code", "cycle_time", "hs_m"]]
    lead0 = lead0.drop_duplicates(["station_id", "source_code", "cycle_time"]).rename(
        columns={"hs_m": "_analysis_hs"}
    )
    fc = fc.merge(lead0, on=["station_id", "source_code", "cycle_time"], how="left")
    fc["analysis_log_error"] = floored_log(fc["persistence_hs_m"]) - floored_log(fc["_analysis_hs"])
    fc["analysis_observed_at"] = fc["persistence_observed_at"].where(
        fc["analysis_log_error"].notna()
    )
    feats = build_feature_frame(fc)
    out = pd.DataFrame(
        {
            "station_id": fc["station_id"].astype("int64"),
            "station_code": [stations.get(int(s), (f"id:{s}", {}))[0] for s in fc["station_id"]],
            "source_code": fc["source_code"].astype(str),
            "family": fc["family"].astype(str),
            "cycle_time": fc["cycle_time"],
            "valid_time": fc["valid_time"],
            "lead_hours": fc["lead_hours"].astype(float),
            "obs_hs_m": pd.to_numeric(matched["obs_hs_m"], errors="coerce").to_numpy(),
            "obs_hs_kind": matched["obs_hs_kind"].to_numpy(),
            "obs_observed_at": pd.to_datetime(matched["obs_observed_at"], utc=True).to_numpy(),
            "persistence_observed_at": pd.to_datetime(fc["persistence_observed_at"], utc=True),
            "analysis_observed_at": pd.to_datetime(fc["analysis_observed_at"], utc=True),
        }
    )
    out["obs_observed_at"] = pd.to_datetime(out["obs_observed_at"], utc=True)
    for name in FEATURE_NAMES:
        out[name] = feats[name].to_numpy()
    keep = out["obs_hs_m"].notna() & (out["lead_hours"] >= MIN_FORECAST_LEAD_H)
    out = out[keep].reset_index(drop=True)
    out[TARGET_NAME] = log_residual(out["obs_hs_m"], out["model_hs_m"])
    return out.sort_values(["cycle_time", "station_id", "lead_hours"], kind="stable").reset_index(
        drop=True
    )


def _empty_frame() -> pd.DataFrame:
    cols = [*ID_COLUMNS, *OBS_COLUMNS, *FEATURE_NAMES, TARGET_NAME]
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})


def _summary(
    frame: pd.DataFrame,
    forecasts: int,
    dropped: dict[str, int],
    hs_kinds: Sequence[str],
    sources: Sequence[str] | None,
) -> dict[str, Any]:
    if frame.empty:
        return {
            "rows": 0,
            "forecasts_read": forecasts,
            "dropped": dropped,
            "stations": [],
            "sources": list(sources or []),
            "features": list(FEATURE_NAMES),
            "target": f"{TARGET_NAME} = ln(obs_hs) - ln(model_hs), floor {HS_FLOOR_M} m",
            "hs_kinds": list(hs_kinds),
        }
    return {
        "rows": len(frame),
        "forecasts_read": forecasts,
        "dropped": dropped,
        "stations": sorted(frame["station_code"].unique().tolist()),
        "sources": sorted(frame["source_code"].unique().tolist()),
        "families": sorted(frame["family"].unique().tolist()),
        "cycles": int(frame["cycle_time"].nunique()),
        "period": {
            "cycle_start": frame["cycle_time"].min().isoformat(),
            "cycle_end": frame["cycle_time"].max().isoformat(),
            "valid_end": frame["valid_time"].max().isoformat(),
        },
        "lead_hours": [float(frame["lead_hours"].min()), float(frame["lead_hours"].max())],
        "features": list(FEATURE_NAMES),
        "feature_coverage": {n: round(float(frame[n].notna().mean()), 4) for n in FEATURE_NAMES},
        "target": f"{TARGET_NAME} = ln(obs_hs) - ln(model_hs), floor {HS_FLOOR_M} m",
        "hs_kinds": list(hs_kinds),
        "fingerprint": dataset_fingerprint(frame),
    }


def dataset_fingerprint(frame: pd.DataFrame) -> str:
    """Short hash of the row keys and target, to tell datasets apart in the registry."""
    h = hashlib.sha256()
    keys = frame[["station_code", "source_code", "cycle_time", "valid_time"]].astype(str)
    h.update("\n".join(keys.agg("|".join, axis=1)).encode())
    h.update(np.round(frame[TARGET_NAME].to_numpy(dtype=float), 6).tobytes())
    return h.hexdigest()[:16]


def save_dataset(dataset: Dataset, path: str | Path) -> Path:
    """Write the frame to Parquet (pyarrow) with the summary in the file metadata."""
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(dataset.frame, preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta[b"swell_dataset_summary"] = json.dumps(dataset.summary, default=str).encode()
    pq.write_table(table.replace_schema_metadata(meta), out)
    return out


def load_dataset(path: str | Path) -> Dataset:
    import json

    import pyarrow.parquet as pq

    table = pq.read_table(Path(path))
    meta = table.schema.metadata or {}
    summary = json.loads(meta.get(b"swell_dataset_summary", b"{}"))
    frame = table.to_pandas()
    for col in (
        "cycle_time",
        "valid_time",
        "obs_observed_at",
        "persistence_observed_at",
        "analysis_observed_at",
    ):
        if col in frame.columns:
            frame[col] = utc_times(frame[col])
    return Dataset(frame, summary)


# ------------------------------------------------------------------------------ guards
def assert_no_leakage(
    train: pd.DataFrame, test: pd.DataFrame, gap: timedelta, *, label: str = "train/test"
) -> dict[str, Any]:
    """Raise LeakageError if any training valid time is later than first test cycle - gap.

    The training targets are observations at the valid times; keeping them all before
    ``first test cycle - gap`` (gap >= max lead + 3 days) means no training observation
    falls inside, or right next to, the period being evaluated.
    """
    if train.empty or test.empty:
        raise InsufficientDataError(f"{label}: empty set (train={len(train)}, test={len(test)})")
    first_test_cycle = pd.Timestamp(test["cycle_time"].min())
    limit = first_test_cycle - pd.Timedelta(gap)
    late = train["valid_time"] > limit
    if bool(late.any()):
        raise LeakageError(
            f"{label}: {int(late.sum())} training rows have valid_time after "
            f"{limit.isoformat()} (first test cycle {first_test_cycle.isoformat()} minus gap "
            f"{gap}); latest is {pd.Timestamp(train['valid_time'].max()).isoformat()}"
        )
    return {
        "guard": "train_valid_time_before_first_test_cycle_minus_gap",
        "label": label,
        "gap_hours": gap.total_seconds() / 3600.0,
        "limit": limit.isoformat(),
        "train_valid_end": pd.Timestamp(train["valid_time"].max()).isoformat(),
        "test_cycle_start": first_test_cycle.isoformat(),
        "train_rows": len(train),
        "test_rows": len(test),
    }


def assert_causal_features(frame: pd.DataFrame) -> dict[str, Any]:
    """Raise LeakageError if an observation feature was taken after its cycle time, or a
    target observation at or before it (the target would then be a known input)."""
    checks: dict[str, int] = {}
    cycle = pd.to_datetime(frame["cycle_time"], utc=True)
    for col in ("persistence_observed_at", "analysis_observed_at"):
        if col not in frame.columns:
            continue
        times = pd.to_datetime(frame[col], utc=True)
        bad = times.notna() & (times > cycle)
        if bool(bad.any()):
            raise LeakageError(f"{int(bad.sum())} rows have {col} after cycle_time")
        checks[col] = int(times.notna().sum())
    if "obs_observed_at" in frame.columns:
        target_times = pd.to_datetime(frame["obs_observed_at"], utc=True)
        early = target_times.notna() & (target_times <= cycle)
        if bool(early.any()):
            raise LeakageError(
                f"{int(early.sum())} rows have a target observation at or before cycle_time"
            )
        checks["obs_observed_at"] = int(target_times.notna().sum())
    return {
        "guard": "features_at_or_before_cycle_time_targets_after",
        "checked_rows": checks,
    }


# ------------------------------------------------------------------------------ splits
@dataclass
class Split:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    gap: timedelta
    val_start: pd.Timestamp
    test_start: pd.Timestamp
    guards: list[dict[str, Any]] = field(default_factory=list)

    @property
    def development(self) -> pd.DataFrame:
        """Train + validation (refit set once hyperparameters are chosen)."""
        return pd.concat([self.train, self.validation], ignore_index=True)

    def describe(self) -> dict[str, Any]:
        def span(df: pd.DataFrame) -> dict[str, Any]:
            return {
                "rows": len(df),
                "cycles": int(df["cycle_time"].nunique()),
                "cycle_start": pd.Timestamp(df["cycle_time"].min()).isoformat(),
                "cycle_end": pd.Timestamp(df["cycle_time"].max()).isoformat(),
                "valid_end": pd.Timestamp(df["valid_time"].max()).isoformat(),
            }

        return {
            "method": "chronological train/validation/test, purged by valid time",
            "gap_hours": self.gap.total_seconds() / 3600.0,
            "val_start": self.val_start.isoformat(),
            "test_start": self.test_start.isoformat(),
            "train": span(self.train),
            "validation": span(self.validation),
            "test": span(self.test),
        }


def chronological_split(
    frame: pd.DataFrame,
    *,
    val_fraction: float = DEFAULT_VAL_FRACTION,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    gap: timedelta | None = None,
    val_start: datetime | None = None,
    test_start: datetime | None = None,
) -> Split:
    """Chronological train / validation / test split with purged gaps.

    Boundaries are placed on distinct cycle times (fractions of cycles, or explicit
    ``val_start``/``test_start``). Train = rows whose valid time is <= val_start - gap;
    validation = cycles >= val_start whose valid time is <= test_start - gap; test =
    cycles >= test_start. ``gap`` defaults to max lead + 3 days.
    """
    if frame.empty:
        raise InsufficientDataError("empty dataset")
    gap = gap or required_gap(float(frame["lead_hours"].max()))
    cycles = np.sort(frame["cycle_time"].unique())
    n = len(cycles)
    if test_start is None:
        test_start = pd.Timestamp(cycles[min(n - 1, int(np.floor(n * (1 - test_fraction))))])
    if val_start is None:
        val_start = pd.Timestamp(
            cycles[max(0, int(np.floor(n * (1 - test_fraction - val_fraction))))]
        )
    vs, ts = pd.Timestamp(val_start), pd.Timestamp(test_start)
    if not vs < ts:
        raise InsufficientDataError("validation must start before the test period")
    g = pd.Timedelta(gap)
    train = frame[frame["valid_time"] <= vs - g]
    validation = frame[(frame["cycle_time"] >= vs) & (frame["valid_time"] <= ts - g)]
    test = frame[frame["cycle_time"] >= ts]
    if train.empty or validation.empty or test.empty:
        raise InsufficientDataError(
            f"period too short for a chronological split with a {gap} gap: train={len(train)}"
            f", validation={len(validation)}, test={len(test)} rows over {n} cycles"
        )
    split = Split(
        train.reset_index(drop=True),
        validation.reset_index(drop=True),
        test.reset_index(drop=True),
        gap,
        vs,
        ts,
    )
    split.guards = [
        assert_no_leakage(split.train, split.validation, gap, label="train/validation"),
        assert_no_leakage(split.development, split.test, gap, label="train+validation/test"),
        assert_causal_features(frame),
    ]
    return split


@dataclass
class Fold:
    index: int
    train: pd.DataFrame
    test: pd.DataFrame
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    guard: dict[str, Any]


def rolling_origin_folds(frame: pd.DataFrame, k: int, gap: timedelta | None = None) -> list[Fold]:
    """Expanding-window rolling-origin evaluation with ``k`` folds by cycle date.

    The distinct cycles are cut into ``k + 1`` contiguous blocks; fold ``i`` (1..k)
    tests on block ``i`` and trains on every row whose valid time is <= block start -
    gap. Folds whose training set would be empty are skipped.
    """
    if k < 1 or frame.empty:
        return []
    gap = gap or required_gap(float(frame["lead_hours"].max()))
    cycles = np.sort(frame["cycle_time"].unique())
    blocks = [b for b in np.array_split(cycles, k + 1) if len(b)]
    folds: list[Fold] = []
    for i, block in enumerate(blocks[1:], start=1):
        start, end = pd.Timestamp(block[0]), pd.Timestamp(block[-1])
        test = frame[(frame["cycle_time"] >= start) & (frame["cycle_time"] <= end)]
        train = frame[frame["valid_time"] <= start - pd.Timedelta(gap)]
        if train.empty or test.empty:
            continue
        guard = assert_no_leakage(train, test, gap, label=f"rolling fold {i}")
        folds.append(
            Fold(i, train.reset_index(drop=True), test.reset_index(drop=True), start, end, guard)
        )
    return folds


def leave_one_station_out(split: Split) -> list[tuple[str, pd.DataFrame, pd.DataFrame]]:
    """(station, train, test): train on the other stations' development rows, test on the
    held-out station's test rows (chronology and gap preserved)."""
    dev = split.development
    out: list[tuple[str, pd.DataFrame, pd.DataFrame]] = []
    for code in sorted(split.test["station_code"].unique()):
        train = dev[dev["station_code"] != code]
        test = split.test[split.test["station_code"] == code]
        if train.empty or test.empty:
            continue
        assert_no_leakage(train, test, split.gap, label=f"leave-one-station-out {code}")
        out.append((str(code), train.reset_index(drop=True), test.reset_index(drop=True)))
    return out
