"""The corrections shipped in backend/ml_models/ import, verify and apply where documented.

The manifests and artifacts are the REAL exported files (docs/ml-evaluation.md sections 10
and 12.4); the probe inputs fed to the model are SYNTHETIC values chosen for the test, not
forecasts. Nothing is activated outside this test's database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import MLModelStatus
from app.services.ml import registry
from app.services.ml.apply import clear_correction_cache, load_active_correction

pytestmark = pytest.mark.integration

SHIPPED = Path(__file__).resolve().parents[2] / "ml_models" / "offshore_hs_residual"
V6 = SHIPPED / "offshore_hs_residual-v6.manifest.json"
V4 = SHIPPED / "offshore_hs_residual-v4.manifest.json"
V6_SPOTS = {
    "jaws-peahi",
    "mavericks",
    "ocean-beach-sf",
    "pipeline",
    "sunset-beach",
    "trestles",
    "waimea-bay",
}
WA_SPOTS = {"margaret-river-main-break", "the-box", "north-point-gracetown"}


@pytest.fixture
def artifact_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "artifacts"
    monkeypatch.setattr(get_settings(), "ml_artifact_dir", str(root))
    monkeypatch.setattr(get_settings(), "ml_corrections_enabled", True)
    clear_correction_cache()
    yield root
    clear_correction_cache()


def test_v6_manifest_matches_its_artifact_and_stays_small() -> None:
    manifest = json.loads(V6.read_text())
    art = manifest["artifact"]
    path = SHIPPED / art["file"]
    assert path.stat().st_size == art["bytes"] < 5_000_000
    assert registry.sha256_bytes(path.read_bytes()) == art["sha256"]
    assert art["source_sha256"] != art["sha256"]  # compressed re-serialisation (12.4)


def test_v6_imports_as_candidate_and_applies_only_where_tested(
    db: Session, artifact_dir: Path
) -> None:
    version = registry.import_manifest(db, V6, artifact_root=artifact_dir)
    assert version.status == MLModelStatus.CANDIDATE  # never imported as ACTIVE
    assert version.model_type == "rf"
    decision = version.decision or {}
    assert decision["passed_all"] and decision["failed"] == []
    app = version.applicability or {}
    assert set(app["spot_slugs"]) == V6_SPOTS
    assert app["hs_kinds"] == ["spectral_hm0"] and app["families"] == ["ncep_gfs"]
    assert (app["lead_min_h"], app["lead_max_h"]) == (3.0, 240.0)
    excluded = app["excluded_spots"]
    assert "rincon" in excluded and "46053" in excluded["rincon"]
    assert {s: excluded[s] for s in WA_SPOTS} == dict.fromkeys(WA_SPOTS, registry.NO_TEST_PAIRS)
    assert app["test_station_codes"] == [
        "ndbc:46026",
        "ndbc:46053",
        "ndbc:46224",
        "ndbc:51201",
        "ndbc:51205",
    ]
    # Importing the same files again is a no-op.
    assert registry.import_manifest(db, V6, artifact_root=artifact_dir).id == version.id

    registry.activate(db, version.id)
    correction = load_active_correction(db)
    assert correction is not None and correction.version_id == version.id
    for slug in V6_SPOTS:
        assert correction.applies_to(slug, 24.0, family="ncep_gfs")
    for slug in {"rincon", *WA_SPOTS, "jeffreys-bay"}:
        assert not correction.applies_to(slug, 24.0, family="ncep_gfs")
    assert not correction.applies_to("trestles", 0.0, family="ncep_gfs")  # below 3 h
    assert not correction.applies_to("trestles", 246.0, family="ncep_gfs")
    assert not correction.applies_to("trestles", 24.0, family="ecmwf_ifs")

    # SYNTHETIC probe inputs inside the trained ranges: finite factors within the clip.
    model = correction.model
    X = registry._probe_matrix(model, rows=200)
    factors, applied = model.factors(X)
    assert applied.all() and np.isfinite(factors).all()
    assert (factors >= model.factor_min).all() and (factors <= model.factor_max).all()

    registry.deactivate(db, version.name)
    clear_correction_cache()
    assert load_active_correction(db) is None


def test_v4_still_imports_as_inactive_candidate_for_the_wa_spots(
    db: Session, artifact_dir: Path
) -> None:
    version = registry.import_manifest(db, V4, artifact_root=artifact_dir)
    assert version.status == MLModelStatus.CANDIDATE
    assert set((version.applicability or {})["spot_slugs"]) == WA_SPOTS
    assert registry.active_version(db, version.name) is None
