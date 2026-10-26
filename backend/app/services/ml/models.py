"""Candidate corrections, held-out scoring and the training procedure.

Candidates (all predict the log residual ``ln(obs) - ln(model)``; research notes, ML
section: MOS-style post-processing, gradient boosting first, a ridge baseline it must
beat):

* ``raw``: identity (factor 1), the reference every candidate must beat.
* ``linear``: ridge regression on the features plus lead interactions (lead², lead x
  ln Hs); missing values are median-imputed with indicator columns and features are
  standardised, both fitted on training rows only (inside the pipeline).
* ``rf``: sklearn RandomForestRegressor, modest size.
* ``gbt``: sklearn HistGradientBoostingRegressor (native missing-value handling).

Procedure (:func:`train_candidates`): chronological split with purged gaps
(app.services.ml.dataset); for each candidate every hyperparameter setting of its small
fixed grid is fitted on TRAIN and scored on VALIDATION (Hs RMSE in metres after the
back-transform, clamps and metre scale); the best setting is refitted on train +
validation and scored ONCE on the TEST period. Rolling-origin folds (expanding window)
on the development period and, optionally, leave-one-station-out runs are reported for
robustness; they never feed back into selection. Seeds are fixed.

Back-transform: corrected Hs = model Hs x clamp(exp(ŷ) x s, 0.5, 2.0). ``s`` is a single
least-squares scale in metres, s = Σ(o·p) / Σ(p²) on the validation rows (p = model x
exp(ŷ)), clamped to [0.9, 1.1]: exp(ŷ) of a log-space fit estimates a median ratio,
while the deployment criteria judge RMSE in metres. ``s`` is estimated on validation,
never on test. Rows whose lead is outside the trained range, or that miss a required
feature, keep the raw value, exactly as at apply time.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.services.ml.baselines import Climatology, add_reference_predictions
from app.services.ml.dataset import (
    Dataset,
    InsufficientDataError,
    Split,
    assert_no_leakage,
    chronological_split,
    leave_one_station_out,
    rolling_origin_folds,
)
from app.services.ml.features import FEATURE_NAMES, OBSERVATION_FEATURES, TARGET_NAME
from app.services.verification import metrics as vm

logger = logging.getLogger(__name__)

SEED = 20261010
FACTOR_MIN = 0.5
FACTOR_MAX = 2.0
METRE_SCALE_LIMITS = (0.9, 1.1)
# A feature present in at least this fraction of training rows is required at apply time
# (the model never learned how to do without it); rarer features may be missing.
REQUIRED_FEATURE_COVERAGE = 0.99
ALWAYS_REQUIRED = ("lead_hours", "model_hs_m")
MIN_TRAIN_ROWS = 200
RF_N_JOBS = 2
CANDIDATE_KINDS = ("linear", "rf", "gbt")
REFERENCE_NAMES = ("raw", "persistence", "climatology")

HYPERPARAMETER_GRIDS: dict[str, list[dict[str, Any]]] = {
    "linear": [{"alpha": a} for a in (0.1, 1.0, 10.0, 100.0)],
    "rf": [
        {
            "n_estimators": 80,
            "max_depth": 12,
            "min_samples_leaf": leaf,
            "max_features": 0.5,
            "max_samples": 0.5,
        }
        for leaf in (10, 40)
    ],
    "gbt": [
        {
            "learning_rate": lr,
            "max_leaf_nodes": leaves,
            "max_iter": 200,
            "min_samples_leaf": 40,
            "l2_regularization": 1.0,
        }
        for lr in (0.05, 0.1)
        for leaves in (15, 31)
    ],
}


class LeadInteractions(BaseEstimator, TransformerMixin):
    """Append (lead/240)² and (lead/240)·ln(Hs) columns for the ridge baseline."""

    def __init__(self, lead_index: int = 0, log_hs_index: int = 2) -> None:
        self.lead_index = lead_index
        self.log_hs_index = log_hs_index

    def fit(self, X: Any, y: Any = None) -> LeadInteractions:
        return self

    def transform(self, X: Any) -> np.ndarray:
        arr = np.asarray(X, dtype=float)
        lead = arr[:, self.lead_index] / 240.0
        return np.column_stack([arr, lead**2, lead * arr[:, self.log_hs_index]])


def make_estimator(
    kind: str, params: dict[str, Any], feature_names: Sequence[str], seed: int = SEED
) -> Any:
    names = list(feature_names)
    if kind == "linear":
        return Pipeline(
            [
                (
                    "impute",
                    SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                ),
                (
                    "lead",
                    LeadInteractions(names.index("lead_hours"), names.index("log_model_hs")),
                ),
                ("scale", StandardScaler()),
                ("ridge", Ridge(alpha=float(params["alpha"]))),
            ]
        )
    if kind == "rf":
        return RandomForestRegressor(random_state=seed, n_jobs=RF_N_JOBS, **params)
    if kind == "gbt":
        return HistGradientBoostingRegressor(random_state=seed, early_stopping=False, **params)
    raise ValueError(f"unknown candidate kind {kind!r}")


@dataclass
class TrainedCorrection:
    """A fitted residual model plus everything needed to apply it exactly as evaluated."""

    kind: str
    params: dict[str, Any]
    feature_names: tuple[str, ...]
    estimator: Any | None
    required_features: tuple[str, ...]
    lead_min: float
    lead_max: float
    metre_scale: float = 1.0
    factor_min: float = FACTOR_MIN
    factor_max: float = FACTOR_MAX
    trained_rows: int = 0
    # Candidate features with no value at all in the training rows (e.g. wind for a
    # source without wind); they carry no information and are left out of the model.
    unused_features: tuple[str, ...] = ()

    def matrix(self, frame: pd.DataFrame) -> np.ndarray:
        return frame[list(self.feature_names)].to_numpy(dtype=float)

    def factors(self, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(multiplicative factors, applied mask) for a feature matrix.

        Rows outside the trained lead range or missing a required feature get factor 1
        (raw value unchanged); never extrapolated.
        """
        X = np.asarray(X, dtype=float)
        idx = {n: i for i, n in enumerate(self.feature_names)}
        lead = X[:, idx["lead_hours"]]
        applied = (lead >= self.lead_min - 1e-9) & (lead <= self.lead_max + 1e-9)
        for name in self.required_features:
            applied &= np.isfinite(X[:, idx[name]])
        out = np.ones(X.shape[0], dtype=float)
        if self.estimator is not None and applied.any():
            pred = np.asarray(self.estimator.predict(X[applied]), dtype=float)
            factor = np.exp(np.clip(pred, -10.0, 10.0)) * self.metre_scale
            out[applied] = np.clip(factor, self.factor_min, self.factor_max)
        return out, applied

    def correct(self, frame: pd.DataFrame) -> np.ndarray:
        """Corrected Hs (m) for every row of a dataset-style frame."""
        if frame.empty:
            return np.zeros(0)
        factors, _ = self.factors(self.matrix(frame))
        return frame["model_hs_m"].to_numpy(dtype=float) * factors

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "params": self.params,
            "feature_names": list(self.feature_names),
            "required_features": list(self.required_features),
            "lead_range_h": [self.lead_min, self.lead_max],
            "metre_scale": self.metre_scale,
            "factor_clamp": [self.factor_min, self.factor_max],
            "trained_rows": self.trained_rows,
            "unused_features": list(self.unused_features),
        }


