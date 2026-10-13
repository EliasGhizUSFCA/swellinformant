"""Where a registered correction may apply, restricting it, and compressed exports.

Every station, spot slug and model here is SYNTHETIC: the ridge model is fitted on a
seeded random case (tests/synthetic_waves.py); nothing is a real measurement or forecast.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.cli import main as cli_main
from app.core.config import get_settings
from app.models import BuoyStation, MLModelVersion
from app.models.enums import MLModelStatus
from app.services.ml import registry
from app.services.ml.apply import clear_correction_cache, load_active_correction
from app.services.ml.dataset import assemble_frame
from app.services.ml.features import FEATURE_NAMES
from app.services.ml.models import TrainedCorrection, fit_correction
from tests.synthetic_waves import frames_for_assembly, make_case

pytestmark = pytest.mark.integration

PASSED = {"passed_all": True, "failed": [], "criteria": [], "n_test": 1000}


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
    """A ridge correction fitted on a small SYNTHETIC frame."""
    case = make_case("bias", n_stations=1, days=30, lead_step=12, max_lead=120, seed=8)
    fc, obs, stations = frames_for_assembly(case)
    return fit_correction(
        "linear", {"alpha": 1.0}, assemble_frame(fc, obs, stations), FEATURE_NAMES
    )


def _register(db: Session, model: TrainedCorrection, spots: list[str]) -> MLModelVersion:
    version = registry.register_version(
        db,
        name="synthetic_hs",
        model=model,
        experiment=None,
        metrics={},
        decision=PASSED,
        applicability={
            "station_codes": ["synthetic:a"],
            "spot_slugs": spots,
            "excluded_spots": {"spot-z": "upstream_regional"},
            "lead_min_h": model.lead_min,
            "lead_max_h": model.lead_max,
            "hs_kinds": ["spectral_hm0"],
        },
        training={},
        hyperparameters={"alpha": 1.0},
        status=MLModelStatus.CANDIDATE,
    )
    db.commit()
    return version


def _station(db: Session, code: str, spots: list[Any]) -> None:
    db.add(
        BuoyStation(
            code=code,
            provider="synthetic",
            name=f"SYNTHETIC {code}",
            latitude=0.0,
            longitude=0.0,
            metadata_={"paired_spots": spots},
        )
    )
    db.commit()


def test_spots_of_stations_without_test_pairs_are_excluded(db: Session) -> None:
    """A station that only contributed training rows was never tested: its spots are not
    corrected (unless another, tested station is their offshore reference)."""
    _station(db, "synthetic:tested", [{"slug": "spot-a", "relation": "offshore_reference"}])
    _station(
        db,
        "synthetic:train-only",
        [
            {"slug": "spot-b", "relation": "offshore_reference"},
            {"slug": "spot-a", "relation": "offshore_reference"},
        ],
    )
    _station(db, "synthetic:upstream", [{"slug": "spot-c", "relation": "upstream_regional"}])
    excluded: dict[str, str] = {}
    spots = registry.applicable_spots(
        db,
        ["synthetic:tested", "synthetic:train-only", "synthetic:upstream"],
        ["synthetic:tested", "synthetic:upstream"],
        excluded=excluded,
    )
    assert spots == ["spot-a"]
    assert excluded == {"spot-b": registry.NO_TEST_PAIRS, "spot-c": "upstream_regional"}
    # Without any tested station nothing applies.
    excluded = {}
    assert registry.applicable_spots(db, ["synthetic:train-only"], [], excluded=excluded) == []
    assert excluded == {"spot-a": registry.NO_TEST_PAIRS, "spot-b": registry.NO_TEST_PAIRS}


def test_restrict_removes_spots_with_reasons(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection
) -> None:
    version = _register(db, small_model, ["spot-a", "spot-b", "spot-c"])
    with pytest.raises(registry.RegistryError, match="does not apply"):
        registry.restrict_applicability(db, version.id, {"spot-x": "why"})
    with pytest.raises(registry.RegistryError, match="reason"):
        registry.restrict_applicability(db, version.id, {"spot-a": "  "})
    with pytest.raises(registry.RegistryError, match="reason"):
        registry.restrict_applicability(db, version.id, {})
    with pytest.raises(LookupError):
        registry.restrict_applicability(db, 999999, {"spot-a": "why"})
    out = registry.restrict_applicability(db, version.id, {"spot-b": "no held-out benefit"})
    db.refresh(out)
    app = out.applicability
    assert app["spot_slugs"] == ["spot-a", "spot-c"]
    assert app["excluded_spots"] == {
        "spot-z": "upstream_regional",
        "spot-b": "no held-out benefit",
    }
    assert app["restrictions"][0]["excluded"] == {"spot-b": "no held-out benefit"}
    # The applied correction follows the restricted list.
    registry.activate(db, version.id)
    correction = load_active_correction(db, "synthetic_hs")
    assert correction is not None and correction.spot_slugs == frozenset({"spot-a", "spot-c"})
    # An active version is never restricted in place (workers cache its applicability).
    with pytest.raises(registry.RegistryError, match="active"):
        registry.restrict_applicability(db, version.id, {"spot-a": "why"})


def test_compressed_export_round_trip(
    db: Session, artifact_dir: Path, small_model: TrainedCorrection, tmp_path: Path
) -> None:
    original = _register(db, small_model, ["spot-a"])
    manifest_path = registry.export_manifest(db, original.id, tmp_path / "out", compress=True)
    manifest = json.loads(manifest_path.read_text())
    art = manifest["artifact"]
    data = (manifest_path.parent / art["file"]).read_bytes()
    assert art["source_sha256"] == original.artifact_sha256
    assert art["sha256"] == registry.sha256_bytes(data) != original.artifact_sha256
    assert art["bytes"] == len(data) and art["compression"].startswith("joblib zlib")
    # Same environment: the version that already holds the original artifact is found.
    assert registry.import_manifest(db, manifest_path).id == original.id
    X = np.array([[24.0, 2.0, math.log(2.0)] + [0.0] * (len(FEATURE_NAMES) - 3)])
    expected = registry.load_artifact(original).factors(X)[0]
    # Another environment: the compressed artifact is registered and gives the same factors.
    Path(original.artifact_path or "").unlink()
    db.delete(original)
    db.commit()
    imported = registry.import_manifest(db, manifest_path)
    assert imported.artifact_sha256 == art["sha256"]
    assert imported.status == MLModelStatus.CANDIDATE
    np.testing.assert_allclose(registry.load_artifact(imported).factors(X)[0], expected)


def test_recompress_refuses_a_changed_model(
    db: Session,
    artifact_dir: Path,
    small_model: TrainedCorrection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = _register(db, small_model, ["spot-a"])
    data = registry.read_verified(registry.registered_artifact(version), version.artifact_sha256)
    monkeypatch.setattr(registry, "RESERIALISE_TOLERANCE", -1.0)  # any difference fails
    with pytest.raises(registry.RegistryError, match="re-serialised model differs"):
        registry.recompress_artifact(data, version.label)
    with pytest.raises(registry.ArtifactIntegrityError):
        registry.recompress_artifact(b"not a joblib payload", version.label)


def test_restrict_and_compressed_export_cli(
    db: Session,
    artifact_dir: Path,
    small_model: TrainedCorrection,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    version = _register(db, small_model, ["spot-a", "spot-b"])
    args = ["ml", "restrict", str(version.id), "--exclude-spot", "spot-b"]
    assert cli_main([*args, "--reason", "no held-out benefit at its buoy"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applicability"]["spot_slugs"] == ["spot-a"]
    assert cli_main(
        ["ml", "restrict", str(version.id), "--exclude-spot", "spot-x", "--reason", "r"]
    )
    capsys.readouterr()
    assert cli_main(["ml", "export-manifest", str(version.id), str(tmp_path), "--compress"]) == 0
    manifest = json.loads(Path(json.loads(capsys.readouterr().out)["manifest"]).read_text())
    assert manifest["artifact"]["source_sha256"] == version.artifact_sha256
    assert manifest["model"]["applicability"]["spot_slugs"] == ["spot-a"]
