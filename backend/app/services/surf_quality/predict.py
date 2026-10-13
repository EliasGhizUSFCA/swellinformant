"""Job B — score every forecast timestep of a model run.

Every prediction also gets its two confidence classes (methodology forecast-v1.1,
``forecast_confidence``): forecast confidence of the offshore forecast and local
confidence of the breaking-wave estimate at the break, with the inputs and rules used,
the run's issue time, the lead time and the research assessment local confidence used.

Where sources disagree about which wind is offshore (the spot's research assessment
fires a conflict rule on wind, or data/surf_spots/profile_decisions.json records the
offshore direction as disputed), the hour also stores the wind relation under the main
contradicting direction (``components.wind_dispute``, ``confidence_inputs.wind_dispute``),
its explanation names both relations when they differ, and its confidence reasons say
"Sources disagree about which wind is offshore here." The score keeps the profile's
direction. Decision records are read from the data directory once per run; a change to
that file alone takes effect at the next rescoring.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import (
    ForecastEnsembleStat,
    ForecastRun,
    ForecastSource,
    SpotResearchAssessment,
    SurfQualityPrediction,
    SurfSpot,
    WaveForecast,
)
from app.models.enums import ForecastRunStatus
from app.services.ml.hook import RunCorrector
from app.services.research.decisions import (
    DecisionValidationError,
    WindDispute,
    load_decisions,
    record_for,
    wind_dispute,
)
from app.services.surf_quality.confidence import completeness, confidence_label, run_agreement
from app.services.surf_quality.confidence_context import RunConfidenceContext, station_codes
from app.services.surf_quality.forecast_confidence import (
    METHOD as CONFIDENCE_METHOD,
)
from app.services.surf_quality.forecast_confidence import (
    EnsembleSignal,
    ForecastInputs,
    ResearchBasisInput,
    classify_forecast,
    classify_local,
    confidence_inputs,
    run_change_ratio,
    wind_dispute_inputs,
)
from app.services.surf_quality.models import SeaState, SpotParams
from app.services.surf_quality.scoring import (
    ALGORITHM_VERSION,
    disputed_wind_relation,
    evaluate,
)
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


def _profile_decisions() -> dict[str, list[dict[str, Any]]]:
    """The profile decision records (slug -> records); empty when the file is missing or
    invalid (an invalid file is logged, never fatal for scoring)."""
    try:
        return load_decisions()
    except (DecisionValidationError, OSError, ValueError) as exc:
        logger.warning("Profile decisions not used: %s", exc)
        return {}


def spot_wind_dispute(
    spot: SurfSpot,
    research: ResearchBasisInput | None,
    decisions: dict[str, list[dict[str, Any]]],
) -> WindDispute | None:
    """Whether sources disagree about the offshore wind at ``spot`` (see the docstring)."""
    record = record_for(decisions, spot.slug, "offshore_wind_direction")
    per_variable = {"wind": research.wind} if research is not None and research.wind else None
    return wind_dispute(per_variable, spot.offshore_wind_direction, record)


def _missing_swell(f: WaveForecast) -> bool:
    """No period or no direction for the swell (nor the bulk sea the model falls back to)."""
    period = f.primary_swell_period_s if f.primary_swell_period_s is not None else f.wave_period_s
    direction = (
        f.primary_swell_direction_deg
        if f.primary_swell_direction_deg is not None
        else f.wave_direction_deg
    )
    return period is None or direction is None


def compute_predictions_for_run(
    db: Session, run_id: int, force: bool = False, now: datetime | None = None
) -> int:
    run = db.get(ForecastRun, run_id)
    if run is None or run.status not in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL):
        return 0
    if run.predictions_computed_at and not force:
        return 0
    now = now or utcnow()
    # Staleness per source (queries.stale_after_hours), previous run, blend comparisons,
    # verification and research assessments: read once for the whole run.
    ctx = RunConfidenceContext.load(db, run, now)
    stale = ctx.stale

    ensembles = _ensemble_index(db, run)
    # An active ML correction of offshore Hs (ml.hook). A blend's members were already
    # corrected before blending, so a blend run is never corrected again here.
    corrector = None if run.source.kind == "blend" else RunCorrector.for_run(db, run)
    spots = {s.id: s for s in db.scalars(select(SurfSpot)).all()}
    decisions = _profile_decisions()
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
        stations = station_codes(spot)
        research = ctx.research.get(spot_id)
        dispute = spot_wind_dispute(spot, research, decisions)
        alternative = dispute.alternative_offshore_deg if dispute is not None else None
        stages = tide_stages([f.valid_time for f in forecasts], [f.sea_level_m for f in forecasts])
        for f, stage in zip(forecasts, stages, strict=True):
            sea = SeaState.from_orm(f, tide_stage=stage)
            ml_label, ml_info = _ml_correction(f), (f.extra or {}).get("ml_corrections")
            if corrector is not None:
                hit = corrector.factor(spot.slug, _row_values(f), f.valid_time)
                if hit is not None:
                    sea = sea.scaled(hit[0])
                    ml_label, ml_info = corrector.label, hit[1]
            result = evaluate(sea, params, alternative_offshore_deg=alternative)
            dispute_entry = (
                None
                if dispute is None
                else wind_dispute_inputs(
                    dispute,
                    result.wind_relation,
                    disputed_wind_relation(
                        sea.wind_speed_kmh, sea.wind_direction_deg, params, alternative
                    ),
                )
            )
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
            prev_hs, prev_swell = ctx.previous.get((spot_id, f.valid_time), (None, None))
            # Diagnostic only (see confidence.py): how much this hour changed since the
            # previous run of the same source.
            agreement = run_agreement(f.primary_swell_height_m, prev_swell)
            ensemble = _nearest_ensemble(ensembles.get(spot_id, []), f.valid_time)
            unc = breaking_uncertainty(
                result.breaking.mid_ft,
                lead_hours,
                ensemble=ensemble,
                between_model_sigma=ctx.between_model_sigma(spot_id, f.valid_time),
                calibration=params.calibration,
            )
            conf = confidence_index(unc.sigma_forecast, completeness(values), stale)
            families = ctx.hour_families(spot_id, f.valid_time)
            forecast_conf = classify_forecast(
                ForecastInputs(
                    index=conf,
                    sigma_source=unc.source,
                    lead_hours=lead_hours,
                    source_code=run.source.code,
                    is_demo=run.source.is_demo,
                    families=families,
                    ensemble=None
                    if ensemble is None
                    else EnsembleSignal(ensemble.hs_mean_m, ensemble.hs_spread_m, ensemble.members),
                    disagreement=ctx.disagreement(spot_id, f.valid_time),
                    run_change_ratio=run_change_ratio(
                        f.sig_wave_height_m
                        if f.sig_wave_height_m is not None
                        else f.primary_swell_height_m,
                        prev_hs if prev_hs is not None else prev_swell,
                    ),
                    verification=ctx.verification.lookup(families, stations, lead_hours),
                    missing_wind=f.wind_speed_kmh is None or f.wind_direction_deg is None,
                    missing_swell=_missing_swell(f),
                    stale=stale,
                    stale_after_hours=ctx.stale_after_hours,
                )
            )
            local_conf = classify_local(forecast_conf.cls, research)
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
                        **(
                            {
                                "wind_dispute": {
                                    k: dispute_entry[k]
                                    for k in (
                                        "alternative_offshore_deg",
                                        "relation",
                                        "relation_alternative",
                                        "differs",
                                    )
                                }
                            }
                            if dispute_entry is not None
                            else {}
                        ),
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
                    "forecast_confidence_class": forecast_conf.cls,
                    "local_confidence_class": local_conf.cls,
                    "confidence_method": CONFIDENCE_METHOD,
                    "confidence_inputs": confidence_inputs(
                        forecast_conf, local_conf, wind_dispute=dispute_entry
                    ),
                    "issued_at": run.issued_at,
                    "lead_hours": round(lead_hours, 2),
                    "research_assessment_id": local_conf.assessment_id,
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


def outdated_authoritative_runs(db: Session, now: datetime | None = None) -> list[int]:
    """Runs currently shown to users whose predictions predate ALGORITHM_VERSION, the
    confidence methodology (forecast-v1) or the spot's current research assessment.

    After an algorithm upgrade the live forecast is rescored at once instead of waiting
    for the next model cycle; likewise after new research evidence (``research load`` or a
    seed), so local confidence follows the current assessment. Older runs keep the version
    they were issued with (the prediction archive is the record of what was forecast).
    """
    from app.services.forecasts.queries import latest_runs_by_spot

    run_ids = sorted({r.id for r in latest_runs_by_spot(db, now).values()})
    if not run_ids:
        return []
    # Each spot's current research assessment; no row = never assessed (NULL on both sides).
    current_research = (
        select(SpotResearchAssessment.spot_id, SpotResearchAssessment.id)
        .where(SpotResearchAssessment.is_current)
        .subquery()
    )
    return list(
        db.scalars(
            select(SurfQualityPrediction.run_id)
            .outerjoin(
                current_research, current_research.c.spot_id == SurfQualityPrediction.spot_id
            )
            .where(
                SurfQualityPrediction.run_id.in_(run_ids),
                (SurfQualityPrediction.algorithm_version != ALGORITHM_VERSION)
                | SurfQualityPrediction.confidence_method.is_distinct_from(CONFIDENCE_METHOD)
                | SurfQualityPrediction.research_assessment_id.is_distinct_from(
                    current_research.c.id
                ),
            )
            .distinct()
        ).all()
    )


def compute_pending_predictions(db: Session, now: datetime | None = None) -> dict[str, int]:
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
        total += compute_predictions_for_run(db, run_id, now=now)
    rescored = outdated_authoritative_runs(db, now)
    for run_id in rescored:
        total += compute_predictions_for_run(db, run_id, force=True, now=now)
    from app.services.forecasts.archive import archive_authoritative

    archived = archive_authoritative(db, now)
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
