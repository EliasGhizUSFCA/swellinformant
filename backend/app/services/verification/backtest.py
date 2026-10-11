"""Rolling-origin backtest of a forecast source against buoy observations.

The source's archived point forecasts (app.services.ml.dataset) are evaluated over K
expanding-window folds by cycle date (app.services.ml.dataset.rolling_origin_folds,
gap = max lead + 3 days between each fold's training data and its test block). The raw
model is scored on every fold's test block against two references: persistence (the
observation at or before the cycle time, <= 1 h old) and climatology (the monthly mean
of the station's observations in that fold's TRAINING rows only). Each row of the
resulting table is one lead bucket (app.services.verification.metrics) with n, bias,
RMSE, SI, HH, CC and MSE skill vs persistence and climatology; RMSE and skill scores
carry cycle-block bootstrap confidence intervals (whole forecast cycles resampled).

Results are written to ``forecast_accuracy_metrics`` with ``scope='global'``,
``scope_key='backtest'`` (or ``'backtest:<hash>'`` for a station subset) and one row
per model version ``raw`` / ``persistence`` / ``climatology`` and lead bucket.

Targets are spectral Hm0 observations by default (like with like: the model value is an
Hm0). ``hs_kind='h_third'`` backtests against time-domain H1/3 instead (some buoys, e.g.
the AODN real-time feeds, report only H1/3); those rows are stored under the variable
``hs_h13`` so they never mix with the Hm0 statistics.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.services.ml.baselines import Climatology, add_reference_predictions
from app.services.ml.dataset import (
    InsufficientDataError,
    build_dataset,
    required_gap,
    rolling_origin_folds,
)
from app.services.verification import metrics as vm
from app.services.verification.jobs import HS_VARIABLE_BY_KIND, upsert_metrics

BACKTEST_CONFIDENCE = 0.90
REFERENCES = ("persistence", "climatology")


@dataclass
class BacktestResult:
    source: str
    hs_kind: str
    stations: list[str]
    gap_hours: float
    folds: list[dict[str, Any]]
    table: list[dict[str, Any]]
    references: dict[str, list[dict[str, Any]]]
    period_start: datetime
    period_end: datetime
    rows: int
    n_boot: int
    metrics_written: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "hs_kind": self.hs_kind,
            "stations": self.stations,
            "gap_hours": self.gap_hours,
            "folds": self.folds,
            "table": self.table,
            "references": self.references,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "rows": self.rows,
            "n_boot": self.n_boot,
            "metrics_written": self.metrics_written,
            "notes": self.notes,
        }


def _ci(ci: vm.BootstrapCI) -> list[float | None]:
    return [vm.finite_or_none(ci.lower), vm.finite_or_none(ci.upper)]


def bucket_row(
    frame: pd.DataFrame,
    pred_col: str,
    bucket: int,
    *,
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    """Scores of ``frame[pred_col]`` in one lead bucket, with bootstrap CIs."""
    sub = frame[vm.in_lead_bucket(frame["lead_hours"].to_numpy(dtype=float), bucket)]
    m = sub[pred_col].to_numpy(dtype=float)
    o = sub["obs_hs_m"].to_numpy(dtype=float)
    blocks = pd.to_datetime(sub["cycle_time"], utc=True).astype("int64").to_numpy()
    scores = vm.continuous_scores(m, o)
    kwargs = {"n_boot": n_boot, "confidence": BACKTEST_CONFIDENCE, "seed": seed}
    row: dict[str, Any] = {
        "bucket": "all" if bucket == vm.ALL_LEADS_BUCKET else bucket,
        "lead_bucket_h": bucket,
        "n": int(scores["n"]),
        "cycles": int(sub["cycle_time"].nunique()),
        **{k: vm.finite_or_none(v) for k, v in scores.items() if k != "n"},
        "rmse_ci": _ci(vm.rmse_ci(m, o, blocks, **kwargs)) if len(sub) else [None, None],
    }
    for ref in REFERENCES:
        if pred_col == f"pred_{ref}":
            continue
        r = sub[f"pred_{ref}"].to_numpy(dtype=float)
        n_ref = int(vm.finite_pairs(m, o, r)[0].size)
        row[f"n_{ref}"] = n_ref
        row[f"skill_vs_{ref}"] = vm.finite_or_none(vm.skill_score(m, o, r)) if n_ref else None
        row[f"skill_vs_{ref}_ci"] = (
            _ci(vm.skill_score_ci(m, o, r, blocks, **kwargs)) if n_ref else [None, None]
        )
    return row


def _scope_key(station_codes: Sequence[str] | None) -> str:
    if not station_codes:
        return "backtest"
    digest = hashlib.sha1("|".join(sorted(station_codes)).encode(), usedforsecurity=False)
    return f"backtest:{digest.hexdigest()[:12]}"


def run_backtest(
    db: Session,
    *,
    source: str,
    start: datetime | None = None,
    end: datetime | None = None,
    folds: int = 5,
    station_codes: Sequence[str] | None = None,
    n_boot: int = vm.DEFAULT_N_BOOT,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
    write: bool = True,
    hs_kind: str = "spectral_hm0",
) -> BacktestResult:
    """Backtest the raw model of ``source`` (``start``/``end`` bound the cycle time)."""
    if hs_kind not in HS_VARIABLE_BY_KIND:
        raise ValueError(f"unknown hs_kind {hs_kind!r}; choose from {sorted(HS_VARIABLE_BY_KIND)}")
    dataset = build_dataset(
        db,
        sources=[source],
        station_codes=station_codes,
        start=start,
        end=end,
        hs_kinds=(hs_kind,),
    )
    frame = dataset.frame
    if frame.empty:
        raise InsufficientDataError(
            f"no paired forecasts/{hs_kind} observations for source {source!r} in the period"
        )
    gap = required_gap(float(frame["lead_hours"].max()))
    fold_list = rolling_origin_folds(frame, folds, gap)
    if not fold_list:
        raise InsufficientDataError(
            f"period too short for {folds} rolling-origin folds with a {gap} gap"
        )
    pieces = []
    fold_info = []
    for fold in fold_list:
        clim = Climatology.fit(fold.train)
        test = add_reference_predictions(fold.test, clim)
        test["fold"] = fold.index
        pieces.append(test)
        fold_info.append(
            {
                "fold": fold.index,
                "test_start": fold.test_start.isoformat(),
                "test_end": fold.test_end.isoformat(),
                "train_rows": len(fold.train),
                "test_rows": len(fold.test),
                "climatology": clim.describe(),
                "leakage_guard": fold.guard,
            }
        )
    tested = pd.concat(pieces, ignore_index=True)
    buckets = (vm.ALL_LEADS_BUCKET, *vm.LEAD_BUCKETS_H)
    table = [bucket_row(tested, "pred_raw", b, n_boot=n_boot, seed=seed) for b in buckets]
    references = {
        ref: [bucket_row(tested, f"pred_{ref}", b, n_boot=n_boot, seed=seed) for b in buckets]
        for ref in REFERENCES
    }
    result = BacktestResult(
        source=source,
        hs_kind=hs_kind,
        stations=sorted(tested["station_code"].unique().tolist()),
        gap_hours=gap.total_seconds() / 3600.0,
        folds=fold_info,
        table=table,
        references=references,
        period_start=pd.Timestamp(tested["cycle_time"].min()).to_pydatetime(),
        period_end=pd.Timestamp(tested["valid_time"].max()).to_pydatetime(),
        rows=len(tested),
        n_boot=n_boot,
    )
    if write:
        result.metrics_written = _write(db, result, station_codes)
        db.commit()
    return result


def _write(db: Session, result: BacktestResult, station_codes: Sequence[str] | None) -> int:
    computed_at = utcnow()
    rows = []
    for model_version, table in (("raw", result.table), *result.references.items()):
        for r in table:
            if not r["n"]:
                continue
            extra = {
                "kind": "backtest",
                "hs_kind": result.hs_kind,
                "folds": len(result.folds),
                "gap_hours": result.gap_hours,
                "stations": result.stations,
                "cycles": r["cycles"],
                "nrmse": r.get("nrmse"),
                "rmse_ci": r["rmse_ci"],
                "skill_vs_persistence_ci": r.get("skill_vs_persistence_ci"),
                "skill_vs_climatology_ci": r.get("skill_vs_climatology_ci"),
                "n_persistence": r.get("n_persistence"),
                "n_climatology": r.get("n_climatology"),
                "confidence": BACKTEST_CONFIDENCE,
                "n_boot": result.n_boot,
            }
            rows.append(
                {
                    "scope": "global",
                    "scope_key": _scope_key(station_codes),
                    "source_code": result.source,
                    "model_version": model_version,
                    "variable": HS_VARIABLE_BY_KIND[result.hs_kind],
                    "lead_bucket_h": int(r["lead_bucket_h"]),
                    "period_start": result.period_start,
                    "period_end": result.period_end,
                    "n": int(r["n"]),
                    "bias": r.get("bias"),
                    "mae": r.get("mae"),
                    "rmse": r.get("rmse"),
                    "scatter_index": r.get("si"),
                    "hh": r.get("hh"),
                    "correlation": r.get("cc"),
                    "skill_vs_persistence": r.get("skill_vs_persistence"),
                    "skill_vs_climatology": r.get("skill_vs_climatology"),
                    "extra": extra,
                    "computed_at": computed_at,
                }
            )
    return upsert_metrics(db, rows)


def _fmt(value: Any, spec: str = ".3f") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    return format(value, spec)


def _fmt_ci(estimate: Any, ci: Sequence[Any] | None, spec: str = ".3f") -> str:
    if estimate is None:
        return "-"
    if not ci or ci[0] is None or ci[1] is None:
        return _fmt(estimate, spec)
    return f"{format(estimate, spec)} [{format(ci[0], spec)},{format(ci[1], spec)}]"


def format_table(result: BacktestResult) -> str:
    """Plain-text per-lead table (raw model; skill vs persistence and climatology)."""
    pct = int(BACKTEST_CONFIDENCE * 100)
    header = (
        f"{'lead':>5} {'n':>6} {'bias':>7} {'RMSE [' + str(pct) + '% CI]':>26} {'SI':>6} "
        f"{'HH':>6} {'CC':>6} {'SS_pers [CI]':>26} {'SS_clim [CI]':>26}"
    )
    lines = [
        f"Backtest {result.source}: {len(result.folds)} rolling-origin folds, gap "
        f"{result.gap_hours:.0f} h, {result.rows} pairs, stations {', '.join(result.stations)}",
        f"Test period {result.period_start:%Y-%m-%d} .. {result.period_end:%Y-%m-%d}; "
        f"model Hs vs observed {result.hs_kind} in m; bootstrap resamples whole cycles "
        f"({result.n_boot} replicates).",
        header,
    ]
    for r in result.table:
        lines.append(
            f"{str(r['bucket']):>5} {r['n']:>6} {_fmt(r['bias'], '+.3f'):>7} "
            f"{_fmt_ci(r['rmse'], r['rmse_ci']):>26} {_fmt(r['si']):>6} {_fmt(r['hh']):>6} "
            f"{_fmt(r['cc']):>6} "
            f"{_fmt_ci(r.get('skill_vs_persistence'), r.get('skill_vs_persistence_ci')):>26} "
            f"{_fmt_ci(r.get('skill_vs_climatology'), r.get('skill_vs_climatology_ci')):>26}"
        )
    lines.append(
        "RMSE and SI favour negatively biased models; read them with bias and HH. "
        "n_persistence / n_climatology (reference sample sizes) are in the JSON output."
    )
    return "\n".join(lines)
