"""Job B — score every forecast timestep of a model run."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, SurfQualityPrediction, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.services.surf_quality.confidence import (
    completeness,
    confidence_label,
    confidence_score,
    run_agreement,
)
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import ALGORITHM_VERSION, evaluate
from app.services.surf_quality.sun import is_daylight

logger = logging.getLogger(__name__)

INSERT_CHUNK = 2000
MIN_TIDAL_RANGE_M = 0.15


def tide_stages(times: list[datetime], levels: list[float | None]) -> list[float | None]:
    """Position in the local tidal cycle: 0 = low, 1 = high (±12.5 h rolling window)."""
    if not times or all(v is None for v in levels):
        return [None] * len(times)
    series = pd.Series(levels, index=pd.DatetimeIndex(times), dtype="float64").sort_index()
    window = pd.Timedelta(hours=25)
    lo = series.rolling(window, center=True, min_periods=6).min()
    hi = series.rolling(window, center=True, min_periods=6).max()
    span = hi - lo
    stage = (series - lo) / span
    stage[(span < MIN_TIDAL_RANGE_M) | series.isna()] = float("nan")
    return [
        None if pd.isna(v) else round(float(v), 3) for v in stage.reindex(pd.DatetimeIndex(times))
    ]


def _previous_run(db: Session, run: ForecastRun) -> ForecastRun | None:
    return db.scalar(
        select(ForecastRun)
        .where(
            ForecastRun.source_id == run.source_id,
            ForecastRun.id != run.id,
            ForecastRun.issued_at <= run.issued_at,
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
        )
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )


def compute_predictions_for_run(db: Session, run_id: int, force: bool = False) -> int:
    run = db.get(ForecastRun, run_id)
    if run is None or run.status not in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL):
        return 0
    if run.predictions_computed_at and not force:
        return 0
    settings = get_settings()
    stale = (utcnow() - run.issued_at).total_seconds() > settings.forecast_stale_hours * 3600

    prev = _previous_run(db, run)
    prev_heights: dict[tuple[int, datetime], float | None] = {}
    if prev is not None:
        for spot_id, valid_time, h in db.execute(
            select(
                WaveForecast.spot_id, WaveForecast.valid_time, WaveForecast.primary_swell_height_m
            ).where(WaveForecast.run_id == prev.id)
        ):
            prev_heights[(spot_id, valid_time)] = h

    spots = {s.id: s for s in db.scalars(select(SurfSpot)).all()}
    by_spot: dict[int, list[WaveForecast]] = defaultdict(list)
    for row in db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == run.id)
        .order_by(WaveForecast.spot_id, WaveForecast.valid_time)
    ):
        by_spot[row.spot_id].append(row)

    rows: list[dict[str, object]] = []
    for spot_id, forecasts in by_spot.items():
        spot = spots.get(spot_id)
        if spot is None:
            continue
        params = SpotParams.from_orm(spot)
        stages = tide_stages([f.valid_time for f in forecasts], [f.sea_level_m for f in forecasts])
        for f, stage in zip(forecasts, stages, strict=True):
            sea = SeaState.from_orm(f, tide_stage=stage)
            result = evaluate(sea, params)
            lead_hours = (f.valid_time - run.issued_at).total_seconds() / 3600.0
            values = {
                k: getattr(f, k)
                for k in (
                    "primary_swell_height_m",
                    "primary_swell_period_s",
                    "primary_swell_direction_deg",
                    "wind_speed_kmh",
                    "wind_direction_deg",
                )
            }
            agreement = run_agreement(
                f.primary_swell_height_m, prev_heights.get((spot_id, f.valid_time))
            )
            conf = confidence_score(lead_hours, completeness(values), agreement, stale)
            rows.append(
                {
                    "forecast_id": f.id,
                    "run_id": run.id,
                    "spot_id": spot_id,
                    "valid_time": f.valid_time,
                    "is_daylight": is_daylight(spot.latitude, spot.longitude, f.valid_time),
                    "breaking_height_min_ft": result.breaking.min_ft,
                    "breaking_height_max_ft": result.breaking.max_ft,
                    "breaking_height_method": result.breaking.method,
                    "score": result.score,
                    "label": result.label,
                    "components": {
                        **result.components,
                        "agreement": agreement,
                        "tide_stage": stage,
                    },
                    "wind_relation": result.wind_relation,
                    "confidence": conf,
                    "confidence_label": confidence_label(conf).value,
                    "explanation": result.explanation,
                    "algorithm_version": ALGORITHM_VERSION,
                }
            )
    if rows:
        base = insert(SurfQualityPrediction)
        stmt = (
            base.on_conflict_do_update(
                index_elements=["forecast_id"],
                set_={c: base.excluded[c] for c in rows[0] if c != "forecast_id"},
            )
            if force
            else base.on_conflict_do_nothing(index_elements=["forecast_id"])
        )
        for i in range(0, len(rows), INSERT_CHUNK):
            db.execute(stmt, rows[i : i + INSERT_CHUNK])  # executemany: compiled once, batched
    run.predictions_computed_at = utcnow()
    db.commit()
    logger.info("Computed %d predictions for run %s", len(rows), run.run_key)
    return len(rows)


def compute_pending_predictions(db: Session) -> dict[str, int]:
    pending = db.scalars(
        select(ForecastRun.id)
        .where(
            ForecastRun.predictions_computed_at.is_(None),
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
        )
        .order_by(ForecastRun.issued_at)
    ).all()
    total = 0
    for run_id in pending:
        total += compute_predictions_for_run(db, run_id)
    return {"runs": len(pending), "predictions": total}
