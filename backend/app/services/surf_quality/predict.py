"""Job B — score every forecast timestep of a model run."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    ForecastComparison,
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    SurfQualityPrediction,
    SurfSpot,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.ml.hook import RunCorrector
from app.services.surf_quality.confidence import completeness, confidence_label, run_agreement
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import ALGORITHM_VERSION, evaluate
from app.services.surf_quality.sun import is_daylight
from app.services.surf_quality.uncertainty import (
    EnsembleInfo,
    breaking_uncertainty,
    confidence_index,
)

logger = logging.getLogger(__name__)

INSERT_CHUNK = 2000
MIN_TIDAL_RANGE_M = 0.15
# An ensemble row is used for a forecast hour only if it is this close in time, and only
# from an ensemble run issued within ENSEMBLE_RUN_WINDOW of the forecast run.
ENSEMBLE_MAX_GAP = timedelta(hours=3)
ENSEMBLE_RUN_WINDOW = timedelta(hours=24)


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


def _ensemble_index(
    db: Session, run: ForecastRun
) -> dict[int, list[tuple[datetime, EnsembleInfo]]]:
    """Ensemble rows per spot from the newest ensemble run close to this run's issue time."""
    ens_run = db.scalar(
        select(ForecastRun)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(
            ForecastSource.kind == "ensemble",
            ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
            ForecastRun.issued_at <= run.issued_at + ENSEMBLE_RUN_WINDOW,
            ForecastRun.issued_at >= run.issued_at - ENSEMBLE_RUN_WINDOW,
        )
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(1)
    )
    out: dict[int, list[tuple[datetime, EnsembleInfo]]] = defaultdict(list)
    if ens_run is None:
        return out
    for row in db.scalars(
        select(ForecastEnsembleStat)
        .where(ForecastEnsembleStat.run_id == ens_run.id)
        .order_by(ForecastEnsembleStat.spot_id, ForecastEnsembleStat.valid_time)
    ):
        out[row.spot_id].append(
            (
                row.valid_time,
                EnsembleInfo(
                    hs_mean_m=row.hs_mean_m,
                    hs_spread_m=row.hs_spread_m,
                    tp_mean_s=row.tp_mean_s,
                    tp_spread_s=row.tp_spread_s,
                    members=row.members,
                ),
            )
        )
    return out


def _nearest_ensemble(
    rows: list[tuple[datetime, EnsembleInfo]], t: datetime
) -> EnsembleInfo | None:
    best: tuple[timedelta, EnsembleInfo] | None = None
    for vt, info in rows:
        gap = abs(vt - t)
        if gap <= ENSEMBLE_MAX_GAP and (best is None or gap < best[0]):
            best = (gap, info)
    return best[1] if best else None


def _between_model_sigma(db: Session, run: ForecastRun) -> dict[tuple[int, datetime], float]:
    """σ_ln(Hs) between independent model families, from the blend's comparisons.

    With two families σ ≈ |ln(Hs_a / Hs_b)| / 2 (their standard deviation); with more,
    half the largest pairwise log ratio is a lower bound, which is the conservative use.
    """
    out: dict[tuple[int, datetime], float] = {}
    for spot_id, valid_time, ratio in db.execute(
        select(
            ForecastComparison.spot_id,
            ForecastComparison.valid_time,
            ForecastComparison.hs_log_ratio_max,
        ).where(ForecastComparison.blend_run_id == run.id)
    ):
        if ratio is not None:
            out[(spot_id, valid_time)] = ratio / 2.0
    return out


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

    ensembles = _ensemble_index(db, run)
    between = _between_model_sigma(db, run)
    # An active ML correction of offshore Hs (ml.hook). A blend's members were already
    # corrected before blending, so a blend run is never corrected again here.
    corrector = None if run.source.kind == "blend" else RunCorrector.for_run(db, run)
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
            ml_label, ml_info = _ml_correction(f), (f.extra or {}).get("ml_corrections")
            if corrector is not None:
                hit = corrector.factor(spot.slug, _row_values(f), f.valid_time)
                if hit is not None:
                    sea = sea.scaled(hit[0])
                    ml_label, ml_info = corrector.label, hit[1]
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
            # Diagnostic only (see confidence.py): how much this hour changed since the
            # previous run of the same source.
            agreement = run_agreement(
                f.primary_swell_height_m, prev_heights.get((spot_id, f.valid_time))
            )
            unc = breaking_uncertainty(
                result.breaking.mid_ft,
                lead_hours,
                ensemble=_nearest_ensemble(ensembles.get(spot_id, []), f.valid_time),
                between_model_sigma=between.get((spot_id, f.valid_time)),
                calibration=params.calibration,
            )
            conf = confidence_index(unc.sigma_forecast, completeness(values), stale)
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
                    "breaking_height_mid_ft": result.breaking.mid_ft,
                    "breaking_p10_ft": unc.p10_ft,
                    "breaking_p90_ft": unc.p90_ft,
                    "uncertainty_kind": unc.kind,
                    "ml_model_version": ml_label,
                    "score": result.score,
                    "label": result.label,
                    "components": {
                        **result.components,
                        "run_agreement": agreement,
                        "tide_stage": stage,
                        "uncertainty": unc.as_dict(),
                        **({"ml_correction": ml_info} if ml_info else {}),
                        "breaking": {
                            "partitions": result.breaking.partitions,
                            "assumptions": result.breaking.assumptions,
                            "depth_limited": result.breaking.depth_limited,
                        },
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


def outdated_authoritative_runs(db: Session) -> list[int]:
    """Runs currently shown to users whose predictions predate ALGORITHM_VERSION.

    After an algorithm upgrade the live forecast is rescored at once instead of waiting
    for the next model cycle. Older runs keep the version they were issued with (the
    prediction archive is the record of what was forecast).
    """
    from app.services.forecasts.queries import latest_runs_by_spot

    run_ids = sorted({r.id for r in latest_runs_by_spot(db).values()})
    if not run_ids:
        return []
    return list(
        db.scalars(
            select(SurfQualityPrediction.run_id)
            .where(
                SurfQualityPrediction.run_id.in_(run_ids),
                SurfQualityPrediction.algorithm_version != ALGORITHM_VERSION,
            )
            .distinct()
        ).all()
    )


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
    rescored = outdated_authoritative_runs(db)
    for run_id in rescored:
        total += compute_predictions_for_run(db, run_id, force=True)
    from app.services.forecasts.archive import archive_authoritative

    archived = archive_authoritative(db)
    return {
        "runs": len(pending),
        "rescored_runs": len(rescored),
        "predictions": total,
        "archived": archived["archived"],
    }


def _row_values(f: WaveForecast) -> dict[str, object]:
    """The forecast values the correction's features are built from (ml.features)."""
    return {
        name: getattr(f, name)
        for name in (
            "sig_wave_height_m",
            "wave_period_s",
            "wave_period_kind",
            "wave_direction_deg",
            "primary_swell_height_m",
            "primary_swell_period_s",
            "primary_swell_direction_deg",
            "wind_speed_kmh",
            "wind_direction_deg",
        )
    }


def _ml_correction(f: WaveForecast) -> str | None:
    """Label of the correction a blend applied to one of this hour's members, if any."""
    applied = (f.extra or {}).get("ml_corrections") or {}
    labels = sorted({str(v.get("model")) for v in applied.values() if isinstance(v, dict)})
    return ", ".join(labels)[:60] or None