def required_features(train: pd.DataFrame, feature_names: Sequence[str]) -> tuple[str, ...]:
    req = [
        n
        for n in feature_names
        if n in ALWAYS_REQUIRED or float(train[n].notna().mean()) >= REQUIRED_FEATURE_COVERAGE
    ]
    return tuple(req)


def fit_correction(
    kind: str,
    params: dict[str, Any],
    train: pd.DataFrame,
    feature_names: Sequence[str],
    *,
    seed: int = SEED,
    metre_scale: float = 1.0,
) -> TrainedCorrection:
    if len(train) < MIN_TRAIN_ROWS:
        raise InsufficientDataError(f"{kind}: only {len(train)} training rows")
    # A feature that is missing in every training row is dropped (histogram gradient
    # boosting cannot bin an all-NaN column, and no model can learn from it).
    names = tuple(n for n in feature_names if bool(train[n].notna().any()))
    unused = tuple(n for n in feature_names if n not in names)
    missing_core = [n for n in ALWAYS_REQUIRED if n not in names]
    if missing_core:
        raise InsufficientDataError(f"{kind}: no values for {missing_core} in the training rows")
    estimator = make_estimator(kind, params, names, seed)
    estimator.fit(train[list(names)].to_numpy(dtype=float), train[TARGET_NAME].to_numpy(float))
    return TrainedCorrection(
        kind=kind,
        params=dict(params),
        feature_names=names,
        estimator=estimator,
        required_features=required_features(train, names),
        lead_min=float(train["lead_hours"].min()),
        lead_max=float(train["lead_hours"].max()),
        metre_scale=metre_scale,
        trained_rows=len(train),
        unused_features=unused,
    )


