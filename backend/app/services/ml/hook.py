"""The active offshore-Hs correction inside the forecast pipeline.

A correction (app.services.ml.apply) was trained and validated on one model family's raw
forecasts at buoys. It is applied at exactly one point per value, never twice:

* **multi-model deployments**: the blend (forecasts.blend) corrects each MEMBER whose
  family the correction was trained on, before combining, so the blend combines the
  validated corrected member with the other, uncorrected families;
* **single-source deployments**: the prediction job (surf_quality.predict) corrects a
  run of that family directly. A blend run is never corrected again (its family is
  ``blend``).

Only offshore heights are scaled (Hs and every partition height by the same factor,
which preserves the partitions' share of the energy); periods, directions and wind are
unchanged. The breaking-height model then runs on the corrected offshore values, so the
surf estimate inherits the correction without being corrected or validated itself.

Features are built the way training built them (ml.dataset): the model's values at the
valid time plus, when the paired station has them, the station's spectral Hm0 at or
before the cycle time (within ``PERSISTENCE_MAX_AGE``) and the analysis error against the
archived lead-0 point forecast of the same cycle. A feature the model requires but that
is unavailable leaves the value raw (Correction.correct_hs).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BuoyObservation, BuoyStation, ForecastRun, PointForecast, SurfSpot
from app.services.ml.apply import Correction, build_features, load_active_correction
from app.services.ml.features import floored_log
from app.services.ml.registry import transferable_slugs
from app.services.surf_quality.models import HEIGHT_FIELDS
from app.services.verification.pairing import PERSISTENCE_MAX_AGE


def scale_heights(values: Mapping[str, Any], factor: float) -> dict[str, Any]:
    """``values`` with every offshore height multiplied by ``factor`` (None stays None)."""
    out = dict(values)
    for name in HEIGHT_FIELDS:
        if out.get(name) is not None:
            out[name] = float(out[name]) * factor
    return out


@dataclass
class RunCorrector:
    """The active correction, prepared for one forecast run of a matching family."""

    correction: Correction
    family: str
    cycle_time: datetime
    station_for_spot: dict[str, str]
    # station code -> (persistence Hm0 at cycle time, analysis log error)
    context: dict[str, tuple[float | None, float | None]]

    @property
    def label(self) -> str:
        return self.correction.label

    @classmethod
    def for_run(cls, db: Session, run: ForecastRun) -> RunCorrector | None:
        """None unless a correction is active and was trained on this run's family."""
        correction = load_active_correction(db)
        if correction is None or run.source is None:
            return None
        family = run.source.family
        if not correction.families or family not in correction.families:
            return None
        cycle = run.wave_model_run_at or run.issued_at
        stations = db.scalars(
            select(BuoyStation).where(BuoyStation.code.in_(sorted(correction.station_codes)))
        ).all()
        station_for_spot: dict[str, str] = {}
        context: dict[str, tuple[float | None, float | None]] = {}
        for st in stations:
            for slug in sorted(transferable_slugs(st.metadata_)):
                if slug in correction.spot_slugs:
                    station_for_spot.setdefault(slug, st.code)
            context[st.code] = _station_context(db, st, cycle, correction.sources)
        return cls(correction, family, cycle, station_for_spot, context)

    def factor(
        self, spot_slug: str, values: Mapping[str, Any], valid_time: datetime
    ) -> tuple[float, dict[str, Any]] | None:
        """(multiplicative factor, info) when the correction applies to this value."""
        lead = (valid_time - self.cycle_time).total_seconds() / 3600.0
        raw = values.get("sig_wave_height_m")
        if not raw or not self.correction.applies_to(spot_slug, lead, family=self.family):
            return None
        station = self.station_for_spot.get(spot_slug)
        persistence, analysis = self.context.get(station or "", (None, None))
        features = build_features(
            {**values, "valid_time": valid_time},
            cycle_time=self.cycle_time,
            persistence_hs_m=persistence,
            analysis_log_error=analysis,
        )
        corrected, info = self.correction.correct_hs(features)
        if not info.get("applied") or corrected is None:
            return None
        return corrected / float(raw), {**info, "station": station, "lead_hours": round(lead, 1)}


def spot_slugs(db: Session) -> dict[int, str]:
    return dict(db.execute(select(SurfSpot.id, SurfSpot.slug)).all())


def _station_context(
    db: Session, station: BuoyStation, cycle: datetime, sources: frozenset[str]
) -> tuple[float | None, float | None]:
    """Spectral Hm0 observed at or before ``cycle`` and the lead-0 analysis log error."""
    obs = db.scalar(
        select(BuoyObservation.hs_m)
        .where(
            BuoyObservation.station_id == station.id,
            BuoyObservation.hs_kind == "spectral_hm0",
            BuoyObservation.hs_m.is_not(None),
            BuoyObservation.qc_flag != "bad",
            BuoyObservation.observed_at <= cycle,
            BuoyObservation.observed_at >= cycle - PERSISTENCE_MAX_AGE,
        )
        .order_by(BuoyObservation.observed_at.desc())
        .limit(1)
    )
    if obs is None:
        return None, None
    lead0 = db.scalar(
        select(PointForecast.hs_m)
        .where(
            PointForecast.station_id == station.id,
            PointForecast.source_code.in_(sorted(sources)),
            PointForecast.cycle_time == cycle,
            PointForecast.lead_hours == 0,
            PointForecast.hs_m.is_not(None),
        )
        .limit(1)
    )
    analysis = None if lead0 is None else float(floored_log(obs) - floored_log(lead0))
    return float(obs), analysis
