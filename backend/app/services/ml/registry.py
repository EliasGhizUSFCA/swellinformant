"""Experiment tracking, model registry, deployment criteria and artifact integrity.

Deployment criteria (evaluated on the held-out TEST period only, recorded one by one
in ``ml_model_versions.decision``). A candidate must:

(a) cut Hs RMSE (metres, after the back-transform) vs the raw model by at least
    ``MIN_RMSE_REDUCTION`` (5 %) over all test pairs;
(b) have a ``BOOTSTRAP_CONFIDENCE`` (90 %) cycle-block bootstrap interval of
    RMSE(candidate) - RMSE(raw) that lies entirely below 0 (whole forecast cycles are
    resampled, because errors within a cycle are correlated);
(c) not worsen RMSE in any lead bucket with at least ``BUCKET_MIN_N`` (50) pairs by more
    than ``MAX_BUCKET_DEGRADATION`` (3 %);
(d) beat or tie (within ``LINEAR_TIE_TOLERANCE``, 1 %) the ridge baseline's test RMSE,
    unless it is the ridge baseline (research notes, ML section: ship ML only if it
    beats the linear correction);
(e) have at least ``MIN_TEST_PAIRS`` (300) test pairs;
(f) not increase the Hanna-Heinold indicator HH vs raw. RMSE rewards damped,
    negatively biased forecasts (Mentaschi et al. 2013); HH does not, so a model that
    lowers RMSE only by shrinking forecasts is not deployed. This guard is an addition to
    (a)-(e) and can only make deployment stricter.

Status: ACTIVE only if every criterion passed AND activation was requested (``ml
activate ID`` or ``ml train --auto-activate``); otherwise REJECTED (some criterion
failed) or CANDIDATE (passed, not activated). Only versions trained against spectral
Hm0 observations can be activated: the correction is applied to model Hm0, so a model
fitted to time-domain H1/3 targets (``--hs-kind h_third``) would silently change the
height definition. Such versions are kept for evaluation. Activating retires the previously active
version of the same name (one active version per name, enforced by a partial unique
index). Among several passing candidates, auto-activation picks the one with the lowest
VALIDATION RMSE, so the test period is used only to accept or reject.

Artifacts are saved with joblib to ``<ML_ARTIFACT_DIR>/<name>/v<version>.joblib`` and
their sha256 is stored; loading re-hashes the bytes and refuses a mismatch, and the
verified bytes are what get deserialised. joblib files are pickles: only load artifacts
and manifests produced by an environment you trust.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import platform
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import BuoyStation, MLExperiment, MLModelVersion
from app.models.enums import MLModelStatus
from app.services.ml.features import HS_FLOOR_M
from app.services.ml.models import TrainedCorrection, TrainingReport
from app.services.verification import metrics as vm

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "offshore_hs_residual"
MODEL_TARGET = "offshore_hs_log_residual"
PIPELINE_VERSION = "ml-pipeline/1"
ARTIFACT_FORMAT = "swell-ml-artifact/1"
MANIFEST_FORMAT = "swell-ml-manifest/1"

MIN_RMSE_REDUCTION = 0.05
BOOTSTRAP_CONFIDENCE = 0.90
BOOTSTRAP_N = 1000
MAX_BUCKET_DEGRADATION = 0.03
BUCKET_MIN_N = 50
LINEAR_TIE_TOLERANCE = 0.01
MIN_TEST_PAIRS = 300
# Observed height definition a version must have been trained on to be activated.
ACTIVATABLE_HS_KINDS = ["spectral_hm0"]


class RegistryError(RuntimeError):
    pass


class ArtifactIntegrityError(RegistryError):
    """The artifact file is missing or its sha256 does not match the registry."""


class ActivationError(RegistryError):
    """The version may not be activated (criteria failed, or it cannot be loaded)."""


# ------------------------------------------------------------------------------ criteria
def _criterion(
    cid: str, name: str, threshold: Any, value: Any, passed: bool, detail: str
) -> dict[str, Any]:
    return {
        "id": cid,
        "name": name,
        "threshold": threshold,
        "value": value,
        "passed": bool(passed),
        "detail": detail,
    }


def evaluate_deployment(
    *,
    kind: str,
    obs: np.ndarray,
    pred: np.ndarray,
    raw: np.ndarray,
    lead_hours: np.ndarray,
    cycles: np.ndarray,
    linear_pred: np.ndarray | None,
    n_boot: int = BOOTSTRAP_N,
    seed: int = vm.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Apply criteria (a)-(f) to held-out TEST predictions; returns the decision record."""
    obs, pred, raw = (np.asarray(a, dtype=float) for a in (obs, pred, raw))
    lead = np.asarray(lead_hours, dtype=float)
    valid = np.isfinite(obs) & np.isfinite(pred) & np.isfinite(raw)
    o, p, r, ld, cyc = obs[valid], pred[valid], raw[valid], lead[valid], np.asarray(cycles)[valid]
    n = int(o.size)
    rmse_c, rmse_r = vm.rmse(p, o), vm.rmse(r, o)
    reduction = 1.0 - rmse_c / rmse_r if rmse_r > 0 else math.nan
    criteria = [
        _criterion(
            "a",
            "rmse_reduction_vs_raw",
            MIN_RMSE_REDUCTION,
            vm.finite_or_none(reduction),
            math.isfinite(reduction) and reduction >= MIN_RMSE_REDUCTION,
            f"test RMSE {rmse_c:.4f} m vs raw {rmse_r:.4f} m",
        )
    ]
    ci = vm.rmse_difference_ci(
        p, r, o, cyc, n_boot=n_boot, confidence=BOOTSTRAP_CONFIDENCE, seed=seed
    )
    criteria.append(
        _criterion(
            "b",
            "bootstrap_ci_rmse_difference_below_zero",
            {"confidence": BOOTSTRAP_CONFIDENCE, "n_boot": n_boot, "blocks": "forecast cycles"},
            ci.as_dict(),
            ci.available and ci.upper < 0,
            f"RMSE(candidate) - RMSE(raw) = {ci.estimate:+.4f} m, "
            f"{int(BOOTSTRAP_CONFIDENCE * 100)}% CI [{ci.lower:+.4f}, {ci.upper:+.4f}] m over "
            f"{ci.n_blocks} cycles",
        )
    )
    buckets: dict[str, Any] = {}
    worst = -math.inf
    for bucket in vm.LEAD_BUCKETS_H:
        mask = vm.in_lead_bucket(ld, bucket)
        nb = int(mask.sum())
        if nb < BUCKET_MIN_N:
            buckets[str(bucket)] = {"n": nb, "checked": False}
            continue
        rc, rr = vm.rmse(p[mask], o[mask]), vm.rmse(r[mask], o[mask])
        change = rc / rr - 1.0 if rr > 0 else math.nan
        buckets[str(bucket)] = {"n": nb, "checked": True, "rmse_change": vm.finite_or_none(change)}
        if math.isfinite(change):
            worst = max(worst, change)
    checked = [b for b in buckets.values() if b["checked"]]
    criteria.append(
        _criterion(
            "c",
            "no_lead_bucket_worse_than_raw",
            {"max_degradation": MAX_BUCKET_DEGRADATION, "min_n": BUCKET_MIN_N},
            {"worst_change": vm.finite_or_none(worst), "buckets": buckets},
            all(
                b["rmse_change"] is not None and b["rmse_change"] <= MAX_BUCKET_DEGRADATION
                for b in checked
            ),
            f"{len(checked)} buckets with n >= {BUCKET_MIN_N}; worst RMSE change {worst:+.1%}"
            if checked
            else "no bucket has enough pairs (nothing to worsen)",
        )
    )
    if kind == "linear" or linear_pred is None:
        criteria.append(
            _criterion(
                "d",
                "beats_or_ties_linear_baseline",
                LINEAR_TIE_TOLERANCE,
                None,
                kind == "linear",
                "is the linear baseline" if kind == "linear" else "no linear baseline available",
            )
        )
    else:
        lin = np.asarray(linear_pred, dtype=float)[valid]
        rmse_l = vm.rmse(lin, o)
        criteria.append(
            _criterion(
                "d",
                "beats_or_ties_linear_baseline",
                LINEAR_TIE_TOLERANCE,
                vm.finite_or_none(rmse_c / rmse_l - 1.0 if rmse_l > 0 else math.nan),
                rmse_c <= rmse_l * (1.0 + LINEAR_TIE_TOLERANCE),
                f"test RMSE {rmse_c:.4f} m vs linear {rmse_l:.4f} m",
            )
        )
    criteria.append(
        _criterion(
            "e",
            "min_test_pairs",
            MIN_TEST_PAIRS,
            n,
            n >= MIN_TEST_PAIRS,
            f"{n} test pairs",
        )
    )
    hh_c, hh_r = vm.hanna_heinold(p, o), vm.hanna_heinold(r, o)
    criteria.append(
        _criterion(
            "f",
            "hh_not_worse_than_raw",
            0.0,
            vm.finite_or_none(hh_c - hh_r),
            math.isfinite(hh_c) and math.isfinite(hh_r) and hh_c <= hh_r,
            f"HH {hh_c:.4f} vs raw {hh_r:.4f}; bias {vm.bias(p, o):+.4f} m vs raw "
            f"{vm.bias(r, o):+.4f} m",
        )
    )
    failed = [c["id"] for c in criteria if not c["passed"]]
    return {
        "evaluated_on": "test",
        "n_test": n,
        "criteria": criteria,
        "failed": failed,
        "passed_all": not failed,
        "diagnostics": {
            "bias": vm.finite_or_none(vm.bias(p, o)),
            "bias_raw": vm.finite_or_none(vm.bias(r, o)),
            "hh": vm.finite_or_none(hh_c),
            "hh_raw": vm.finite_or_none(hh_r),
            "si": vm.finite_or_none(vm.scatter_index(p, o)),
            "si_raw": vm.finite_or_none(vm.scatter_index(r, o)),
        },
    }