def least_squares_scale(obs: np.ndarray, pred: np.ndarray) -> float:
    """s = Σ(o·p) / Σ(p²), clamped to METRE_SCALE_LIMITS (1.0 when undefined)."""
    o, p = vm.finite_pairs(obs, pred)
    denom = float(np.sum(p**2))
    if not o.size or denom <= 0:
        return 1.0
    lo, hi = METRE_SCALE_LIMITS
    return float(np.clip(float(np.sum(o * p)) / denom, lo, hi))


# ------------------------------------------------------------------------------ scoring
def bucket_labels() -> list[tuple[str, int]]:
    return [("all", vm.ALL_LEADS_BUCKET), *((str(b), b) for b in vm.LEAD_BUCKETS_H)]


def score_hs(frame: pd.DataFrame, pred: np.ndarray) -> dict[str, dict[str, float | None]]:
    """Hs scores of ``pred`` per lead bucket ("all" = every lead), with references.

    ``frame`` needs obs_hs_m, lead_hours, pred_raw, pred_persistence and
    pred_climatology (see baselines.add_reference_predictions). Skill vs each reference
    is computed on the rows where that reference exists (``n_<ref>`` reports how many)
    and only when there are at least ``MIN_REFERENCE_PAIRS`` of them.
    """
    obs = frame["obs_hs_m"].to_numpy(dtype=float)
    lead = frame["lead_hours"].to_numpy(dtype=float)
    refs = {
        "raw": frame["pred_raw"].to_numpy(dtype=float),
        "persistence": frame["pred_persistence"].to_numpy(dtype=float),
        "climatology": frame["pred_climatology"].to_numpy(dtype=float),
    }
    pred = np.asarray(pred, dtype=float)
    out: dict[str, dict[str, float | None]] = {}
    for label, bucket in bucket_labels():
        mask = vm.in_lead_bucket(lead, bucket)
        p, o = pred[mask], obs[mask]
        scores = vm.continuous_scores(p, o)
        row: dict[str, float | None] = {k: vm.finite_or_none(v) for k, v in scores.items()}
        rmse_raw = vm.rmse(refs["raw"][mask], o)
        row["rmse_raw"] = vm.finite_or_none(rmse_raw)
        row["hh_raw"] = vm.finite_or_none(vm.hanna_heinold(refs["raw"][mask], o))
        row["bias_raw"] = vm.finite_or_none(vm.bias(refs["raw"][mask], o))
        row["rmse_change_vs_raw"] = vm.finite_or_none(
            scores["rmse"] / rmse_raw - 1.0 if rmse_raw and rmse_raw > 0 else math.nan
        )
        for ref_name, ref in refs.items():
            r = ref[mask]
            n_ref = int(vm.finite_pairs(p, o, r)[0].size)
            row[f"n_{ref_name}"] = float(n_ref)
            row[f"skill_vs_{ref_name}"] = (
                vm.finite_or_none(vm.skill_score(p, o, r))
                if n_ref >= vm.MIN_REFERENCE_PAIRS
                else None
            )
        out[label] = row
    return out


