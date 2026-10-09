"""Job A — centralised forecast acquisition shared by every user.

One acquisition per (source, upstream model run): the run is identified by ``run_key``
and stored once; re-running the job for an already ingested run is a no-op. Failed or
interrupted runs (worker crash) are retried on the next schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.services.forecasts.base import FORECAST_FIELDS, ForecastProvider, RunInfo, SpotPoint
from app.services.forecasts.demo import DemoForecastProvider, SwellOverride
from app.services.forecasts.registry import build_provider
from app.services.http import ProviderError
from app.services.surf_quality.models import SpotParams

logger = logging.getLogger(__name__)

STUCK_RUN_AFTER = timedelta(hours=1)
INSERT_CHUNK = 2000


@dataclass
class AcquisitionOutcome:
    source_code: str
    run_key: str | None
    status: str
    run_id: int | None = None
    records: int = 0
    spot_errors: int = 0
    message: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_code,
            "run_key": self.run_key,
            "status": self.status,
            "run_id": self.run_id,
            "records": self.records,
            "spot_errors": self.spot_errors,
            "message": self.message,
        }


def ensure_source(db: Session, provider: ForecastProvider, priority: int = 100) -> ForecastSource:
    info = provider.source_info()
    source = db.scalar(select(ForecastSource).where(ForecastSource.code == info.code))
    if source is None:
        source = ForecastSource(code=info.code)
        db.add(source)
    source.name = info.name
    source.provider = info.provider
    source.wave_model = info.wave_model
    source.atmosphere_model = info.atmosphere_model
    source.is_demo = info.is_demo
    source.attribution = info.attribution
    source.license_notes = info.license_notes
    source.priority = priority
    source.is_enabled = True
    db.flush()
    return source


def spot_points(db: Session) -> list[SpotPoint]:
    spots = db.scalars(select(SurfSpot).where(SurfSpot.is_active).order_by(SurfSpot.id)).all()
    return [
        SpotPoint(
            spot_id=s.id,
            slug=s.slug,
            latitude=s.latitude,
            longitude=s.longitude,
            forecast_latitude=s.forecast_latitude,
            forecast_longitude=s.forecast_longitude,
            params=SpotParams.from_orm(s),
        )
        for s in spots
    ]


def _wave_power(values: dict[str, float | None]) -> float | None:
    h = values.get("primary_swell_height_m") or values.get("sig_wave_height_m")
    t = values.get("primary_swell_period_s") or values.get("wave_period_s")
    if h is None or t is None:
        return None
    # Deep-water energy flux P = ρ g² H² T / (64 π) ≈ 0.49 H² T  kW/m
    return round(0.49 * h * h * t, 2)


def store_records(db: Session, run: ForecastRun, records: dict[int, list[Any]]) -> int:
    rows: list[dict[str, Any]] = []
    for spot_id, items in records.items():
        for rec in items:
            values = {k: rec.values.get(k) for k in FORECAST_FIELDS}
            rows.append(
                {
                    "run_id": run.id,
                    "spot_id": spot_id,
                    "valid_time": rec.valid_time,
                    "issued_at": run.issued_at,
                    **values,
                    "wave_power_kw_m": _wave_power(values),
                    "missing_fields": [k for k in FORECAST_FIELDS if values[k] is None],
                }
            )
    stmt = insert(WaveForecast).on_conflict_do_nothing(
        index_elements=["run_id", "spot_id", "valid_time"]
    )
    for i in range(0, len(rows), INSERT_CHUNK):
        db.execute(stmt, rows[i : i + INSERT_CHUNK])  # executemany: compiled once, batched
    return len(rows)


def active_demo_overrides(db: Session, now: datetime) -> list[SwellOverride]:
    """Simulated swells that are still in the future, so new demo runs keep them."""
    source = db.scalar(
        select(ForecastSource).where(ForecastSource.code == DemoForecastProvider.code)
    )
    if source is None:
        return []
    runs = db.scalars(
        select(ForecastRun).where(
            ForecastRun.source_id == source.id,
            ForecastRun.started_at >= now - timedelta(days=16),
        )
    ).all()
    seen: dict[tuple[str, str], SwellOverride] = {}
    for run in runs:
        for raw in run.details.get("overrides", []):
            o = SwellOverride.from_json(raw)
            if o.end > now:
                seen[(o.spot_slug, o.start.isoformat())] = o
    return list(seen.values())


def acquire_from_provider(
    db: Session,
    provider: ForecastProvider,
    *,
    now: datetime | None = None,
    force: bool = False,
    run_info: RunInfo | None = None,
    extra_details: dict[str, Any] | None = None,
    priority: int = 100,
) -> AcquisitionOutcome:
    now = now or utcnow()
    source = ensure_source(db, provider, priority)
    db.commit()
    try:
        info = run_info or provider.current_run(now)
    except ProviderError as exc:
        logger.error("Could not determine current run for %s: %s", source.code, exc)
        return AcquisitionOutcome(source.code, None, "failed", message=str(exc))

    run = db.scalar(
        select(ForecastRun).where(
            ForecastRun.source_id == source.id, ForecastRun.run_key == info.run_key
        )
    )
    if run is not None and not force:
        if run.status in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL):
            return AcquisitionOutcome(
                source.code, info.run_key, "skipped", run.id, message="run already ingested"
            )
        if run.status == ForecastRunStatus.RUNNING and now - run.started_at < STUCK_RUN_AFTER:
            return AcquisitionOutcome(
                source.code, info.run_key, "skipped", run.id, message="run in progress"
            )
    if run is None:
        run = ForecastRun(source_id=source.id, run_key=info.run_key, details={})
        db.add(run)
    run.issued_at = info.issued_at
    run.wave_model_run_at = info.wave_model_run_at
    run.atmosphere_model_run_at = info.atmosphere_model_run_at
    run.started_at = now
    run.completed_at = None
    run.status = ForecastRunStatus.RUNNING
    run.error_message = None
    run.predictions_computed_at = None
    run.details = {"metadata_source": info.metadata_source, **(extra_details or {})}
    db.commit()

    points = spot_points(db)
    try:
        result = provider.fetch(points, info)
    except ProviderError as exc:
        run.status = ForecastRunStatus.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = utcnow()
        db.commit()
        logger.error("Forecast fetch failed for %s: %s", source.code, exc)
        return AcquisitionOutcome(source.code, info.run_key, "failed", run.id, message=str(exc))

    count = store_records(db, run, result.records)
    slugs = {p.spot_id: p.slug for p in points}
    run.record_count = count
    run.spot_count = len(result.records)
    run.request_count = result.request_count
    run.details = {
        **run.details,
        "spot_ids": sorted(result.records),
        "spot_errors": {slugs.get(k, str(k)): v for k, v in result.errors.items()},
        "warnings": result.warnings[:50],
    }
    if not result.records:
        run.status = ForecastRunStatus.FAILED
        run.error_message = "no spot returned usable data"
    elif result.errors:
        run.status = ForecastRunStatus.PARTIAL
    else:
        run.status = ForecastRunStatus.SUCCESS
    run.completed_at = utcnow()
    db.commit()
    logger.info(
        "Ingested %s run %s: %d records for %d spots (%d errors)",
        source.code,
        info.run_key,
        count,
        run.spot_count,
        len(result.errors),
    )
    return AcquisitionOutcome(
        source.code,
        info.run_key,
        run.status.value,
        run.id,
        records=count,
        spot_errors=len(result.errors),
        errors=run.details["spot_errors"],
    )


def acquire_all(
    db: Session, *, now: datetime | None = None, force: bool = False
) -> list[AcquisitionOutcome]:
    settings = get_settings()
    now = now or utcnow()
    outcomes: list[AcquisitionOutcome] = []
    for priority, name in enumerate(settings.forecast_provider_list):
        try:
            overrides = active_demo_overrides(db, now) if name == "demo" else None
            provider = build_provider(name, settings, overrides=overrides)
        except ProviderError as exc:
            outcomes.append(AcquisitionOutcome(name, None, "failed", message=str(exc)))
            continue
        extra = {"overrides": [o.to_json() for o in overrides]} if overrides else None
        outcomes.append(
            acquire_from_provider(
                db, provider, now=now, force=force, extra_details=extra, priority=priority
            )
        )
    return outcomes


def simulate_swell(
    db: Session,
    spot: SurfSpot,
    *,
    start: datetime,
    duration_hours: int = 60,
    period_s: float | None = None,
    target_breaking_ft: float | None = None,
    now: datetime | None = None,
) -> AcquisitionOutcome:
    """Developer tool: ingest a new DEMO run containing a strong swell at ``spot``."""
    now = now or utcnow()
    override = SwellOverride(
        spot_slug=spot.slug,
        start=start.astimezone(UTC),
        end=(start + timedelta(hours=duration_hours)).astimezone(UTC),
        period_s=period_s,
        target_breaking_ft=target_breaking_ft,
    )
    overrides = [*active_demo_overrides(db, now), override]
    provider = DemoForecastProvider(get_settings().forecast_days, overrides=overrides)
    issued = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    info = RunInfo(
        run_key=f"demo-sim:{now:%Y%m%d%H%M%S%f}",
        issued_at=issued,
        wave_model_run_at=issued,
        atmosphere_model_run_at=issued,
        metadata_source="demo-simulation",
    )
    return acquire_from_provider(
        db,
        provider,
        now=now,
        run_info=info,
        extra_details={"simulated": True, "overrides": [o.to_json() for o in overrides]},
    )