# ------------------------------------------------------------------------------ artifacts
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_path(name: str, version: int, root: str | Path | None = None) -> Path:
    base = Path(root or get_settings().ml_artifact_dir)
    return base / name / f"v{version}.joblib"


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "joblib": joblib.__version__,
    }


def dump_artifact(model: TrainedCorrection, meta: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    joblib.dump({"format": ARTIFACT_FORMAT, "model": model, "meta": meta}, buf)
    return buf.getvalue()


def write_artifact(data: bytes, path: Path) -> str:
    """Atomically write ``data`` to ``path``; returns its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return sha256_bytes(data)


def read_verified(path: str | Path, expected_sha256: str | None) -> bytes:
    p = Path(path)
    if not expected_sha256:
        raise ArtifactIntegrityError(f"no sha256 recorded for {p}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot read artifact {p}: {exc}") from exc
    actual = sha256_bytes(data)
    if actual != expected_sha256:
        raise ArtifactIntegrityError(
            f"sha256 mismatch for {p}: registry {expected_sha256}, file {actual}"
        )
    return data


def load_artifact(version: MLModelVersion) -> TrainedCorrection:
    """Load a version's model after verifying the file's sha256 against the registry."""
    if not version.artifact_path:
        raise ArtifactIntegrityError(f"{version.label} has no artifact")
    data = read_verified(version.artifact_path, version.artifact_sha256)
    payload = joblib.load(io.BytesIO(data))
    if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
        raise ArtifactIntegrityError(f"{version.label}: unexpected artifact format")
    model = payload["model"]
    if not isinstance(model, TrainedCorrection):
        raise ArtifactIntegrityError(f"{version.label}: artifact holds no TrainedCorrection")
    return model


# ------------------------------------------------------------------------------ registry
def next_version(db: Session, name: str) -> int:
    current = db.scalar(select(func.max(MLModelVersion.version)).where(MLModelVersion.name == name))
    return int(current or 0) + 1


def active_version(db: Session, name: str) -> MLModelVersion | None:
    return db.scalar(
        select(MLModelVersion).where(
            MLModelVersion.name == name, MLModelVersion.status == MLModelStatus.ACTIVE
        )
    )


def list_versions(db: Session, name: str | None = None) -> list[MLModelVersion]:
    stmt = select(MLModelVersion).order_by(MLModelVersion.name, MLModelVersion.version)
    if name:
        stmt = stmt.where(MLModelVersion.name == name)
    return list(db.scalars(stmt))


def paired_spots(db: Session, station_codes: list[str]) -> list[str]:
    """Spot slugs listed in buoy_stations.metadata.paired_spots (strings or {"slug"})."""
    slugs: set[str] = set()
    for meta in db.scalars(
        select(BuoyStation.metadata_).where(BuoyStation.code.in_(station_codes))
    ):
        for item in (meta or {}).get("paired_spots") or []:
            slug = item.get("slug") if isinstance(item, dict) else item
            if isinstance(slug, str) and slug:
                slugs.add(slug)
    return sorted(slugs)


def register_version(
    db: Session,
    *,
    name: str,
    model: TrainedCorrection,
    experiment: MLExperiment | None,
    metrics: dict[str, Any],
    decision: dict[str, Any],
    applicability: dict[str, Any],
    training: dict[str, Any],
    hyperparameters: dict[str, Any],
    status: MLModelStatus,
    artifact_root: str | Path | None = None,
) -> MLModelVersion:
    """Save the artifact and insert the ml_model_versions row (flushes, no commit)."""
    version = next_version(db, name)
    path = artifact_path(name, version, artifact_root)
    data = dump_artifact(model, {"name": name, "version": version, "created_at": str(utcnow())})
    digest = write_artifact(data, path)
    row = MLModelVersion(
        name=name,
        version=version,
        experiment_id=experiment.id if experiment is not None else None,
        model_type=model.kind,
        target=MODEL_TARGET,
        feature_names=list(model.feature_names),
        hyperparameters=hyperparameters,
        training=training,
        metrics=metrics,
        decision=decision,
        applicability=applicability,
        artifact_path=str(path),
        artifact_sha256=digest,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def activate(db: Session, version_id: int, *, now: datetime | None = None) -> MLModelVersion:
    """Make a version ACTIVE (criteria must have passed); retires the previous one."""
    version = db.get(MLModelVersion, version_id)
    if version is None:
        raise LookupError(f"no model version {version_id}")
    if not (version.decision or {}).get("passed_all"):
        failed = (version.decision or {}).get("failed", ["not evaluated"])
        raise ActivationError(
            f"{version.label} failed the deployment criteria {failed}; it cannot be activated"
        )
    hs_kinds = (version.applicability or {}).get("hs_kinds", ACTIVATABLE_HS_KINDS)
    if list(hs_kinds) != ACTIVATABLE_HS_KINDS:
        raise ActivationError(
            f"{version.label} was trained against {hs_kinds} observations; corrections are "
            f"applied to model Hm0, so only {ACTIVATABLE_HS_KINDS}-trained versions can be "
            "activated"
        )
    if version.status == MLModelStatus.ACTIVE:
        return version
    load_artifact(version)  # refuse to activate an artifact that fails verification
    now = now or utcnow()
    current = active_version(db, version.name)
    if current is not None and current.id != version.id:
        current.status = MLModelStatus.RETIRED
        current.retired_at = now
        db.flush()
    version.status = MLModelStatus.ACTIVE
    version.activated_at = now
    version.retired_at = None
    db.commit()
    return version


def deactivate(db: Session, name: str, *, now: datetime | None = None) -> MLModelVersion | None:
    """Retire the active version of ``name`` (forecasts revert to raw). None if none."""
    current = active_version(db, name)
    if current is None:
        return None
    current.status = MLModelStatus.RETIRED
    current.retired_at = now or utcnow()
    db.commit()
    return current


# ------------------------------------------------------------------------------ runs
@dataclass
class TrainingRecord:
    experiment: MLExperiment
    versions: list[MLModelVersion]
    activated: MLModelVersion | None


def _cycle_blocks(frame: pd.DataFrame) -> np.ndarray:
    return pd.to_datetime(frame["cycle_time"], utc=True).astype("int64").to_numpy()


def _training_info(report: TrainingReport) -> dict[str, Any]:
    dev = report.split.development
    return {
        "period": {
            "cycle_start": pd.Timestamp(dev["cycle_time"].min()).isoformat(),
            "cycle_end": pd.Timestamp(dev["cycle_time"].max()).isoformat(),
            "valid_end": pd.Timestamp(dev["valid_time"].max()).isoformat(),
        },
        "data_end": pd.Timestamp(dev["valid_time"].max()).isoformat(),
        "test_period": {
            "cycle_start": report.split.test_start.isoformat(),
            "cycle_end": pd.Timestamp(report.split.test["cycle_time"].max()).isoformat(),
        },
        "rows": {
            "train": len(report.split.train),
            "validation": len(report.split.validation),
            "test": len(report.split.test),
            "refit": len(dev),
        },
        "stations": sorted(dev["station_code"].unique().tolist()),
        "sources": sorted(dev["source_code"].unique().tolist()),
        "families": sorted(dev["family"].unique().tolist()),
        "seed": report.seed,
        "environment": _environment(),
        "pipeline_version": PIPELINE_VERSION,
    }


def _conclusion(report: TrainingReport, decisions: dict[str, dict[str, Any]]) -> str:
    raw = report.references["test"]["raw"]["all"]
    lines = [
        f"Held-out test period from {report.split.test_start.date()} "
        f"({int(raw['n'] or 0)} pairs): raw model RMSE {_num(raw['rmse'])} m, "
        f"bias {_fmt(raw['bias'])} m, HH {_num(raw['hh'])}."
    ]
    for ref in ("persistence", "climatology"):
        ref_all = report.references["test"][ref]["all"]
        if ref_all["rmse"] is not None:
            lines.append(
                f"{ref.capitalize()} reference RMSE {_num(ref_all['rmse'])} m on "
                f"{int(ref_all['n'] or 0)} pairs; raw skill vs {ref} "
                f"{_fmt(raw.get(f'skill_vs_{ref}'))}."
            )
    for c in sorted(report.candidates, key=lambda c: c.validation_rmse):
        t = c.test["all"]
        d = decisions[c.kind]
        verdict = (
            "passes every deployment criterion"
            if d["passed_all"]
            else f"fails criteria {', '.join(d['failed'])}"
        )
        lines.append(
            f"{c.kind}: validation RMSE {c.validation_rmse:.3f} m; test RMSE {_num(t['rmse'])} m "
            f"({_fmt_pct(t['rmse_change_vs_raw'])} vs raw), bias {_fmt(t['bias'])} m, "
            f"HH {_num(t['hh'])}; {verdict}."
        )
    passed = [c.kind for c in report.candidates if decisions[c.kind]["passed_all"]]
    if passed:
        lines.append(f"Deployable (passed on held-out data): {', '.join(passed)}.")
    else:
        lines.append(
            "No candidate passed the deployment criteria; forecasts stay uncorrected (raw)."
        )
    return " ".join(lines)


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def _num(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _fmt_pct(value: Any) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


def record_training_run(
    db: Session,
    report: TrainingReport,
    *,
    name: str = DEFAULT_MODEL_NAME,
    auto_activate: bool = False,
    description: str = "",
    n_boot: int = BOOTSTRAP_N,
    artifact_root: str | Path | None = None,
) -> TrainingRecord:
    """Write the experiment, one version per candidate (with its decision) and, when
    requested, activate the best passing candidate (lowest validation RMSE)."""
    test = report.test_frame
    obs = test["obs_hs_m"].to_numpy(dtype=float)
    raw = test["pred_raw"].to_numpy(dtype=float)
    lead = test["lead_hours"].to_numpy(dtype=float)
    cycles = _cycle_blocks(test)
    linear = report.candidate("linear")
    decisions = {
        c.kind: evaluate_deployment(
            kind=c.kind,
            obs=obs,
            pred=c.test_pred,
            raw=raw,
            lead_hours=lead,
            cycles=cycles,
            linear_pred=linear.test_pred if linear is not None else None,
            n_boot=n_boot,
            seed=report.seed,
        )
        for c in report.candidates
    }
    training = _training_info(report)
    stations = training["stations"]
    applicability = {
        "station_codes": stations,
        "spot_slugs": paired_spots(db, stations),
        "lead_min_h": float(report.split.development["lead_hours"].min()),
        "lead_max_h": float(report.split.development["lead_hours"].max()),
        "sources": training["sources"],
        "families": training["families"],
        "hs_kinds": report.dataset_summary.get("hs_kinds", ["spectral_hm0"]),
        "variable": "offshore significant wave height Hm0 (not breaking height)",
    }
    results = {
        "references": report.references,
        "candidates": {
            c.kind: {
                "params": c.params,
                "grid": c.grid,
                "validation": c.validation,
                "test": c.test,
                "rolling_origin": c.folds,
                "leave_one_station_out": c.loso,
                "decision": decisions[c.kind],
            }
            for c in report.candidates
        },
    }
    experiment = MLExperiment(
        name=name,
        target=MODEL_TARGET,
        description=description or "Offshore Hs residual correction (MOS, log space)",
        dataset={
            **report.dataset_summary,
            "features_used": list(report.feature_names),
            "leakage_guards": report.guards,
            "climatology": report.climatology,
            "notes": report.notes,
        },
        split={
            **report.split.describe(),
            "rolling_origin_folds": report.folds_requested,
            "selection": "hyperparameters on validation only; test evaluated once",
        },
        results=results,
        conclusion=_conclusion(report, decisions),
        code_version=PIPELINE_VERSION,
        status="finished",
        finished_at=utcnow(),
    )
    db.add(experiment)
    db.flush()
    versions: list[MLModelVersion] = []
    for c in report.candidates:
        decision = decisions[c.kind]
        version = register_version(
            db,
            name=name,
            model=c.model,
            experiment=experiment,
            metrics={
                "test": c.test,
                "validation": c.validation,
                "references_test": report.references["test"],
                "rolling_origin": c.folds,
                "leave_one_station_out": c.loso,
            },
            decision=decision,
            applicability=applicability,
            training=training,
            hyperparameters={
                **c.params,
                "metre_scale": c.model.metre_scale,
                "factor_clamp": [c.model.factor_min, c.model.factor_max],
                "hs_floor_m": HS_FLOOR_M,
                "seed": report.seed,
                "unused_features": list(c.model.unused_features),
            },
            status=MLModelStatus.CANDIDATE if decision["passed_all"] else MLModelStatus.REJECTED,
            artifact_root=artifact_root,
        )
        versions.append(version)
    db.commit()
    activated: MLModelVersion | None = None
    if auto_activate and list(applicability["hs_kinds"]) != ACTIVATABLE_HS_KINDS:
        logger.warning(
            "Not auto-activating: trained against %s observations (evaluation only)",
            applicability["hs_kinds"],
        )
    elif auto_activate:
        passing = [
            (c.validation_rmse, v)
            for c, v in zip(report.candidates, versions, strict=True)
            if decisions[c.kind]["passed_all"]
        ]
        if passing:
            activated = activate(db, min(passing, key=lambda t: t[0])[1].id)
    return TrainingRecord(experiment, versions, activated)


# ------------------------------------------------------------------------------ manifests
def _version_record(v: MLModelVersion) -> dict[str, Any]:
    return {
        "name": v.name,
        "version": v.version,
        "label": v.label,
        "model_type": v.model_type,
        "target": v.target,
        "feature_names": v.feature_names,
        "hyperparameters": v.hyperparameters,
        "training": v.training,
        "metrics": v.metrics,
        "decision": v.decision,
        "applicability": v.applicability,
        "status": v.status.value,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "activated_at": v.activated_at.isoformat() if v.activated_at else None,
    }


def _experiment_record(e: MLExperiment | None) -> dict[str, Any] | None:
    if e is None:
        return None
    return {
        "name": e.name,
        "target": e.target,
        "description": e.description,
        "dataset": e.dataset,
        "split": e.split,
        "results": e.results,
        "conclusion": e.conclusion,
        "code_version": e.code_version,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def export_manifest(db: Session, version_id: int, directory: str | Path) -> Path:
    """Copy a version's verified artifact plus its registry record into ``directory``."""
    version = db.get(MLModelVersion, version_id)
    if version is None:
        raise LookupError(f"no model version {version_id}")
    data = read_verified(version.artifact_path or "", version.artifact_sha256)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{version.name}-v{version.version}"
    artifact_file = out / f"{stem}.joblib"
    write_artifact(data, artifact_file)
    manifest = {
        "format": MANIFEST_FORMAT,
        "exported_at": utcnow().isoformat(),
        "model": _version_record(version),
        "experiment": _experiment_record(version.experiment),
        "artifact": {
            "file": artifact_file.name,
            "sha256": version.artifact_sha256,
            "bytes": len(data),
        },
        "environment": _environment(),
    }
    path = out / f"{stem}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return path


def import_manifest(
    db: Session, path: str | Path, *, artifact_root: str | Path | None = None
) -> MLModelVersion:
    """Register a manifest's model here (sha256 verified). Never imported as ACTIVE:
    status is CANDIDATE when its recorded decision passed, else REJECTED. Importing the
    same artifact twice returns the existing version."""
    manifest_path = Path(path)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != MANIFEST_FORMAT:
        raise RegistryError(f"{manifest_path}: not a {MANIFEST_FORMAT} manifest")
    record, art = manifest["model"], manifest["artifact"]
    data = read_verified(manifest_path.parent / art["file"], art["sha256"])
    existing = db.scalar(
        select(MLModelVersion).where(
            MLModelVersion.name == record["name"],
            MLModelVersion.artifact_sha256 == art["sha256"],
        )
    )
    if existing is not None:
        return existing
    env = manifest.get("environment", {})
    if env.get("sklearn") and env["sklearn"] != sklearn.__version__:
        logger.warning(
            "Importing %s trained with scikit-learn %s into %s; re-evaluate before activating",
            record["label"],
            env["sklearn"],
            sklearn.__version__,
        )
    experiment: MLExperiment | None = None
    exp = manifest.get("experiment")
    if exp:
        experiment = MLExperiment(
            name=exp["name"],
            target=exp["target"],
            description=f"[imported from {record['label']}] {exp.get('description', '')}",
            dataset=exp.get("dataset") or {},
            split=exp.get("split") or {},
            results=exp.get("results") or {},
            conclusion=exp.get("conclusion") or "",
            code_version=exp.get("code_version") or "",
            status="imported",
            finished_at=utcnow(),
        )
        db.add(experiment)
        db.flush()
    name = record["name"]
    version_no = next_version(db, name)
    target_path = artifact_path(name, version_no, artifact_root)
    digest = write_artifact(data, target_path)
    decision = record.get("decision") or {}
    version = MLModelVersion(
        name=name,
        version=version_no,
        experiment_id=experiment.id if experiment else None,
        model_type=record["model_type"],
        target=record["target"],
        feature_names=record.get("feature_names") or [],
        hyperparameters=record.get("hyperparameters") or {},
        training={
            **(record.get("training") or {}),
            "imported_from": {
                "label": record["label"],
                "exported_at": manifest.get("exported_at"),
                "environment": env,
            },
        },
        metrics=record.get("metrics") or {},
        decision=decision,
        applicability=record.get("applicability") or {},
        artifact_path=str(target_path),
        artifact_sha256=digest,
        status=MLModelStatus.CANDIDATE if decision.get("passed_all") else MLModelStatus.REJECTED,
    )
    db.add(version)
    db.commit()
    return version


def evaluate_version(db: Session, version_id: int) -> dict[str, Any]:
    """A version's recorded decision plus scores on forecasts issued after its test period.

    The recorded decision used the held-out test period; the fresh scores use cycles
    later than that period (never seen in training, selection or the decision). The
    climatology reference is not recomputed here.
    """
    from app.services.ml.baselines import Climatology, add_reference_predictions
    from app.services.ml.dataset import build_dataset
    from app.services.ml.models import score_hs

    version = db.get(MLModelVersion, version_id)
    if version is None:
        raise LookupError(f"no model version {version_id}")
    model = load_artifact(version)
    training, app = version.training or {}, version.applicability or {}
    out: dict[str, Any] = {
        "version": version_summary(version),
        "decision": version.decision,
        "recorded_test_scores": ((version.metrics or {}).get("test") or {}).get("all"),
        "applicability": app,
    }
    test_end = (training.get("test_period") or {}).get("cycle_end")
    if not test_end:
        out["since_test_period"] = {"rows": 0, "note": "no test period recorded"}
        return out
    after = pd.Timestamp(test_end)
    ds = build_dataset(
        db,
        sources=app.get("sources") or None,
        station_codes=app.get("station_codes") or None,
        start=after.to_pydatetime(),
    )
    frame = ds.frame[ds.frame["cycle_time"] > after] if not ds.frame.empty else ds.frame
    if frame.empty:
        out["since_test_period"] = {"rows": 0, "note": "no forecasts newer than the test period"}
        return out
    scored = add_reference_predictions(frame.reset_index(drop=True), Climatology())
    out["since_test_period"] = {
        "rows": len(scored),
        "cycle_start": pd.Timestamp(scored["cycle_time"].min()).isoformat(),
        "cycle_end": pd.Timestamp(scored["cycle_time"].max()).isoformat(),
        "scores": score_hs(scored, model.correct(scored)),
    }
    return out


def version_summary(v: MLModelVersion) -> dict[str, Any]:
    test_all = ((v.metrics or {}).get("test") or {}).get("all") or {}
    return {
        "id": v.id,
        "label": v.label,
        "model_type": v.model_type,
        "status": v.status.value,
        "passed_all": bool((v.decision or {}).get("passed_all")),
        "failed": (v.decision or {}).get("failed", []),
        "test_rmse": test_all.get("rmse"),
        "test_rmse_raw": test_all.get("rmse_raw"),
        "test_rmse_change_vs_raw": test_all.get("rmse_change_vs_raw"),
        "n_test": (v.decision or {}).get("n_test"),
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "activated_at": v.activated_at.isoformat() if v.activated_at else None,
        "retired_at": v.retired_at.isoformat() if v.retired_at else None,
    }