def reference_scores(frame: pd.DataFrame) -> dict[str, dict[str, dict[str, float | None]]]:
    return {
        name: score_hs(frame, frame[f"pred_{name}"].to_numpy(float)) for name in REFERENCE_NAMES
    }


# ------------------------------------------------------------------------------ training
@dataclass
class CandidateResult:
    kind: str
    params: dict[str, Any]
    model: TrainedCorrection
    grid: list[dict[str, Any]]
    validation: dict[str, dict[str, float | None]]
    test: dict[str, dict[str, float | None]]
    test_pred: np.ndarray
    validation_rmse: float
    folds: list[dict[str, Any]] = field(default_factory=list)
    loso: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TrainingReport:
    dataset_summary: dict[str, Any]
    split: Split
    feature_names: tuple[str, ...]
    guards: list[dict[str, Any]]
    references: dict[str, Any]
    candidates: list[CandidateResult]
    test_frame: pd.DataFrame
    climatology: dict[str, Any]
    seed: int
    folds_requested: int
    notes: list[str] = field(default_factory=list)

    def candidate(self, kind: str) -> CandidateResult | None:
        return next((c for c in self.candidates if c.kind == kind), None)


def _fold_summary(
    model: TrainedCorrection, test: pd.DataFrame, label: dict[str, Any]
) -> dict[str, Any]:
    pred = model.correct(test)
    obs = test["obs_hs_m"].to_numpy(dtype=float)
    raw = test["model_hs_m"].to_numpy(dtype=float)
    rmse_c, rmse_r = vm.rmse(pred, obs), vm.rmse(raw, obs)
    return {
        **label,
        "n": int(vm.n_pairs(pred, obs)),
        "rmse": vm.finite_or_none(rmse_c),
        "rmse_raw": vm.finite_or_none(rmse_r),
        "rmse_change_vs_raw": vm.finite_or_none(rmse_c / rmse_r - 1.0 if rmse_r > 0 else math.nan),
        "bias": vm.finite_or_none(vm.bias(pred, obs)),
        "bias_raw": vm.finite_or_none(vm.bias(raw, obs)),
        "hh": vm.finite_or_none(vm.hanna_heinold(pred, obs)),
        "hh_raw": vm.finite_or_none(vm.hanna_heinold(raw, obs)),
    }


