"""Job A — centralised forecast acquisition shared by every user.

One acquisition per (source, upstream model run): the run is identified by ``run_key``
and stored once; re-running the job for an already ingested run is a no-op. Failed or
interrupted runs (worker crash) are retried on the next schedule.

The map's global ocean-layer fields (``map_fields``, docs/map-layers.md) are stored with
the run they were read with (``FetchResult.fields``), or by :func:`acquire_map_fields`
under their own source in the standalone mode.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.models.forecast_engine import CORE_MAP_FIELD_VARIABLES, MapField
from app.services.forecasts.base import FORECAST_FIELDS, ForecastProvider, RunInfo, SpotPoint
from app.services.forecasts.demo import DemoForecastProvider, SwellOverride
from app.services.forecasts.fields import EncodedField, summarise
from app.services.forecasts.registry import build_provider
from app.services.http import ProviderError
from app.services.maintenance import prune_map_fields
from app.services.surf_quality.models import SpotParams

if TYPE_CHECKING:
    from app.services.forecasts.noaa import NoaaGfsFieldsProvider

logger = logging.getLogger(__name__)

STUCK_RUN_AFTER = timedelta(hours=1)
INSERT_CHUNK = 2000
MAP_FIELD_CHUNK = 50
# forecast_sources.priority of the fields-only source: after every spot source.
FIELDS_PRIORITY = 1000


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
    source.family = info.family
    source.kind = info.kind
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
                    "wave_period_kind": getattr(rec, "period_kind", None),
                    "missing_fields": [k for k in FORECAST_FIELDS if values[k] is None],
                    "extra": getattr(rec, "extra", None) or {},
                }
            )
    stmt = insert(WaveForecast).on_conflict_do_nothing(
        index_elements=["run_id", "spot_id", "valid_time"]
    )
    for i in range(0, len(rows), INSERT_CHUNK):
        db.execute(stmt, rows[i : i + INSERT_CHUNK])  # executemany: compiled once, batched
    return len(rows)


def store_map_fields(db: Session, run: ForecastRun, fields: list[EncodedField]) -> int:
    """Store a run's map fields; a field already stored for the same run, valid time and
    variable (``uq_map_fields_key``) is replaced, never duplicated. Returns the count."""
    if not fields:
        return 0
    cycle = run.wave_model_run_at or run.issued_at
    rows = [
        {
            "run_id": run.id,
            "source_code": run.source.code,
            "run_key": run.run_key,
            "cycle_time": cycle,
            "valid_time": f.valid_time,
            "variable": f.variable,
            "nx": f.nx,
            "ny": f.ny,
            "lat0": f.lat0,
            "lon0": f.lon0,
            "dlat": f.dlat,
            "dlon": f.dlon,
            "scale": f.scale,
            "offset": f.offset,
            "data": f.data,
            "created_at": utcnow(),
        }
        for f in fields
    ]
    base = insert(MapField)
    keys = ("run_id", "valid_time", "variable")
    stmt = base.on_conflict_do_update(
        constraint="uq_map_fields_key",
        set_={c: base.excluded[c] for c in rows[0] if c not in keys},
    )
    # About 0.1 MB per field: small batches keep each statement a few MB.
    for i in range(0, len(rows), MAP_FIELD_CHUNK):
        db.execute(stmt, rows[i : i + MAP_FIELD_CHUNK])
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
    if result.fields or result.field_warnings:
        fields, field_warnings = result.fields, list(result.field_warnings)
        try:
            with db.begin_nested():  # a SAVEPOINT: the map fields never cost the spot records
                store_map_fields(db, run, fields)
                if fields:
                    prune_map_fields(db)  # bound storage now, not only at the daily cleanup
        except SQLAlchemyError as exc:
            # The first line of the driver's message only: the statement's parameters and
            # PostgreSQL's DETAIL line repeat the row, field bytes included.
            message = str(getattr(exc, "orig", None) or exc).strip().splitlines() or [""]
            reason = f"{type(exc).__name__}: {message[0][:300]}"
            logger.error("Map fields of %s not stored: %s", info.run_key, reason)
            field_warnings.append(f"map fields not stored ({reason})")
            fields = []
        run.details = {**run.details, "map_fields": summarise(fields, field_warnings)}
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


def acquire_map_fields(
    db: Session,
    *,
    now: datetime | None = None,
    cycle: datetime | None = None,
    max_lead_hours: int | None = None,
    force: bool = False,
    provider: NoaaGfsFieldsProvider | None = None,
) -> dict[str, Any]:
    """Standalone map fields: the global fields of the newest complete GFS cycle.

    Stored under a run of the separate source ``noaa_gfs_fields`` (see
    ``noaa.NoaaGfsFieldsProvider``). The caller decides whether to run: the Celery task
    only in "standalone" mode, the CLI in every mode except "off". ``cycle`` picks a cycle
    instead of the newest complete one; ``max_lead_hours`` overrides
    MAP_FIELD_MAX_LEAD_HOURS.

    Without ``force``, nothing is downloaded when the cycle's fields already exist: a
    fields run of the cycle that is SUCCESS or PARTIAL and read every requested map hour
    (``details.map_hours``; a PARTIAL one is not retried, as for spot runs), or map_fields
    rows of the cycle from any run (e.g. the NOAA spot run in "with_noaa" mode) covering
    every map hour and variable. Afterwards only the two best servable runs keep their
    fields (``maintenance.prune_map_fields``).

    The run has no spot records, ``details.spot_ids = []`` and ``predictions_computed_at``
    set on completion (there is nothing to score), so the prediction job skips it.
    """
    started = time.monotonic()
    now = now or utcnow()
    if provider is None:
        from app.services.forecasts.noaa import NoaaGfsFieldsProvider  # ecCodes: lazy

        provider = NoaaGfsFieldsProvider(get_settings(), max_lead_hours=max_lead_hours)
    source = ensure_source(db, provider, FIELDS_PRIORITY)
    db.commit()
    out: dict[str, Any] = {"source": source.code, "run_key": None, "run_id": None}
    try:
        info = provider.run_for_cycle(cycle) if cycle is not None else provider.current_run(now)
    except ProviderError as exc:
        logger.error("Could not determine the map-field cycle: %s", exc)
        return {**out, "status": "failed", "message": str(exc)}
    hours = provider.map_hours()
    cycle_time = info.wave_model_run_at or info.issued_at
    out.update(run_key=info.run_key, cycle=cycle_time.isoformat(), map_hours=len(hours))

    run = db.scalar(
        select(ForecastRun).where(
            ForecastRun.source_id == source.id, ForecastRun.run_key == info.run_key
        )
    )
    if not force and run is not None:
        # A run counts only for the hours it read: a short --max-lead-hours run of the
        # cycle must not stop the scheduled job from reading the whole cycle.
        attempted = set((run.details or {}).get("map_hours", []))
        done = run.status in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
        if done and attempted >= set(hours):
            return {**out, "status": "skipped", "run_id": run.id, "message": "already stored"}
        if run.status == ForecastRunStatus.RUNNING and now - run.started_at < STUCK_RUN_AFTER:
            return {**out, "status": "skipped", "run_id": run.id, "message": "in progress"}
    if not force:
        expected = {
            (cycle_time + timedelta(hours=h), name)
            for h in hours
            for name in CORE_MAP_FIELD_VARIABLES
        }
        stored = db.execute(
            select(MapField.valid_time, MapField.variable)
            .where(MapField.cycle_time == cycle_time)
            .distinct()
        ).all()
        if expected <= {(t, v) for t, v in stored}:
            return {**out, "status": "skipped", "message": "fields of this cycle already stored"}

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
    run.details = {
        "metadata_source": info.metadata_source,
        "fields_only": True,
        "spot_ids": [],
        "map_hours": hours,
    }
    db.commit()
    out["run_id"] = run.id

    try:
        result = provider.fetch([], info)
    except ProviderError as exc:
        run.status = ForecastRunStatus.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = utcnow()
        run.predictions_computed_at = run.completed_at
        db.commit()
        logger.error("Map-field fetch failed for %s: %s", info.run_key, exc)
        return {**out, "status": "failed", "message": str(exc)}

    count = store_map_fields(db, run, result.fields)
    summary = summarise(result.fields, result.field_warnings)
    complete = len(summary["valid_times"])
    run.record_count = 0
    run.spot_count = 0
    run.request_count = result.request_count
    run.details = {**run.details, "warnings": result.warnings[:50], "map_fields": summary}
    if complete == len(hours):
        run.status = ForecastRunStatus.SUCCESS
    elif complete:
        run.status = ForecastRunStatus.PARTIAL
    else:
        run.status = ForecastRunStatus.FAILED
        run.error_message = "no map hour has all six fields"
    run.completed_at = utcnow()
    run.predictions_computed_at = run.completed_at  # no spot records: nothing to score
    pruned = prune_map_fields(db)
    db.commit()
    steps = sorted(provider.last_field_steps, key=lambda s: s.fhour)
    logger.info(
        "Stored map fields %s: %d fields, %d of %d hours complete, %.1f MB",
        info.run_key,
        count,
        complete,
        len(hours),
        summary["bytes"] / 1e6,
    )
    return {
        **out,
        "status": run.status.value,
        "fields": count,
        "complete_hours": complete,
        "stored_bytes": summary["bytes"],
        "downloaded_bytes": provider.last_bytes_downloaded,
        "requests": result.request_count,
        "pruned_fields": pruned,
        "duration_s": round(time.monotonic() - started, 1),
        "steps": [
            {
                "fhour": s.fhour,
                "bytes": s.bytes_downloaded,
                "requests": s.request_count,
                "seconds": round(s.seconds, 2),
                "failed": s.failed,
            }
            for s in steps
        ],
        "warnings": summary["warnings"][:20],
    }


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
    settings = get_settings()
    provider = DemoForecastProvider(
        settings.forecast_days, overrides=overrides, natural_swells=settings.demo_natural_swells
    )
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
