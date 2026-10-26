#!/usr/bin/env python
"""Per-lead evaluation tables with bootstrap CIs for one recorded ``ml train`` experiment.

``ml train`` stores point scores per lead bucket; docs/ml-evaluation.md also reports
cycle-block bootstrap confidence intervals per bucket and a per-station breakdown. This
script recomputes them from the same inputs, without changing the registry:

* the dataset (``ml build-dataset --out``) and the chronological split are rebuilt
  with the defaults ``train_candidates`` uses; the split boundaries are checked against
  the experiment record;
* TEST predictions come from each version's verified artifact (the model refitted on
  train + validation, exactly as evaluated by the deployment criteria);
* VALIDATION predictions come from refitting the selected setting on the training rows
  with the recorded metre scale (the fits are seeded, so this reproduces the selection
  run); the recorded validation and test RMSE are checked against the recomputation.

Optional supplementary reference (``--lt-climatology-before ISO``): a long-term monthly
climatology per station, the mean of the station's QC'd (``good``) spectral Hm0
observations before ISO with at least MIN_CLIMATOLOGY_OBS per station and month. It uses
no observation after ISO, so with ISO at or before the first cycle of the dataset it is
out of sample for every row. The pipeline's own climatology is fitted on the training
rows only; with less than a year of training data it covers few test months.
``--raw-from ISO`` adds tables of the raw model against that long-term climatology over
every dataset row whose cycle is at or after ISO (e.g. the backtest's test span).

Usage (from the repository root, with DATABASE_URL and ML_ARTIFACT_DIR as for training)::

    PYTHONPATH=backend python scripts/ml_evaluation_report.py EXPERIMENT_ID DATASET.parquet \
        [--boot 1000] [--block-hours 0] [--lt-climatology-before ISO [--raw-from ISO]] \
        [--json OUT.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import BuoyObservation, MLExperiment, MLModelVersion
from app.services.ml.baselines import (
    MIN_CLIMATOLOGY_OBS,
    Climatology,
    add_reference_predictions,
)
from app.services.ml.dataset import chronological_split, load_dataset
from app.services.ml.models import fit_correction
from app.services.ml.registry import load_artifact
from app.services.verification import metrics as vm

CONFIDENCE = 0.90
REFERENCES = ("raw", "persistence", "climatology")
LT_REFERENCE = "climatology_lt"
NON_PARAMS = ("metre_scale", "factor_clamp", "hs_floor_m", "seed", "unused_features")


def _f(value: float) -> float | None:
    return vm.finite_or_none(value)


def _iso(value: str) -> datetime:
    t = pd.Timestamp(value)
    return (t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")).to_pydatetime()


def bootstrap_blocks(frame: pd.DataFrame, block_hours: float) -> np.ndarray:
    """Bootstrap block label per row: the forecast cycle, or ``block_hours`` windows of
    cycles (consecutive cycles share weather, so per-cycle blocks are optimistic)."""
    # Whole seconds since the epoch: the frames hold microsecond timestamps, so a plain
    # astype("int64") would not be in nanoseconds.
    secs = pd.to_datetime(frame["cycle_time"], utc=True).dt.as_unit("s").astype("int64")
    values = secs.to_numpy()
    if block_hours <= 0:
        return values
    return values // int(block_hours * 3600)


def scores(
    frame: pd.DataFrame,
    pred: np.ndarray,
    refs: tuple[str, ...],
    *,
    n_boot: int,
    seed: int,
    block_hours: float = 0.0,
) -> dict[str, Any]:
    """Per-bucket scores of ``pred``: RMSE and RMSE(pred) - RMSE(raw) with CIs, and MSE
    skill vs each reference (with CI) on the rows where that reference exists."""
    obs = frame["obs_hs_m"].to_numpy(dtype=float)
    raw = frame["pred_raw"].to_numpy(dtype=float)
    lead = frame["lead_hours"].to_numpy(dtype=float)
    blocks = bootstrap_blocks(frame, block_hours)
    out: dict[str, Any] = {}
    kw = {"n_boot": n_boot, "confidence": CONFIDENCE, "seed": seed}
    for label, bucket in (("all", vm.ALL_LEADS_BUCKET), *((str(b), b) for b in vm.LEAD_BUCKETS_H)):
        mask = vm.in_lead_bucket(lead, bucket) & np.isfinite(pred) & np.isfinite(obs)
        p, o, r, b = pred[mask], obs[mask], raw[mask], blocks[mask]
        row: dict[str, Any] = {k: _f(v) for k, v in vm.continuous_scores(p, o).items()}
        row["n"] = int(p.size)
        row["cycles"] = int(np.unique(b).size)
        if p.size:
            ci = vm.rmse_ci(p, o, b, **kw)
            row["rmse_ci"] = [_f(ci.lower), _f(ci.upper)]
            diff = vm.rmse_difference_ci(p, r, o, b, **kw)
            row["rmse_minus_raw"] = _f(diff.estimate)
            row["rmse_minus_raw_ci"] = [_f(diff.lower), _f(diff.upper)]
        for ref in refs:
            ref_values = frame[f"pred_{ref}"].to_numpy(dtype=float)[mask]
            has = np.isfinite(ref_values)
            n_ref = int(has.sum())
            row[f"n_{ref}"] = n_ref
            row[f"skill_vs_{ref}"] = None
            row[f"skill_vs_{ref}_ci"] = [None, None]
            if n_ref >= vm.MIN_REFERENCE_PAIRS:
                row[f"skill_vs_{ref}"] = _f(vm.skill_score(p, o, ref_values))
                sci = vm.skill_score_ci(p[has], o[has], ref_values[has], b[has], **kw)
                row[f"skill_vs_{ref}_ci"] = [_f(sci.lower), _f(sci.upper)]
        out[label] = row
    return out


def per_station(frame: pd.DataFrame, preds: dict[str, np.ndarray]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    obs = frame["obs_hs_m"].to_numpy(dtype=float)
    for code in sorted(frame["station_code"].unique()):
        sel = (frame["station_code"] == code).to_numpy()
        row: dict[str, Any] = {"n": int(sel.sum())}
        for name, pred in preds.items():
            ok = sel & np.isfinite(pred)
            row[f"n_{name}"] = int(ok.sum())
            row[f"rmse_{name}"] = _f(vm.rmse(pred[ok], obs[ok]))
            row[f"bias_{name}"] = _f(vm.bias(pred[ok], obs[ok]))
            row[f"hh_{name}"] = _f(vm.hanna_heinold(pred[ok], obs[ok]))
        out[str(code)] = row
    return out


def long_term_climatology(
    db: Session, station_ids: list[int], before: datetime
) -> dict[tuple[int, int], tuple[float, int, int]]:
    """(station, month) -> (mean Hm0, observations, distinct years), QC'd data before
    ``before`` only, months with fewer than MIN_CLIMATOLOGY_OBS observations left out."""
    o = BuoyObservation
    month = func.extract("month", o.observed_at)
    year = func.extract("year", o.observed_at)
    rows = db.execute(
        select(o.station_id, month, func.avg(o.hs_m), func.count(), func.count(year.distinct()))
        .where(
            o.station_id.in_(station_ids),
            o.observed_at < before,
            o.qc_flag == "good",
            o.hs_kind == "spectral_hm0",
            o.hs_m.isnot(None),
        )
        .group_by(o.station_id, month)
    ).all()
    return {
        (int(sid), int(m)): (float(mean), int(n), int(years))
        for sid, m, mean, n, years in rows
        if int(n) >= MIN_CLIMATOLOGY_OBS
    }


def add_lt_climatology(
    frame: pd.DataFrame, table: dict[tuple[int, int], tuple[float, int, int]]
) -> pd.DataFrame:
    out = frame.copy()
    months = pd.to_datetime(out["valid_time"], utc=True).dt.month.to_numpy()
    out[f"pred_{LT_REFERENCE}"] = [
        table.get((int(s), int(m)), (np.nan, 0, 0))[0]
        for s, m in zip(out["station_id"].to_numpy(), months, strict=True)
    ]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("experiment_id", type=int)
    parser.add_argument("dataset")
    parser.add_argument("--boot", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=vm.DEFAULT_BOOTSTRAP_SEED)
    parser.add_argument(
        "--block-hours",
        type=float,
        default=0.0,
        help="bootstrap blocks of this many hours of cycles (default 0: one block per cycle, "
        "as in the deployment criteria)",
    )
    parser.add_argument("--lt-climatology-before", type=_iso, default=None)
    parser.add_argument("--raw-from", type=_iso, default=None)
    parser.add_argument("--json", default=None, help="write the tables to this JSON file")
    args = parser.parse_args(argv)

    ds = load_dataset(args.dataset)
    split = chronological_split(ds.frame)
    val = add_reference_predictions(split.validation, Climatology.fit(split.train))
    test = add_reference_predictions(split.test, Climatology.fit(split.development))
    refs: tuple[str, ...] = REFERENCES
    report: dict[str, Any] = {
        "experiment_id": args.experiment_id,
        "dataset_fingerprint": ds.summary.get("fingerprint"),
        "split": split.describe(),
        "confidence": CONFIDENCE,
        "n_boot": args.boot,
        "block_hours": args.block_hours,
    }
    kw = {"n_boot": args.boot, "seed": args.seed, "block_hours": args.block_hours}
    with SessionLocal() as db:
        experiment = db.get(MLExperiment, args.experiment_id)
        if experiment is None:
            print(f"no experiment {args.experiment_id}", file=sys.stderr)
            return 1
        recorded = experiment.split or {}
        for key in ("val_start", "test_start"):
            if pd.Timestamp(recorded.get(key)) != pd.Timestamp(report["split"][key]):
                print(f"split mismatch on {key}: {recorded.get(key)}", file=sys.stderr)
                return 1
        if args.lt_climatology_before is not None:
            station_ids = sorted({int(s) for s in ds.frame["station_id"]})
            table = long_term_climatology(db, station_ids, args.lt_climatology_before)
            report["lt_climatology"] = {
                "before": args.lt_climatology_before.isoformat(),
                "entries": {f"{s}:{m}": list(v) for (s, m), v in sorted(table.items())},
            }
            val, test = add_lt_climatology(val, table), add_lt_climatology(test, table)
            refs = (*REFERENCES, LT_REFERENCE)
            if args.raw_from is not None:
                frame = ds.frame[ds.frame["cycle_time"] >= pd.Timestamp(args.raw_from)]
                frame = add_lt_climatology(frame.reset_index(drop=True), table)
                frame["pred_raw"] = frame["model_hs_m"].to_numpy(dtype=float)
                raw_pred = frame["pred_raw"].to_numpy(dtype=float)
                lt_pred = frame[f"pred_{LT_REFERENCE}"].to_numpy(dtype=float)
                report["raw_vs_lt_climatology"] = {
                    "from": args.raw_from.isoformat(),
                    "pooled": {
                        "raw": scores(frame, raw_pred, ("raw", LT_REFERENCE), **kw),
                        LT_REFERENCE: scores(frame, lt_pred, ("raw",), **kw),
                    },
                    "by_station": {
                        str(code): scores(
                            sub.reset_index(drop=True),
                            sub["pred_raw"].to_numpy(dtype=float),
                            ("raw", LT_REFERENCE),
                            **kw,
                        )
                        for code, sub in frame.groupby("station_code")
                    },
                }
        features = tuple(experiment.dataset.get("features_used") or ())
        versions = list(
            db.scalars(
                select(MLModelVersion)
                .where(MLModelVersion.experiment_id == experiment.id)
                .order_by(MLModelVersion.id)
            )
        )
        val_preds = {ref: val[f"pred_{ref}"].to_numpy(dtype=float) for ref in refs}
        test_preds = {ref: test[f"pred_{ref}"].to_numpy(dtype=float) for ref in refs}
        val_obs = val["obs_hs_m"].to_numpy(dtype=float)
        test_obs = test["obs_hs_m"].to_numpy(dtype=float)
        checks: dict[str, Any] = {}
        for v in versions:
            hp = dict(v.hyperparameters or {})
            params = {k: value for k, value in hp.items() if k not in NON_PARAMS}
            scale = float(hp.get("metre_scale", 1.0))
            refit = fit_correction(v.model_type, params, split.train, features, metre_scale=scale)
            val_preds[v.model_type] = refit.correct(val)
            test_preds[v.model_type] = load_artifact(v).correct(test)
            checks[v.label] = {
                "validation_rmse_recorded": (v.metrics or {})["validation"]["all"]["rmse"],
                "validation_rmse_refit": _f(vm.rmse(val_preds[v.model_type], val_obs)),
                "test_rmse_recorded": (v.metrics or {})["test"]["all"]["rmse"],
                "test_rmse_artifact": _f(vm.rmse(test_preds[v.model_type], test_obs)),
                "status": v.status.value,
                "failed": (v.decision or {}).get("failed"),
            }
    report["consistency"] = checks
    report["validation"] = {k: scores(val, p, refs, **kw) for k, p in val_preds.items()}
    report["test"] = {k: scores(test, p, refs, **kw) for k, p in test_preds.items()}
    report["test_by_station"] = per_station(test, test_preds)
    report["validation_by_station"] = per_station(val, val_preds)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str))
    for part in ("validation", "test"):
        print(f"\n{part.upper()} (Hs RMSE m [{int(CONFIDENCE * 100)}% CI], n)")
        for name, table in report[part].items():
            cells = []
            for label, row in table.items():
                if not row["n"]:
                    cells.append(f"{label}: -")
                    continue
                lo, hi = row.get("rmse_ci", [None, None])
                ci = f" [{lo:.3f},{hi:.3f}]" if lo is not None and hi is not None else ""
                cells.append(f"{label}: {row['rmse']:.3f}{ci} n={row['n']}")
            print(f"  {name:<14} " + "; ".join(cells))
    print("\nconsistency:", json.dumps(checks, indent=1))
    return 0 if all(_consistent(c) for c in checks.values()) else 2


def _consistent(check: dict[str, Any]) -> bool:
    pairs = (
        (check["validation_rmse_recorded"], check["validation_rmse_refit"]),
        (check["test_rmse_recorded"], check["test_rmse_artifact"]),
    )
    return all(
        a is not None and b is not None and math.isclose(a, b, abs_tol=1e-6) for a, b in pairs
    )


if __name__ == "__main__":
    raise SystemExit(main())