def train_candidates(
    dataset: Dataset,
    *,
    candidates: Sequence[str] = CANDIDATE_KINDS,
    folds: int = 3,
    seed: int = SEED,
    loso: bool = False,
    use_observation_features: bool = True,
    val_fraction: float = 0.2,
    test_fraction: float = 0.2,
    grids: dict[str, list[dict[str, Any]]] | None = None,
) -> TrainingReport:
    """Fit, select (validation) and evaluate (test, once) every candidate."""
    frame = dataset.frame
    if len(frame) < MIN_TRAIN_ROWS:
        summary = dataset.summary
        raise InsufficientDataError(
            f"dataset has only {len(frame)} rows (minimum {MIN_TRAIN_ROWS}); targets are "
            f"{summary.get('hs_kinds')} observations; dropped: {summary.get('dropped')}"
        )
    grids = grids or HYPERPARAMETER_GRIDS
    kinds = [k for k in candidates if k != "raw"]
    unknown = sorted(set(kinds) - set(CANDIDATE_KINDS))
    if unknown:
        raise ValueError(f"unknown candidates {unknown}; choose from {CANDIDATE_KINDS}")
    notes: list[str] = []
    if "linear" not in kinds:
        # Criterion (d) compares every candidate with the ridge baseline.
        kinds = ["linear", *kinds]
        notes.append("linear baseline added: deployment criterion (d) needs it")
    feature_names = tuple(
        n for n in FEATURE_NAMES if use_observation_features or n not in OBSERVATION_FEATURES
    )
    split = chronological_split(frame, val_fraction=val_fraction, test_fraction=test_fraction)
    guards = list(split.guards)
    clim_train = Climatology.fit(split.train)
    val = add_reference_predictions(split.validation, clim_train)
    development = split.development
    clim_dev = Climatology.fit(development)
    test = add_reference_predictions(split.test, clim_dev)
    obs_val = val["obs_hs_m"].to_numpy(dtype=float)
    dev_folds = rolling_origin_folds(development, folds, split.gap) if folds > 0 else []
    loso_sets = leave_one_station_out(split) if loso else []

    results: list[CandidateResult] = []
    for kind in kinds:
        grid_results: list[dict[str, Any]] = []
        best: tuple[float, dict[str, Any], float, TrainedCorrection] | None = None
        for params in grids[kind]:
            model = fit_correction(kind, params, split.train, feature_names, seed=seed)
            unscaled = model.correct(val)
            scale = least_squares_scale(obs_val, unscaled)
            model.metre_scale = scale
            val_rmse = vm.rmse(model.correct(val), obs_val)
            grid_results.append(
                {
                    "params": params,
                    "metre_scale": scale,
                    "validation_rmse": vm.finite_or_none(val_rmse),
                }
            )
            if math.isfinite(val_rmse) and (best is None or val_rmse < best[0]):
                best = (val_rmse, params, scale, model)
        if best is None:
            raise InsufficientDataError(f"{kind}: no finite validation score")
        val_rmse, params, scale, selected = best
        validation_scores = score_hs(val, selected.correct(val))
        # Refit on train + validation; the guard is re-checked right before fitting.
        guards.append(assert_no_leakage(development, split.test, split.gap, label=f"{kind} refit"))
        final = fit_correction(kind, params, development, feature_names, seed=seed)
        final.metre_scale = scale
        test_pred = final.correct(test)
        result = CandidateResult(
            kind=kind,
            params=params,
            model=final,
            grid=grid_results,
            validation=validation_scores,
            test=score_hs(test, test_pred),
            test_pred=test_pred,
            validation_rmse=float(val_rmse),
        )
        for fold in dev_folds:
            try:
                fm = fit_correction(kind, params, fold.train, feature_names, seed=seed)
            except InsufficientDataError as exc:
                result.folds.append({"fold": fold.index, "skipped": str(exc)})
                continue
            fm.metre_scale = scale
            result.folds.append(
                _fold_summary(
                    fm,
                    fold.test,
                    {
                        "fold": fold.index,
                        "test_start": fold.test_start.isoformat(),
                        "test_end": fold.test_end.isoformat(),
                        "train_rows": len(fold.train),
                    },
                )
            )
        for code, lo_train, lo_test in loso_sets:
            try:
                lm = fit_correction(kind, params, lo_train, feature_names, seed=seed)
            except InsufficientDataError as exc:
                result.loso.append({"station": code, "skipped": str(exc)})
                continue
            lm.metre_scale = scale
            result.loso.append(_fold_summary(lm, lo_test, {"station": code}))
        results.append(result)
        logger.info(
            "Candidate %s %s: validation RMSE %.4f m, test RMSE %s m",
            kind,
            params,
            val_rmse,
            result.test["all"]["rmse"],
        )
    test = test.copy()
    for r in results:
        test[f"pred_{r.kind}"] = r.test_pred
    return TrainingReport(
        dataset_summary=dataset.summary,
        split=split,
        feature_names=feature_names,
        guards=guards,
        references={"validation": reference_scores(val), "test": reference_scores(test)},
        candidates=results,
        test_frame=test,
        climatology={"train": clim_train.describe(), "development": clim_dev.describe()},
        seed=seed,
        folds_requested=folds,
        notes=notes,
    )
