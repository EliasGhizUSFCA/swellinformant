"""Read models for the developer accuracy dashboard.

Everything here reads verification tables written by the observation, verification and ML
jobs (``prediction_validation``, ``forecast_accuracy_metrics``, ``ml_model_versions``,
``ml_experiments``, ``buoy_stations``). Nothing is computed from forecasts that have no
matching observation, and an empty database gives empty results, never placeholders.

What the numbers are: forecast minus observation of OFFSHORE quantities (Hs, Tp,
direction) at buoys. They are not breaking-height errors: no nearshore observations exist
yet, so the breaking-wave model is not validated by anything shown here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import Select, Text, cast, func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import (
    BuoyObservation,
    BuoyStation,
    ForecastAccuracyMetric,
    MLExperiment,
    MLModelVersion,
    PredictionValidation,
)
from app.schemas.accuracy import (
    AccuracyOverview,
    DailyStat,
    ExperimentSummary,
    HistogramBin,
    MetricRow,
    ModelVersionSummary,
    PairCount,
    PairsOut,
    ScatterPoint,
    StationSummary,
)

UNITS = {"hs": "m", "tp": "s", "dir": "deg"}
HISTOGRAM_BINS = 40
NOTES = [
    "Errors are forecast minus observation of offshore quantities at buoys, not surf "
    "(breaking) height: no nearshore observations exist yet.",
    "RMSE and scatter index favour forecasts that are biased low; read them with bias and HH.",
    "Metrics come only from held-out pairs: a forecast is paired with an observation "
    "taken after the forecast was issued.",
]


def _paired_spots(station: BuoyStation) -> list[str]:
    spots = (station.metadata_ or {}).get("paired_spots") or []
    return [s["slug"] for s in spots if isinstance(s, dict) and s.get("slug")]


def _dataset_version(model: MLModelVersion) -> str | None:
    """The training dataset's identity as recorded by the training job, if any."""
    for source in (model.training or {}, (model.experiment.dataset if model.experiment else {})):
        for key in ("dataset_version", "dataset_sha256", "fingerprint"):
            value = source.get(key)
            if value:
                return str(value)
    return None


def _test_metrics(model: MLModelVersion) -> dict[str, Any]:
    metrics = model.metrics or {}
    test = metrics.get("test")
    return test if isinstance(test, dict) else metrics


def overview(db: Session) -> AccuracyOverview:
    pair_counts = [
        PairCount(
            source_code=src, model_version=mv, variable=var, n=n, first_valid=lo, last_valid=hi
        )
        for src, mv, var, n, lo, hi in db.execute(
            select(
                PredictionValidation.source_code,
                PredictionValidation.model_version,
                PredictionValidation.variable,
                func.count(),
                func.min(PredictionValidation.valid_time),
                func.max(PredictionValidation.valid_time),
            )
            .group_by(
                PredictionValidation.source_code,
                PredictionValidation.model_version,
                PredictionValidation.variable,
            )
            .order_by(PredictionValidation.source_code, PredictionValidation.variable)
        ).all()
    ]
    obs = {
        sid: (n, latest)
        for sid, n, latest in db.execute(
            select(
                BuoyObservation.station_id, func.count(), func.max(BuoyObservation.observed_at)
            ).group_by(BuoyObservation.station_id)
        ).all()
    }
    pairs = dict(
        db.execute(
            select(PredictionValidation.station_id, func.count()).group_by(
                PredictionValidation.station_id
            )
        ).all()
    )
    stations = [
        StationSummary(
            code=s.code,
            name=s.name,
            provider=s.provider,
            latitude=s.latitude,
            longitude=s.longitude,
            water_depth_m=s.water_depth_m,
            paired_spots=_paired_spots(s),
            observations=obs.get(s.id, (0, None))[0],
            latest_observation=obs.get(s.id, (0, None))[1],
            pairs=pairs.get(s.id, 0),
        )
        for s in db.scalars(select(BuoyStation).order_by(BuoyStation.code)).all()
    ]
    models = [
        ModelVersionSummary(
            id=m.id,
            label=m.label,
            name=m.name,
            version=m.version,
            status=m.status.value,
            model_type=m.model_type,
            target=m.target,
            created_at=m.created_at,
            activated_at=m.activated_at,
            retired_at=m.retired_at,
            experiment_id=m.experiment_id,
            training=m.training or {},
            dataset_version=_dataset_version(m),
            decision=m.decision or {},
            applicability=m.applicability or {},
            test_metrics=_test_metrics(m),
        )
        for m in db.scalars(
            select(MLModelVersion).order_by(MLModelVersion.created_at.desc()).limit(50)
        ).all()
    ]
    experiments = [
        ExperimentSummary(
            id=e.id,
            name=e.name,
            target=e.target,
            status=e.status,
            created_at=e.created_at,
            finished_at=e.finished_at,
            conclusion=e.conclusion,
        )
        for e in db.scalars(
            select(MLExperiment).order_by(MLExperiment.created_at.desc()).limit(20)
        ).all()
    ]
    buckets = sorted(db.scalars(select(ForecastAccuracyMetric.lead_bucket_h).distinct()).all())
    return AccuracyOverview(
        latest_validation_at=db.scalar(select(func.max(PredictionValidation.computed_at))),
        latest_metrics_at=db.scalar(select(func.max(ForecastAccuracyMetric.computed_at))),
        pair_counts=pair_counts,
        stations=stations,
        models=models,
        experiments=experiments,
        lead_buckets_h=buckets,
        notes=NOTES,
    )


