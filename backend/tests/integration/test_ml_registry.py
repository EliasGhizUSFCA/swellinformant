"""Model registry and the end-to-end training pipeline against the database.

Every station, observation and forecast here is SYNTHETIC (tests/synthetic_waves.py):
a seeded random process with a known, injected model error. Nothing is a real
measurement or forecast, and the accuracy numbers asserted here say nothing about any
real model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.core.config import get_settings
from app.models import ForecastAccuracyMetric, MLExperiment, MLModelVersion
from app.models.enums import MLModelStatus
from app.services.ml import models as ml_models
from app.services.ml import registry
from app.services.ml.apply import clear_correction_cache, load_active_correction
from app.services.ml.dataset import assemble_frame, build_dataset, load_dataset
from app.services.ml.features import FEATURE_NAMES, features_from_wave_forecast
from app.services.ml.models import TrainedCorrection, fit_correction, train_candidates
from app.services.verification.jobs import compute_accuracy_metrics
from app.services.verification.pairing import pair_forecasts
from tests.synthetic_waves import (
    SYNTHETIC_SOURCE,
    frames_for_assembly,
    insert_case,
    make_case,
)

pytestmark = pytest.mark.integration

# Small grids keep the test fast; production uses ml_models.HYPERPARAMETER_GRIDS.
FAST_GRIDS: dict[str, list[dict[str, Any]]] = {
    "linear": [{"alpha": 1.0}, {"alpha": 10.0}],
    "rf": [
        {
            "n_estimators": 30,
            "max_depth": 8,
            "min_samples_leaf": 20,
            "max_features": 0.5,
            "max_samples": 0.5,
        }
    ],
    "gbt": [
        {
            "learning_rate": 0.1,
            "max_leaf_nodes": 15,
            "max_iter": 80,
            "min_samples_leaf": 40,
            "l2_regularization": 1.0,
        }
    ],
}
PASSED = {"passed_all": True, "failed": [], "criteria": [], "n_test": 1000}
FAILED = {"passed_all": False, "failed": ["a"], "criteria": [], "n_test": 1000}


@pytest.fixture
def artifact_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "artifacts"
    monkeypatch.setattr(get_settings(), "ml_artifact_dir", str(root))
    monkeypatch.setattr(get_settings(), "ml_corrections_enabled", True)
    clear_correction_cache()
    yield root
    clear_correction_cache()


@pytest.fixture(scope="module")
def small_model() -> TrainedCorrection:
    """A real ridge correction fitted on a small SYNTHETIC frame (no database needed)."""
    case = make_case("bias", n_stations=1, days=30, lead_step=12, max_lead=120, seed=8)
    fc, obs, stations = frames_for_assembly(case)
    frame = assemble_frame(fc, obs, stations)
    return fit_correction("linear", {"alpha": 1.0}, frame, FEATURE_NAMES)


def _register(
    db: Session,
    model: TrainedCorrection,
    name: str = "synthetic_hs",
    decision: Any = PASSED,
    hs_kinds: tuple[str, ...] = ("spectral_hm0",),
) -> MLModelVersion:
    version = registry.register_version(
        db,
        name=name,
        model=model,
        experiment=None,
        metrics={},
        decision=decision,
        applicability={
            "station_codes": ["synthetic:station-1"],
            "spot_slugs": ["synthetic-spot-1"],
            "lead_min_h": model.lead_min,
            "lead_max_h": model.lead_max,
            "hs_kinds": list(hs_kinds),
        },
        training={},
        hyperparameters={"alpha": 1.0},
        status=MLModelStatus.CANDIDATE if decision["passed_all"] else MLModelStatus.REJECTED,
    )
    db.commit()
    return version


def test_versions_increment_per_name(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    v1 = _register(db, small_model)
    v2 = _register(db, small_model)
    other = _register(db, small_model, name="other_model")
    assert (v1.version, v2.version, other.version) == (1, 2, 1)
    assert v1.label == "synthetic_hs@v1"
    assert Path(v2.artifact_path or "") == artifact_dir / "synthetic_hs" / "v2.joblib"
    assert len(v1.artifact_sha256 or "") == 64
    assert [v.label for v in registry.list_versions(db, "synthetic_hs")] == [
        "synthetic_hs@v1",
        "synthetic_hs@v2",
    ]


def test_activation_retires_the_previous_version(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    v1 = _register(db, small_model)
    v2 = _register(db, small_model)
    rejected = _register(db, small_model, decision=FAILED)
    registry.activate(db, v1.id)
    assert registry.active_version(db, "synthetic_hs").id == v1.id  # type: ignore[union-attr]
    registry.activate(db, v2.id)
    db.refresh(v1)
    assert v1.status == MLModelStatus.RETIRED and v1.retired_at is not None
    assert v2.status == MLModelStatus.ACTIVE and v2.activated_at is not None
    with pytest.raises(registry.ActivationError, match="failed the deployment criteria"):
        registry.activate(db, rejected.id)
    assert registry.active_version(db, "synthetic_hs").id == v2.id  # type: ignore[union-attr]
    corr = load_active_correction(db, "synthetic_hs")
    assert corr is not None and corr.label == "synthetic_hs@v2"
    assert load_active_correction(db, "synthetic_hs") is corr  # cached per process
    registry.activate(db, v1.id)  # re-activating swaps again; the cache follows
    swapped = load_active_correction(db, "synthetic_hs")
    assert swapped is not None and swapped.label == "synthetic_hs@v1"
    retired = registry.deactivate(db, "synthetic_hs")
    assert retired is not None and retired.id == v1.id
    assert registry.active_version(db, "synthetic_hs") is None
    assert load_active_correction(db, "synthetic_hs") is None
    assert registry.deactivate(db, "synthetic_hs") is None
    with pytest.raises(LookupError):
        registry.activate(db, 999_999)


def test_only_hm0_trained_versions_can_be_activated(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    """A model fitted to H1/3 targets would change the height definition of Hm0 forecasts."""
    h13 = _register(db, small_model, hs_kinds=("h_third",))
    with pytest.raises(registry.ActivationError, match="h_third"):
        registry.activate(db, h13.id)
    assert registry.active_version(db, "synthetic_hs") is None


def test_checksum_mismatch_is_refused(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    version = _register(db, small_model)
    loaded = registry.load_artifact(version)
    assert loaded.feature_names == small_model.feature_names
    path = Path(version.artifact_path or "")
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(registry.ArtifactIntegrityError, match="sha256 mismatch"):
        registry.load_artifact(version)
    with pytest.raises(registry.ArtifactIntegrityError):
        registry.activate(db, version.id)
    db.rollback()
    assert db.get(MLModelVersion, version.id).status == MLModelStatus.CANDIDATE  # type: ignore[union-attr]
    # Even if the row is marked active, a tampered artifact is never applied.
    version.status = MLModelStatus.ACTIVE
    db.commit()
    clear_correction_cache()
    assert load_active_correction(db, "synthetic_hs") is None
    path.unlink()
    with pytest.raises(registry.ArtifactIntegrityError, match="cannot read"):
        registry.load_artifact(version)


def test_manifest_round_trip(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection, tmp_path: Path
) -> None:
    original = _register(db, small_model)
    export_dir = tmp_path / "export"
    manifest_path = registry.export_manifest(db, original.id, export_dir)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["format"] == registry.MANIFEST_FORMAT
    assert manifest["artifact"]["sha256"] == original.artifact_sha256
    assert (export_dir / manifest["artifact"]["file"]).exists()

    X = np.array([[24.0, 2.0, math.log(2.0)] + [0.0] * (len(FEATURE_NAMES) - 3)])
    expected = registry.load_artifact(original).factors(X)[0]
    # Importing into the environment that already holds this artifact is a no-op.
    assert registry.import_manifest(db, manifest_path).id == original.id

    # "Another environment": neither the registry row nor the artifact file exist there.
    Path(original.artifact_path or "").unlink()
    db.delete(original)
    db.commit()
    imported = registry.import_manifest(db, manifest_path)
    assert imported.version == 1 and imported.label == "synthetic_hs@v1"
    assert imported.artifact_sha256 == manifest["artifact"]["sha256"]
    assert imported.status == MLModelStatus.CANDIDATE  # never imported as active
    assert imported.training["imported_from"]["label"] == "synthetic_hs@v1"
    np.testing.assert_allclose(registry.load_artifact(imported).factors(X)[0], expected)
    assert registry.import_manifest(db, manifest_path).id == imported.id  # idempotent

    # A corrupted artifact next to the manifest is refused.
    db.delete(imported)
    db.commit()
    artifact_file = export_dir / manifest["artifact"]["file"]
    artifact_file.write_bytes(artifact_file.read_bytes()[:-5])
    with pytest.raises(registry.ArtifactIntegrityError):
        registry.import_manifest(db, manifest_path)
    bad_format = tmp_path / "bad.json"
    bad_format.write_text(json.dumps({"format": "something-else"}))
    with pytest.raises(registry.RegistryError):
        registry.import_manifest(db, bad_format)


def test_artifacts_are_only_loaded_from_the_artifact_dir(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection, tmp_path: Path
) -> None:
    """A registry row pointing outside ML_ARTIFACT_DIR is refused even when the file's
    sha256 matches: the checksum protects the bytes, this protects the location."""
    version = _register(db, small_model)
    outside = tmp_path / "elsewhere" / "v1.joblib"
    outside.parent.mkdir()
    outside.write_bytes(Path(version.artifact_path or "").read_bytes())
    version.artifact_path = str(outside)
    db.commit()
    with pytest.raises(registry.ArtifactIntegrityError, match="outside ML_ARTIFACT_DIR"):
        registry.load_artifact(version)
    with pytest.raises(registry.ArtifactIntegrityError):
        registry.export_manifest(db, version.id, tmp_path / "export")
    # "../" inside the recorded path does not escape either.
    version.artifact_path = str(
        artifact_dir / "synthetic_hs" / ".." / ".." / "elsewhere" / "v1.joblib"
    )
    db.commit()
    with pytest.raises(registry.ArtifactIntegrityError, match="outside ML_ARTIFACT_DIR"):
        registry.load_artifact(version)


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "x" * 81])
def test_model_names_cannot_leave_the_artifact_dir(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection, name: str
) -> None:
    with pytest.raises(registry.RegistryError, match="invalid model name"):
        _register(db, small_model, name=name)
    db.rollback()
    assert not any(p.suffix == ".joblib" for p in artifact_dir.parent.rglob("*.joblib"))


def test_manifest_paths_are_validated(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection, tmp_path: Path
) -> None:
    original = _register(db, small_model)
    manifest_path = registry.export_manifest(db, original.id, tmp_path / "export")
    manifest = json.loads(manifest_path.read_text())
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"not for import")
    for field, value in (
        ("file", str(secret)),  # absolute path
        ("file", "../secret.bin"),  # traversal
    ):
        bad = json.loads(json.dumps(manifest))
        bad["artifact"][field] = value
        bad["artifact"]["sha256"] = registry.sha256_bytes(secret.read_bytes())
        path = tmp_path / "export" / "bad.manifest.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(registry.RegistryError, match="not a bare name"):
            registry.import_manifest(db, path)
    bad = json.loads(json.dumps(manifest))
    bad["model"]["name"] = "../../outside"
    path = tmp_path / "export" / "bad-name.manifest.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(registry.RegistryError, match="invalid model name"):
        registry.import_manifest(db, path)


def test_unloadable_active_artifact_falls_back_to_raw(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    """A verified file that cannot be unpickled (e.g. after a scikit-learn upgrade) must
    leave forecasts raw instead of crashing the prediction and verification jobs."""
    version = _register(db, small_model)
    path = Path(version.artifact_path or "")
    path.write_bytes(b"\x80\x04this is not a joblib pickle")
    version.artifact_sha256 = registry.sha256_bytes(path.read_bytes())  # checksum matches
    version.status = MLModelStatus.ACTIVE
    db.commit()
    clear_correction_cache()
    assert load_active_correction(db, "synthetic_hs") is None
    summary = compute_accuracy_metrics(db, now=datetime(2026, 1, 1, tzinfo=UTC), n_boot=10)
    assert summary["corrected_model"] is None  # raw metrics still computed


def test_applicability_skips_upstream_and_nearshore_pairings(db: Session) -> None:
    """Only offshore-reference pairings receive a correction learned at the buoy."""
    from app.models import BuoyStation

    db.add_all(
        [
            BuoyStation(
                code="synthetic:offshore",
                provider="synthetic",
                name="SYNTHETIC offshore buoy",
                latitude=0.0,
                longitude=0.0,
                metadata_={
                    "paired_spots": [
                        {"slug": "spot-a", "relation": "offshore_reference"},
                        {"slug": "spot-b", "relation": "upstream_regional"},
                        {"slug": "spot-c", "relation": "nearshore_reference"},
                        "spot-d",
                    ]
                },
            ),
            BuoyStation(
                code="synthetic:other",
                provider="synthetic",
                name="SYNTHETIC second buoy",
                latitude=1.0,
                longitude=1.0,
                metadata_={
                    "paired_spots": [{"slug": "spot-c", "relation": "historical_reference"}]
                },
            ),
        ]
    )
    db.commit()
    excluded: dict[str, str] = {}
    assert registry.paired_spots(db, ["synthetic:offshore"], excluded=excluded) == [
        "spot-a",
        "spot-d",
    ]
    assert excluded == {"spot-b": "upstream_regional", "spot-c": "nearshore_reference"}
    excluded = {}
    both = registry.paired_spots(db, ["synthetic:offshore", "synthetic:other"], excluded=excluded)
    assert both == ["spot-a", "spot-c", "spot-d"] and excluded == {"spot-b": "upstream_regional"}


def test_evaluate_uses_the_trained_height_definition(
    db: Session,
    artifact_dir: Path,
    small_model: TrainedCorrection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.ml import dataset as ml_dataset

    version = _register(db, small_model, hs_kinds=("h_third",))
    version.training = {"test_period": {"cycle_end": "2025-01-01T00:00:00+00:00"}}
    db.commit()
    seen: dict[str, Any] = {}
    real = ml_dataset.build_dataset

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(ml_dataset, "build_dataset", spy)
    out = registry.evaluate_version(db, version.id)
    assert seen["hs_kinds"] == ("h_third",)
    assert out["since_test_period"]["rows"] == 0


# ------------------------------------------------------------------------------ end to end
def test_end_to_end_synthetic_bias_is_corrected_and_deployable(
    db: Session, artifact_dir: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """SYNTHETIC: the model over-forecasts by 1.15 + 0.20 * lead / 240 (multiplicative,
    lead-dependent). Linear and GBT corrections must learn it and pass every criterion."""
    case = make_case("bias", n_stations=3, days=150, lead_step=12, max_lead=240, seed=1)
    insert_case(db, case)
    dataset = build_dataset(db, sources=[SYNTHETIC_SOURCE])
    assert dataset.summary["rows"] == 3 * 150 * 20  # lead-0 rows dropped
    assert dataset.summary["stations"] == [s["code"] for s in case.stations]
    report = train_candidates(dataset, candidates=["linear", "gbt"], folds=2, grids=FAST_GRIDS)
    record = registry.record_training_run(db, report, auto_activate=True, n_boot=300)
    by_kind = {v.model_type: v for v in record.versions}
    assert set(by_kind) == {"linear", "gbt"}
    for kind in ("linear", "gbt"):
        decision = by_kind[kind].decision
        assert decision["passed_all"], (kind, decision["failed"], decision["criteria"])
        test_all = by_kind[kind].metrics["test"]["all"]
        assert test_all["rmse"] < 0.8 * test_all["rmse_raw"]
        # Every lead bucket with enough pairs improves; skill vs raw is positive.
        for bucket in ("24", "48", "72", "120", "168", "240"):
            assert by_kind[kind].metrics["test"][bucket]["rmse_change_vs_raw"] < 0
        assert test_all["skill_vs_raw"] > 0
    activated = record.activated
    assert activated is not None and activated.status == MLModelStatus.ACTIVE
    assert sum(v.status == MLModelStatus.ACTIVE for v in record.versions) == 1
    assert {v.status for v in record.versions} <= {MLModelStatus.ACTIVE, MLModelStatus.CANDIDATE}
    applicability = activated.applicability
    assert applicability["station_codes"] == [s["code"] for s in case.stations]
    assert applicability["spot_slugs"] == [
        "synthetic-spot-1",
        "synthetic-spot-2",
        "synthetic-spot-3",
    ]
    assert applicability["lead_min_h"] == 12.0 and applicability["lead_max_h"] == 240.0

    experiment = db.get(MLExperiment, record.experiment.id)
    assert experiment is not None and experiment.status == "finished"
    guards = {g["guard"] for g in experiment.dataset["leakage_guards"]}
    assert guards == {
        "train_valid_time_before_first_test_cycle_minus_gap",
        "features_at_or_before_cycle_time_targets_after",
    }
    assert experiment.split["gap_hours"] == pytest.approx(240 + 72)
    assert set(experiment.results["candidates"]) == {"linear", "gbt"}
    assert set(experiment.results["candidates"]["gbt"]["validation"]) >= {"all", "24", "240"}
    assert "passes every deployment criterion" in experiment.conclusion
    assert "Persistence reference RMSE" in experiment.conclusion

    # The applied correction: spots paired with the training buoys, trained lead range.
    corr = load_active_correction(db)
    assert corr is not None and corr.label == activated.label
    assert corr.applies_to("synthetic-spot-2", 96) and not corr.applies_to("elsewhere", 96)
    assert not corr.applies_to("synthetic-spot-2", 300)
    cycle = datetime(2025, 5, 1, tzinfo=UTC)
    wave_row = {
        "valid_time": cycle + timedelta(hours=96),
        "sig_wave_height_m": 2.4,
        "wave_period_s": 11.0,
        "wave_period_kind": "tp",
        "wave_direction_deg": 205.0,
        "primary_swell_height_m": 1.9,
        "primary_swell_period_s": 12.0,
        "primary_swell_direction_deg": 210.0,
        "wind_speed_kmh": 15.0,
        "wind_direction_deg": 100.0,
    }
    feats = features_from_wave_forecast(
        wave_row, cycle_time=cycle, persistence_hs_m=1.8, analysis_log_error=-0.14
    )
    corrected, info = corr.correct_hs(feats)
    assert info["applied"] and corrected is not None
    # The synthetic model is ~1.23x too high at 96 h: the correction lowers it.
    assert 0.5 * 2.4 <= corrected < 2.4 and 0.5 <= info["factor"] < 1.0

    # Verification of the corrected model on cycles after its training data.
    pair_forecasts(db, now=datetime(2025, 6, 15, tzinfo=UTC), full=True)
    summary = compute_accuracy_metrics(db, now=datetime(2025, 6, 15, tzinfo=UTC), n_boot=50)
    assert summary["corrected_model"] == activated.label and summary["corrected_pairs"] > 0
    rows = {
        r.model_version: r
        for r in db.scalars(
            select(ForecastAccuracyMetric).where(
                ForecastAccuracyMetric.scope == "global",
                ForecastAccuracyMetric.variable == "hs",
                ForecastAccuracyMetric.lead_bucket_h == -1,
                ForecastAccuracyMetric.period_start == datetime(2025, 3, 17, tzinfo=UTC),
            )
        )
    }
    assert set(rows) == {"raw", activated.label}
    corrected_row, raw_row = rows[activated.label], rows["raw"]
    assert corrected_row.rmse is not None and raw_row.rmse is not None
    assert corrected_row.bias is not None and raw_row.bias is not None
    assert corrected_row.rmse < raw_row.rmse
    assert abs(corrected_row.bias) < abs(raw_row.bias)

    # CLI round trip on the same registry.
    assert cli_main(["ml", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {v["status"] for v in listed} == {"active", "candidate"}
    assert cli_main(["ml", "evaluate", str(activated.id)]) == 0
    evaluated = json.loads(capsys.readouterr().out)
    assert evaluated["decision"]["passed_all"] is True
    assert cli_main(["ml", "export-manifest", str(activated.id), str(tmp_path / "m")]) == 0
    manifest = json.loads(capsys.readouterr().out)["manifest"]
    assert cli_main(["ml", "import-manifest", manifest]) == 0
    # Same registry, same artifact: the import resolves to the existing version.
    assert json.loads(capsys.readouterr().out)["label"] == activated.label
    assert cli_main(["ml", "deactivate", registry.DEFAULT_MODEL_NAME]) == 0
    assert json.loads(capsys.readouterr().out)["retired"] == activated.label
    assert cli_main(["ml", "activate", str(activated.id)]) == 0
    capsys.readouterr()
    out_path = tmp_path / "dataset.parquet"
    assert (
        cli_main(["ml", "build-dataset", "--source", SYNTHETIC_SOURCE, "--out", str(out_path)]) == 0
    )
    capsys.readouterr()
    saved = load_dataset(out_path)
    assert len(saved.frame) == dataset.summary["rows"]
    assert saved.summary["fingerprint"] == dataset.summary["fingerprint"]
    assert str(saved.frame["cycle_time"].dtype) == "datetime64[us, UTC]"


def test_end_to_end_pure_noise_is_rejected(
    db: Session,
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SYNTHETIC: model = latent sea state, observation = latent x independent noise, so
    no feature predicts the residual. Every candidate must be REJECTED."""
    case = make_case("noise", n_stations=3, days=150, lead_step=12, max_lead=240, seed=2)
    insert_case(db, case)
    monkeypatch.setattr(ml_models, "HYPERPARAMETER_GRIDS", FAST_GRIDS)
    code = cli_main(
        [
            "ml",
            "train",
            "--source",
            SYNTHETIC_SOURCE,
            "--candidates",
            "linear,rf,gbt",
            "--folds",
            "2",
            "--auto-activate",
            "--boot",
            "300",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["activated"] is None
    assert {v["status"] for v in out["versions"]} == {"rejected"}
    assert len(out["versions"]) == 3
    for v in out["versions"]:
        assert "a" in v["failed"]  # no 5 % RMSE gain from noise
    assert "No candidate passed" in out["conclusion"]
    versions = registry.list_versions(db)
    assert all(v.status == MLModelStatus.REJECTED for v in versions)
    assert registry.active_version(db, registry.DEFAULT_MODEL_NAME) is None
    assert load_active_correction(db) is None
    # A rejected version can never be activated (exit code 3).
    assert cli_main(["ml", "activate", str(versions[0].id)]) == 3
    assert "failed the deployment criteria" in capsys.readouterr().err
    assert cli_main(["ml", "evaluate", "999999"]) == 1
