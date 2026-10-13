"""Apply the ACTIVE offshore Hs correction to forecasts.

Only a version that passed every deployment criterion on held-out data and was then
explicitly activated is ever applied (app.services.ml.registry). The correction acts on
OFFSHORE significant wave height (Hm0) only; breaking (surf) height is derived later by
the nearshore model from the corrected offshore value and is not itself corrected.

Rules enforced here:

* applicability: only at spots paired with a training station
  (``buoy_stations.metadata.paired_spots``) and only within the trained lead range;
  never extrapolated;
* the multiplicative factor is clamped to [0.5, 2.0];
* when a required feature is missing (see TrainedCorrection.required_features) the raw
  value is returned unchanged, with the reason in the info dict.

``load_active_correction`` caches the loaded model per process and per name; every call
runs one cheap query for the active version id and sha256 and reloads only when they
changed (activation, deactivation or a new version), so workers pick up a change on
their next call. Artifacts that fail the sha256 check, lie outside ML_ARTIFACT_DIR or
cannot be unpickled are never applied (the forecast stays raw and the error is logged).

The prediction job builds the features from a ``wave_forecasts`` row with
:func:`app.services.ml.features.features_from_wave_forecast` (re-exported here as
``build_features``); its docstring documents the column -> feature mapping.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import MLModelVersion
from app.models.enums import MLModelStatus
from app.services.ml.features import feature_matrix, features_from_wave_forecast
from app.services.ml.models import TrainedCorrection
from app.services.ml.registry import (
    DEFAULT_MODEL_NAME,
    ArtifactIntegrityError,
    load_artifact,
)

logger = logging.getLogger(__name__)

build_features = features_from_wave_forecast

__all__ = [
    "Correction",
    "build_features",
    "clear_correction_cache",
    "features_from_wave_forecast",
    "load_active_correction",
]


@dataclass(frozen=True)
class Correction:
    """A loaded, verified ACTIVE correction and where it may be applied."""

    version_id: int
    label: str
    sha256: str
    model: TrainedCorrection
    station_codes: frozenset[str]
    spot_slugs: frozenset[str]
    sources: frozenset[str]
    families: frozenset[str]
    lead_min_h: float
    lead_max_h: float
    training_end: datetime | None

    def in_lead_range(self, lead_hours: float) -> bool:
        return (
            math.isfinite(lead_hours)
            and self.lead_min_h - 1e-9 <= lead_hours <= self.lead_max_h + 1e-9
        )

    def applies_to(self, spot_slug: str, lead_hours: float, *, family: str | None = None) -> bool:
        """True when the spot is paired with a training station and the lead is in range.

        ``family`` (optional): the forecast source's dependency family; when given it
        must be one the model was trained on (a GFS-Wave correction is not applied to
        ECMWF forecasts).
        """
        if spot_slug not in self.spot_slugs or not self.in_lead_range(float(lead_hours)):
            return False
        return family is None or family in self.families

    def correct_hs(
        self, features: Mapping[str, float | None]
    ) -> tuple[float | None, dict[str, Any]]:
        """(corrected Hs in m, info). Returns the raw value unchanged when it cannot apply."""
        raw = features.get("model_hs_m")
        info: dict[str, Any] = {"model": self.label, "raw": raw, "factor": 1.0, "applied": False}
        if raw is None or not math.isfinite(float(raw)):
            info["reason"] = "missing model Hs"
            return None, info
        lead = features.get("lead_hours")
        if lead is None or not self.in_lead_range(float(lead)):
            info["reason"] = "lead outside the trained range"
            return float(raw), info
        missing = [n for n in self.model.required_features if features.get(n) is None]
        if missing:
            info["reason"] = f"missing required features {missing}"
            return float(raw), info
        X = feature_matrix(features, self.model.feature_names)
        factors, applied = self.model.factors(X)
        if not bool(applied[0]):
            info["reason"] = "not applicable"
            return float(raw), info
        factor = float(factors[0])
        corrected = float(raw) * factor
        info.update({"factor": factor, "applied": True, "corrected": corrected})
        return corrected, info

    def correct_frame(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """(corrected Hs, applied mask) for a dataset-style frame (verification job)."""
        if frame.empty:
            return np.zeros(0), np.zeros(0, dtype=bool)
        station_ok = frame["station_code"].isin(list(self.station_codes)).to_numpy()
        factors, applied = self.model.factors(self.model.matrix(frame))
        applied = applied & station_ok
        factors = np.where(applied, factors, 1.0)
        return frame["model_hs_m"].to_numpy(dtype=float) * factors, applied


_cache: dict[str, tuple[int, str, Correction]] = {}
_lock = threading.Lock()


def clear_correction_cache() -> None:
    with _lock:
        _cache.clear()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return pd.Timestamp(value).to_pydatetime()
    except (TypeError, ValueError):
        return None


def _build(version: MLModelVersion) -> Correction:
    model = load_artifact(version)
    app = version.applicability or {}
    return Correction(
        version_id=version.id,
        label=version.label,
        sha256=version.artifact_sha256 or "",
        model=model,
        station_codes=frozenset(app.get("station_codes") or []),
        spot_slugs=frozenset(app.get("spot_slugs") or []),
        sources=frozenset(app.get("sources") or []),
        families=frozenset(app.get("families") or []),
        lead_min_h=float(app.get("lead_min_h", model.lead_min)),
        lead_max_h=float(app.get("lead_max_h", model.lead_max)),
        training_end=_parse_time((version.training or {}).get("data_end")),
    )


def load_active_correction(db: Session, name: str = DEFAULT_MODEL_NAME) -> Correction | None:
    """The ACTIVE correction for ``name`` or None (none active, disabled, or unverifiable)."""
    if not get_settings().ml_corrections_enabled:
        return None
    row = db.execute(
        select(MLModelVersion.id, MLModelVersion.artifact_sha256).where(
            MLModelVersion.name == name, MLModelVersion.status == MLModelStatus.ACTIVE
        )
    ).first()
    if row is None:
        with _lock:
            _cache.pop(name, None)
        return None
    version_id, digest = int(row[0]), str(row[1] or "")
    with _lock:
        cached = _cache.get(name)
        if cached is not None and cached[0] == version_id and cached[1] == digest:
            return cached[2]
    version = db.get(MLModelVersion, version_id)
    if version is None:
        return None
    try:
        correction = _build(version)
    except ArtifactIntegrityError:
        logger.exception("Active model %s failed verification; not applied", version.label)
        with _lock:
            _cache.pop(name, None)
        return None
    except Exception:
        # A verified file can still fail to unpickle (e.g. scikit-learn upgraded since
        # training) or hold an unusable model. Forecasts then stay raw; the prediction and
        # verification jobs must not fail because of it.
        logger.exception("Active model %s could not be loaded; not applied", version.label)
        with _lock:
            _cache.pop(name, None)
        return None
    with _lock:
        _cache[name] = (version_id, digest, correction)
    return correction