def metrics(
    db: Session,
    *,
    variable: str,
    scope: str | None = None,
    source_code: str | None = None,
    window_days: int | None = None,
) -> list[MetricRow]:
    """The newest computed metrics per key (one period per scope/source/model/bucket)."""
    q = select(ForecastAccuracyMetric).where(ForecastAccuracyMetric.variable == variable)
    if scope:
        q = q.where(ForecastAccuracyMetric.scope == scope)
    if source_code:
        q = q.where(ForecastAccuracyMetric.source_code == source_code)
    rows = db.scalars(
        q.order_by(ForecastAccuracyMetric.period_end.desc(), ForecastAccuracyMetric.id.desc())
    ).all()
    newest: dict[tuple[Any, ...], ForecastAccuracyMetric] = {}
    for r in rows:
        days = round((r.period_end - r.period_start).total_seconds() / 86400)
        if window_days is not None and days != window_days:
            continue
        key = (r.scope, r.scope_key, r.source_code, r.model_version, r.lead_bucket_h, days)
        newest.setdefault(key, r)  # rows come newest first
    return [
        MetricRow.model_validate(r, from_attributes=True)
        for r in sorted(
            newest.values(),
            key=lambda r: (r.scope, r.scope_key, r.source_code, r.model_version, r.lead_bucket_h),
        )
    ]


def _pairs_query(
    variable: str,
    source_code: str | None,
    model_version: str,
    station: str | None,
    lead_min: float | None,
    lead_max: float | None,
    since: datetime,
) -> Select[Any]:
    q = select(PredictionValidation).where(
        PredictionValidation.variable == variable,
        PredictionValidation.model_version == model_version,
        PredictionValidation.valid_time >= since,
    )
    if source_code:
        q = q.where(PredictionValidation.source_code == source_code)
    if station:
        q = q.join(BuoyStation, BuoyStation.id == PredictionValidation.station_id).where(
            BuoyStation.code == station
        )
    if lead_min is not None:
        q = q.where(PredictionValidation.lead_hours >= lead_min)
    if lead_max is not None:
        q = q.where(PredictionValidation.lead_hours <= lead_max)
    return q


def pairs(
    db: Session,
    *,
    variable: str,
    source_code: str | None = None,
    model_version: str = "raw",
    station: str | None = None,
    lead_min: float | None = None,
    lead_max: float | None = None,
    days: int = 90,
    limit: int = 1500,
) -> PairsOut:
    """Scatter sample, error histogram and daily error statistics for a set of pairs."""
    since = utcnow() - timedelta(days=days)
    base = _pairs_query(variable, source_code, model_version, station, lead_min, lead_max, since)
    filtered = base.subquery()
    n_total = db.scalar(select(func.count()).select_from(filtered)) or 0
    codes = dict(db.execute(select(BuoyStation.id, BuoyStation.code)).all())

    # Reproducible pseudo-random sample: order by a hash of the row id.
    sample_rows = db.execute(
        select(
            filtered.c.station_id,
            filtered.c.valid_time,
            filtered.c.lead_hours,
            filtered.c.forecast_value,
            filtered.c.observed_value,
            filtered.c.error,
        )
        .order_by(func.md5(cast(filtered.c.id, Text)))
        .limit(limit)
    ).all()
    sample = [
        ScatterPoint(
            station=codes.get(sid, str(sid)),
            valid_time=vt,
            lead_hours=lead,
            forecast=fc,
            observed=ob,
            error=err,
        )
        for sid, vt, lead, fc, ob, err in sample_rows
    ]

    errors = np.array(db.scalars(select(filtered.c.error)).all(), dtype=float)
    histogram: list[HistogramBin] = []
    if errors.size:
        lo, hi = np.percentile(errors, [0.5, 99.5])
        if hi <= lo:
            lo, hi = lo - 0.5, hi + 0.5
        counts, edges = np.histogram(np.clip(errors, lo, hi), bins=HISTOGRAM_BINS, range=(lo, hi))
        histogram = [
            HistogramBin(lo=float(edges[i]), hi=float(edges[i + 1]), count=int(c))
            for i, c in enumerate(counts)
        ]

    day = func.date_trunc("day", filtered.c.valid_time)
    daily = [
        DailyStat(day=d, n=n, bias=float(b), mae=float(m), rmse=float(r))
        for d, n, b, m, r in db.execute(
            select(
                day,
                func.count(),
                func.avg(filtered.c.error),
                func.avg(func.abs(filtered.c.error)),
                func.sqrt(func.avg(filtered.c.error * filtered.c.error)),
            )
            .group_by(day)
            .order_by(day)
        ).all()
    ]
    return PairsOut(
        variable=variable,
        unit=UNITS.get(variable, ""),
        source_code=source_code,
        model_version=model_version,
        n_total=n_total,
        sample=sample,
        histogram=histogram,
        daily=daily,
    )
