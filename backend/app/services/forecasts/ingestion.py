"""Job A — centralised forecast acquisition shared by every user.

One acquisition per (source, upstream model run): the run is identified by ``run_key``
and stored once; re-running the job for an already ingested run is a no-op. Failed or
interrupted runs (worker crash) are retried on the next schedule, and transient failures
(timeouts, 5xx, a full minute or hour call budget, a run still being published) are marked
``retryable`` so the Celery task retries within minutes (app.workers.tasks).

Providers run one after another. Each run records its own start time, what the fetch cost
(``details["stats"]``: bytes, HTTP responses, statuses, weighted calls) and every model
run behind its data (``details["model_runs"]``). Any exception of one provider ends that
provider's run FAILED with a message, and the next provider still runs.

The map's global ocean-layer fields (``map_fields``, docs/map-layers.md) are stored with
the run they were read with (``FetchResult.fields``), or by :func:`acquire_map_fields`
under their own source in the standalone mode.

Component refreshes (design D6, D9; :func:`refresh_source`, the synced pipeline's spot
stage): only the due components of a spot source are fetched (a new wind run fetches the
atmosphere only); the other component is carried forward, in the database, from the run each
spot is served from, with its ORIGINAL model run (``forecast_runs.wave_model_run_at`` /
``atmosphere_model_run_at``, ``details.model_runs`` per domain with its component,
``details.components``, ``details.component_keys``). Every spot is then validated
(``carry.validate_spots``); a spot whose data failed is left out of the run and keeps its
previous run. A retry of only the failed spots stores a run of its own (run key
``<key>#retry<n>``).
"""

from __future__ import annotations

import inspect
import logging
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.models import ForecastRun, ForecastSource, SurfSpot, WaveForecast
from app.models.enums import ForecastRunStatus
from app.models.forecast_engine import CORE_MAP_FIELD_VARIABLES, MapField
from app.services.forecasts import carry
from app.services.forecasts.base import (
    ATMOSPHERE,
    COMPONENTS,
    FORECAST_FIELDS,
    WAVE,
    FetchResult,
    FetchStats,
    ForecastProvider,
    ForecastRecord,
    ModelRunRef,
    RunInfo,
    SpotPoint,
    component_of,
)
from app.services.forecasts.demo import DemoForecastProvider, SwellOverride
from app.services.forecasts.fields import EncodedField, summarise
from app.services.forecasts.registry import build_provider
from app.services.http import HttpTally, ProviderError, RunNotServedError
from app.services.maintenance import prune_map_fields
from app.services.surf_quality.models import SpotParams

if TYPE_CHECKING:
    from app.services.forecasts.noaa import NoaaGfsFieldsProvider
    from app.services.forecasts.versions import ComponentRun

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
    # A transient failure (or a run not served yet): the job should retry, after
    # ``retry_after_s`` when the provider said how long to wait.
    retryable: bool = False
    retry_after_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_code,
            "run_key": self.run_key,
            "status": self.status,
            "run_id": self.run_id,
            "records": self.records,
            "spot_errors": self.spot_errors,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_s": self.retry_after_s,
        }


def _error_text(exc: BaseException) -> str:
    """A stored, logged error message: redacted and bounded."""
    text = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
    return redact(text)[:2000]


def _failure(
    source_code: str, run_key: str | None, exc: BaseException, run_id: int | None = None
) -> AcquisitionOutcome:
    transient = isinstance(exc, ProviderError) and exc.transient
    # A used-up day or month limit is not retried by the job (not transient), but says
    # when the next attempt can succeed (quota.QuotaWindowExhaustedError): the sync waits.
    return AcquisitionOutcome(
        source_code,
        run_key,
        "failed",
        run_id,
        message=_error_text(exc),
        retryable=transient,
        retry_after_s=getattr(exc, "retry_after_s", None),
    )


def model_runs_detail(info: RunInfo, provider: ForecastProvider) -> list[dict[str, Any]]:
    """``run.details["model_runs"]``: every model run behind the data.

    Providers that do not list them (the direct GRIB ingesters, demo) have one wave and
    one atmosphere run, named after the source's models.
    """
    if info.model_runs:
        return [
            {
                "model": r.model,
                "init_time": r.init_time.isoformat(),
                "available_at": r.available_at.isoformat() if r.available_at else None,
                **({"component": r.component} if r.component else {}),
            }
            for r in info.model_runs
        ]
    source = provider.source_info()
    out = []
    for model, when in (
        (source.wave_model, info.wave_model_run_at),
        (source.atmosphere_model, info.atmosphere_model_run_at),
    ):
        if model and when is not None:
            out.append({"model": model, "init_time": when.isoformat(), "available_at": None})
    return out


def fetch_stats_detail(result: FetchResult, provider: object) -> dict[str, Any] | None:
    """``run.details["stats"]``: the provider's FetchStats, or what it exposes otherwise
    (bytes from ``last_bytes_downloaded``; None when the provider does not say)."""
    if result.stats is not None:
        return asdict(result.stats)
    downloaded = getattr(provider, "last_bytes_downloaded", None)
    return {
        "bytes_downloaded": int(downloaded) if isinstance(downloaded, int) else None,
        "requests": result.request_count,
        "http_status_counts": {},
        "weighted_calls": None,
    }


def _counters(provider: object) -> tuple[int, int, dict[str, int], float | None] | None:
    """(responses, bytes, statuses, weighted calls) a provider has counted so far, or None
    when it keeps no HttpTally."""
    tally = getattr(provider, "tally", None)
    if not isinstance(tally, HttpTally):
        return None
    responses, wire_bytes, statuses = tally.snapshot()
    weighted = getattr(provider, "weighted_calls_total", None)
    return responses, wire_bytes, statuses, weighted if isinstance(weighted, float) else None


def failed_fetch_stats(
    provider: object, before: tuple[int, int, dict[str, int], float | None] | None
) -> dict[str, Any] | None:
    """What a fetch that raised had downloaded and been charged (``details["stats"]`` of a
    FAILED run), from the provider's counters before and after it."""
    after = _counters(provider)
    if before is None or after is None:
        return None
    weighted = None
    if after[3] is not None and before[3] is not None:
        weighted = round(after[3] - before[3], 3)
    return asdict(FetchStats.between(before[:3], after[:3], weighted))


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


def store_records(db: Session, run: ForecastRun, records: dict[int, list[ForecastRecord]]) -> int:
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
                    "wave_period_kind": rec.period_kind,
                    "missing_fields": [k for k in FORECAST_FIELDS if values[k] is None],
                    "extra": rec.extra or {},
                }
            )
    # render_nulls: every row has the same columns (a None is sent as NULL instead of
    # leaving the column out), so each chunk is one batched statement.
    stmt = (
        insert(WaveForecast)
        .on_conflict_do_nothing(index_elements=["run_id", "spot_id", "valid_time"])
        .execution_options(render_nulls=True)
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


def _store_result_fields(
    db: Session, run: ForecastRun, result: FetchResult, run_key: str
) -> dict[str, Any] | None:
    """Store the map fields a fetch read with its spot run (the "with_noaa" map mode,
    docs/map-layers.md) in a SAVEPOINT, so the map fields never cost the spot records.
    Returns the ``details["map_fields"]`` summary, or None when the fetch read none."""
    if not (result.fields or result.field_warnings):
        return None
    fields, field_warnings = result.fields, list(result.field_warnings)
    try:
        with db.begin_nested():
            store_map_fields(db, run, fields)
            if fields:
                prune_map_fields(db)  # bound storage now, not only at the daily cleanup
    except SQLAlchemyError as exc:
        # The first line of the driver's message only: the statement's parameters and
        # PostgreSQL's DETAIL line repeat the row, field bytes included.
        message = str(getattr(exc, "orig", None) or exc).strip().splitlines() or [""]
        reason = f"{type(exc).__name__}: {message[0][:300]}"
        logger.error("Map fields of %s not stored: %s", run_key, reason)
        field_warnings.append(f"map fields not stored ({reason})")
        fields = []
    return summarise(fields, field_warnings)


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
    """Fetch and store one provider's current run (``now`` selects the model cycle).

    Never raises for a provider problem: every exception ends the attempt with a
    "failed" outcome (and a FAILED run once one was recorded), marked ``retryable`` when
    the failure was transient. A run the provider does not serve unmixed yet (Open-Meteo
    while a new run is published) is "skipped" without recording a run.
    """
    now = now or utcnow()
    source = ensure_source(db, provider, priority)
    db.commit()
    try:
        info = run_info or provider.current_run(now)
    except Exception as exc:  # provider errors and bugs alike end only this provider
        logger.error("Could not determine current run for %s: %s", source.code, _error_text(exc))
        return _failure(source.code, None, exc)

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
        # The real clock: the shared ``now`` of acquire_all can be minutes old by now.
        if run.status == ForecastRunStatus.RUNNING and utcnow() - run.started_at < STUCK_RUN_AFTER:
            return AcquisitionOutcome(
                source.code, info.run_key, "skipped", run.id, message="run in progress"
            )
    servable = getattr(provider, "ensure_servable", None)
    if run_info is None and callable(servable):
        try:
            servable(info)
        except RunNotServedError as exc:
            logger.info("%s run %s not served yet: %s", source.code, info.run_key, exc)
            return AcquisitionOutcome(
                source.code,
                info.run_key,
                "skipped",
                run.id if run else None,
                message=_error_text(exc),
                retryable=exc.retry_after_s is not None,
                retry_after_s=exc.retry_after_s,
            )
        except Exception as exc:
            logger.error("%s run %s cannot be read: %s", source.code, info.run_key, exc)
            return _failure(source.code, info.run_key, exc, run.id if run else None)
    if run is None:
        run = ForecastRun(source_id=source.id, run_key=info.run_key, details={})
        db.add(run)
    run.issued_at = info.issued_at
    run.wave_model_run_at = info.wave_model_run_at
    run.atmosphere_model_run_at = info.atmosphere_model_run_at
    run.started_at = utcnow()  # this provider's own start (providers run one by one)
    run.completed_at = None
    run.status = ForecastRunStatus.RUNNING
    run.error_message = None
    run.predictions_computed_at = None
    run.details = {
        "metadata_source": info.metadata_source,
        "model_runs": model_runs_detail(info, provider),
        **({"component_keys": dict(info.component_keys)} if info.component_keys else {}),
        **(extra_details or {}),
    }
    db.commit()
    run_id = run.id

    points = spot_points(db)
    counters = _counters(provider)
    try:
        result = provider.fetch(points, info)
        count = store_records(db, run, result.records)
    except Exception as exc:
        db.rollback()
        failed = db.get(ForecastRun, run_id)
        if failed is not None:
            failed.status = ForecastRunStatus.FAILED
            failed.error_message = _error_text(exc)
            failed.completed_at = utcnow()
            failed.details = {
                **(failed.details or {}),
                "retryable": isinstance(exc, ProviderError) and exc.transient,
                "stats": failed_fetch_stats(provider, counters),
            }
            db.commit()
        if isinstance(exc, ProviderError):
            logger.error("Forecast fetch failed for %s: %s", source.code, _error_text(exc))
        else:
            logger.exception("Forecast acquisition of %s failed unexpectedly", source.code)
        return _failure(source.code, info.run_key, exc, run_id)

    slugs = {p.spot_id: p.slug for p in points}
    run.record_count = count
    run.spot_count = len(result.records)
    run.request_count = result.request_count
    run.details = {
        **run.details,
        "spot_ids": sorted(result.records),
        "spot_errors": {slugs.get(k, str(k)): v for k, v in result.errors.items()},
        "warnings": result.warnings[:50],
        "stats": fetch_stats_detail(result, provider),
    }
    fields_summary = _store_result_fields(db, run, result, info.run_key)
    if fields_summary is not None:
        run.details = {**run.details, "map_fields": fields_summary}
    if not result.records:
        run.status = ForecastRunStatus.FAILED
        run.error_message = "no spot returned usable data"
    elif result.errors:
        run.status = ForecastRunStatus.PARTIAL
    else:
        run.status = ForecastRunStatus.SUCCESS
    run.completed_at = utcnow()
    db.commit()
    stats = run.details.get("stats") or {}
    logger.info(
        "Ingested %s run %s: %d records for %d spots (%d errors), %s bytes, %s weighted calls",
        source.code,
        info.run_key,
        count,
        run.spot_count,
        len(result.errors),
        stats.get("bytes_downloaded"),
        stats.get("weighted_calls"),
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


# --------------------------------------------------------------------------- components
RETRY_SUFFIX = "#retry"


@dataclass
class RefreshOutcome:
    """One spot source's component refresh (:func:`refresh_source`).

    ``status``: success, partial (some spots failed), failed (nothing usable: a provider or
    whole-component failure, nothing to publish), or skipped (``message``: "run already
    ingested" = the target is stored, "run in progress", or the provider does not serve the
    run unmixed yet). ``changed``: the components fetched as new; ``component_keys`` the run
    key of each component in the run; ``components`` the version's ``ComponentRun`` dicts;
    ``updated_spots`` / ``failed_spots`` the spots this refresh handled (a failed spot keeps
    its previous run, D9); ``retry`` names the attempt of a retry of failed spots only.
    """

    source_code: str
    provider: str
    run_key: str | None
    status: str
    run_id: int | None = None
    message: str = ""
    records: int = 0
    changed: frozenset[str] = frozenset()
    component_keys: dict[str, str] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_spots: list[int] = field(default_factory=list)
    failed_spots: dict[int, str] = field(default_factory=dict)
    retry: dict[str, Any] | None = None
    retryable: bool = False
    retry_after_s: float | None = None
    acquired_at: datetime | None = None
    estimated: bool = False
    fallback_from: str | None = None
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_code,
            "provider": self.provider,
            "run_key": self.run_key,
            "status": self.status,
            "run_id": self.run_id,
            "records": self.records,
            "spot_errors": len(self.failed_spots),
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_s": self.retry_after_s,
            "changed": sorted(self.changed),
            "component_keys": dict(self.component_keys),
            "spots_updated": len(self.updated_spots),
            "failed_spot_ids": sorted(self.failed_spots),
            "retry": self.retry,
            "reused": self.reused,
        }


def _accepts_components(fn: Callable[..., Any]) -> bool:
    try:
        return "components" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _component_refs(info: RunInfo, component: str) -> list[ModelRunRef]:
    """The model runs of one component (refs without a component feed both)."""
    return [
        r if r.component else ModelRunRef(r.model, r.init_time, r.available_at, component)
        for r in info.model_runs
        if r.component in (None, component)
    ]


def _component_init(info: RunInfo, component: str) -> datetime | None:
    inits = [r.init_time for r in _component_refs(info, component)]
    if inits:
        return min(inits)
    return info.wave_model_run_at if component == WAVE else info.atmosphere_model_run_at


def _keep_components(record: ForecastRecord, components: frozenset[str]) -> ForecastRecord:
    """A copy of ``record`` with the values of other components emptied (they are carried)."""
    values = {
        k: (v if component_of(k) in components else None)
        for k, v in record.values.items()
        if k in FORECAST_FIELDS
    }
    return ForecastRecord(
        valid_time=record.valid_time,
        values=values,
        period_kind=record.period_kind if WAVE in components else None,
        extra=dict(record.extra),
    )


def _retry_attempt(db: Session, source_id: int, base_key: str) -> int:
    count = db.scalar(
        select(func.count(ForecastRun.id)).where(
            ForecastRun.source_id == source_id,
            ForecastRun.run_key.like(f"{base_key}{RETRY_SUFFIX}%"),
        )
    )
    return int(count or 0) + 1


def refresh_source(
    db: Session,
    provider: ForecastProvider,
    *,
    token: str,
    due: Collection[str],
    stored: Mapping[str, ComponentRun],
    serving: Mapping[int, int],
    retry_spot_ids: Collection[int] | None = None,
    now: datetime | None = None,
    priority: int = 100,
    extra_details: dict[str, Any] | None = None,
) -> RefreshOutcome:
    """Refresh the due components of one spot source (design D6, D9); never raises for a
    provider problem.

    * ``due``: the components the plan found new (all of them for a provider without
      component keys); a component whose available key equals the stored one is current.
    * ``stored``: the published component runs of this source (``versions.ComponentRun`` by
      component: their run keys, model runs and retrieval times); a component not in it is
      fetched.
    * ``serving``: spot id -> the run it is served from (``versions.serving_runs``); a
      carried component is copied from that run when the run holds the carried key, else
      that spot fetches the component too (when the provider still serves the stored run),
      else the spot fails and keeps its previous run.
    * ``retry_spot_ids``: with nothing due, refresh only these spots (a retry of the spots
      that failed; run key ``<key>#retry<n>``).

    Spots are fetched in at most three groups by the components they need (normally one:
    the due components for every spot), each one batched request per model. Then the
    carried components are filled in the database, every spot is validated
    (FORECAST_MIN_VALID_FRACTION, physical ranges) and the spots that failed are left out.
    """
    settings = get_settings()
    now = now or utcnow()
    source = ensure_source(db, provider, priority)
    db.commit()
    try:
        available = provider.current_run(now)
    except Exception as exc:
        logger.error("Could not determine current run for %s: %s", source.code, _error_text(exc))
        no_run = _failure(source.code, None, exc)
        return RefreshOutcome(
            source.code,
            token,
            None,
            "failed",
            message=no_run.message,
            retryable=no_run.retryable,
            retry_after_s=no_run.retry_after_s,
        )
    if available.testing:
        # A testing key's data are modified on purpose: never stored or published (D3).
        return RefreshOutcome(
            source.code,
            token,
            available.run_key,
            "failed",
            message="windy_testing_key: the provider answered with testing data; not stored",
        )
    split = bool(available.component_keys)
    avail = (
        {c: str(available.component_keys[c]) for c in COMPONENTS}
        if split
        else dict.fromkeys(COMPONENTS, available.run_key)
    )
    stored_keys = {c: stored[c].run_key for c in COMPONENTS if c in stored}
    if split:
        # A run stored before component keys existed (Upgrade 4) names both components in
        # one key: compare each component with the part naming its own model domains.
        stored_keys = {
            c: carry.component_part(key, avail[c]) or key for c, key in stored_keys.items()
        }
    wanted = set(due) & COMPONENTS
    changed = {
        c
        for c in COMPONENTS
        if c in wanted
        and avail[c] != stored_keys.get(c)
        and not carry.older_than_stored(avail[c], stored_keys.get(c))
    }
    if not split and changed:
        changed = set(COMPONENTS)  # one run key: both parts or nothing
    # A part without a published run is fetched with the due ones.
    if changed:
        changed |= {c for c in COMPONENTS if not stored_keys.get(c)}
    retry = bool(retry_spot_ids) and not changed
    if not changed and not retry:
        return RefreshOutcome(
            source.code,
            token,
            available.run_key,
            "skipped",
            message="run already ingested",
            component_keys=avail,
        )
    target = {c: (avail[c] if c in changed else str(stored_keys[c])) for c in COMPONENTS}
    base_key = carry.combined_run_key(target) if split else available.run_key
    carried = COMPONENTS - changed

    # Which spots need which components: the due ones, plus a carried one whose served
    # run does not hold the carried key.
    points = spot_points(db)
    if retry:
        wanted_spots = {int(s) for s in retry_spot_ids or ()}
        points = [p for p in points if p.spot_id in wanted_spots]

    def served_keys(run_key: str, details: Mapping[str, Any] | None) -> dict[str, str]:
        keys = carry.run_component_keys(details)
        if keys or not split:
            return keys
        # Stored before component keys existed: the parts of its run key, by model domain.
        parts = {c: carry.component_part(run_key, target[c]) for c in COMPONENTS}
        return {c: k for c, k in parts.items() if k}

    runs = {
        rid: (sid, served_keys(str(key), details))
        for rid, sid, key, details in db.execute(
            select(
                ForecastRun.id, ForecastRun.source_id, ForecastRun.run_key, ForecastRun.details
            ).where(ForecastRun.id.in_(sorted(set(serving.values()))))
        ).all()
    }
    groups: dict[frozenset[str], list[SpotPoint]] = {}
    sources: dict[str, dict[int, int]] = {c: {} for c in carried}
    failed: dict[int, str] = {}
    for p in points:
        need = set(changed)
        served = serving.get(p.spot_id)
        source_id, keys = runs.get(served, (None, {})) if served is not None else (None, {})
        for c in carried:
            if served is not None and source_id == source.id and keys.get(c) == target[c]:
                sources[c][p.spot_id] = served
            else:
                need.add(c)
        if not need:
            continue  # a retry of a spot that is up to date already
        unserved = sorted(c for c in need if target[c] != avail[c])
        if unserved:
            failed[p.spot_id] = (
                f"no stored {unserved[0]} data of run {target[unserved[0]]} to carry, and the "
                "provider no longer serves that run"
            )
            continue
        groups.setdefault(frozenset(need), []).append(p)
    for c in carried:
        for spot_id in list(sources[c]):
            if not any(p.spot_id == spot_id for g, ps in groups.items() if c not in g for p in ps):
                sources[c].pop(spot_id)

    run_key = base_key
    attempt = None
    if retry:
        attempt = _retry_attempt(db, source.id, base_key)
        run_key = f"{base_key}{RETRY_SUFFIX}{attempt}"
    if not groups:
        message = "; ".join(sorted(set(failed.values())))[:2000] or "nothing to refresh"
        run_id = None
        if retry and failed:
            # The attempt is recorded (a FAILED retry run), so the next retry waits longer
            # and SYNC_MAX_ATTEMPTS bounds them, as for a retry whose requests failed.
            at = utcnow()
            attempt_run = ForecastRun(
                source_id=source.id,
                run_key=run_key,
                issued_at=available.issued_at,
                started_at=at,
                completed_at=at,
                status=ForecastRunStatus.FAILED,
                error_message=redact(message)[:2000],
                details={
                    "retry": {"attempt": attempt, "base_run_key": base_key},
                    "failed_spot_ids": sorted(failed),
                    "spot_ids": [],
                },
            )
            db.add(attempt_run)
            db.commit()
            run_id = attempt_run.id
        return RefreshOutcome(
            source.code,
            token,
            run_key,
            "failed" if failed else "skipped",
            run_id,
            message=message,
            component_keys=target,
            failed_spots=failed,
            retry={"attempt": attempt, "base_run_key": base_key} if retry else None,
        )

    # The run as stored: each component's own model runs and times (a carried component
    # keeps the published ones; never the new run's).
    refs: list[ModelRunRef] = []
    inits: dict[str, datetime | None] = {}
    for c in (WAVE, ATMOSPHERE):
        if c in carried and c in stored:
            comp = stored[c]
            own = [
                ModelRunRef(d.model, d.init_time, d.available_at, c)
                for d in comp.domains
                if d.init_time is not None
            ]
            if not own and comp.init_time is not None:
                # A run stored before components existed lists no domains per component.
                own = [ModelRunRef(comp.model or c, comp.init_time, comp.available_at, c)]
            refs += own
            inits[c] = comp.init_time or _component_init(available, c)
        else:
            refs += _component_refs(available, c) if split else []
            inits[c] = _component_init(available, c)
    if not split:
        refs = list(available.model_runs)
    known_inits = [t for t in inits.values() if t is not None]
    info = RunInfo(
        run_key=run_key,
        issued_at=min(known_inits) if known_inits else available.issued_at,
        wave_model_run_at=inits.get(WAVE) if split else available.wave_model_run_at,
        atmosphere_model_run_at=inits.get(ATMOSPHERE)
        if split
        else available.atmosphere_model_run_at,
        metadata_source=available.metadata_source,
        model_runs=tuple(refs),
        component_keys=MappingProxyType(dict(target)) if split else MappingProxyType({}),
        estimated=available.estimated,
    )
    fetched = frozenset().union(*groups)
    servable = getattr(provider, "ensure_servable", None)
    if callable(servable):
        try:
            if split and _accepts_components(servable):
                servable(info, components=fetched)
            else:
                servable(available)
        except RunNotServedError as exc:
            logger.info("%s run %s not served yet: %s", source.code, run_key, exc)
            return RefreshOutcome(
                source.code,
                token,
                run_key,
                "skipped",
                message=_error_text(exc),
                component_keys=target,
                retryable=exc.retry_after_s is not None,
                retry_after_s=exc.retry_after_s,
            )
        except Exception as exc:
            failure = _failure(source.code, run_key, exc)
            return RefreshOutcome(
                source.code,
                token,
                run_key,
                "failed",
                message=failure.message,
                component_keys=target,
                retryable=failure.retryable,
                retry_after_s=failure.retry_after_s,
            )

    run = db.scalar(
        select(ForecastRun).where(
            ForecastRun.source_id == source.id, ForecastRun.run_key == run_key
        )
    )
    if run is not None:
        if run.status in (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL):
            return _reused(run, source.code, token, target, changed)
        if run.status == ForecastRunStatus.RUNNING and utcnow() - run.started_at < STUCK_RUN_AFTER:
            return RefreshOutcome(
                source.code, token, run_key, "skipped", run.id, message="run in progress"
            )
        db.execute(delete(WaveForecast).where(WaveForecast.run_id == run.id))
    else:
        run = ForecastRun(source_id=source.id, run_key=run_key, details={})
        db.add(run)
    started = utcnow()
    retrieved: dict[str, str | None] = {}
    for c in COMPONENTS:
        old_time = stored[c].retrieved_at if c in carried and c in stored else None
        retrieved[c] = old_time.isoformat() if old_time is not None else None
    main_source = {
        c: Counter(sources[c].values()).most_common(1)[0][0] if sources.get(c) else None
        for c in COMPONENTS
    }
    run.issued_at = info.issued_at
    run.wave_model_run_at = info.wave_model_run_at
    run.atmosphere_model_run_at = info.atmosphere_model_run_at
    run.started_at = started
    run.completed_at = None
    run.status = ForecastRunStatus.RUNNING
    run.error_message = None
    run.predictions_computed_at = None
    run.details = {
        "metadata_source": info.metadata_source,
        "model_runs": model_runs_detail(info, provider),
        **({"component_keys": dict(target)} if split else {}),
        "components": {
            c: {
                "changed": c in changed,
                "run_key": target[c],
                "retrieved_at": retrieved[c],
                "carried_from_run_id": main_source[c],
            }
            for c in sorted(COMPONENTS)
        },
        **({"retry": {"attempt": attempt, "base_run_key": base_key}} if retry else {}),
        **(extra_details or {}),
    }
    db.commit()
    run_id = run.id

    counters = _counters(provider)
    count = 0
    errors: dict[int, str] = {}
    warnings: list[str] = []
    stats: list[dict[str, Any] | None] = []
    group_failures: list[BaseException] = []
    fields_summary: dict[str, Any] | None = None
    slugs = {p.spot_id: p.slug for p in spot_points(db)}
    for parts, group in sorted(groups.items(), key=lambda g: (-len(g[1]), sorted(g[0]))):
        components = None if parts == COMPONENTS else parts
        try:
            if not split:
                result = provider.fetch(group, available)
            elif components is not None and _accepts_components(provider.fetch):
                result = provider.fetch(group, info, components)
            else:
                result = provider.fetch(group, info)
            records = {
                spot_id: [_keep_components(r, parts) for r in recs]
                for spot_id, recs in result.records.items()
            }
            count += store_records(db, run, records)
            fields_summary = _store_result_fields(db, run, result, run_key) or fields_summary
            db.commit()
        except Exception as exc:
            db.rollback()
            group_failures.append(exc)
            for p in group:
                errors[p.spot_id] = _error_text(exc)
            if isinstance(exc, ProviderError):
                logger.error("Forecast fetch failed for %s: %s", source.code, _error_text(exc))
            else:
                logger.exception("Forecast refresh of %s failed unexpectedly", source.code)
            continue
        errors.update({k: v for k, v in result.errors.items() if k not in result.records})
        for k, v in result.errors.items():
            if k in result.records:
                errors.setdefault(k, v)
        warnings += result.warnings
        stats.append(fetch_stats_detail(result, provider))
    acquired = utcnow()
    refreshed = db.get(ForecastRun, run_id)
    assert refreshed is not None
    run = refreshed
    for c in carried:
        carry.carry_component(db, run_id=run_id, component=c, sources=sources[c])
    scope = [p.spot_id for g in groups.values() for p in g]
    verdicts = carry.validate_spots(db, run_id, settings.forecast_min_valid_fraction, scope)
    for spot_id, reason in verdicts.items():
        if reason is not None:
            failed[spot_id] = errors.get(spot_id) or reason
    carry.drop_spots(db, run_id, [s for s in failed if s in verdicts])
    updated = sorted(s for s, reason in verdicts.items() if reason is None)
    total = _sum_stats(stats) if stats else failed_fetch_stats(provider, counters)
    if total is None:
        total = asdict(FetchStats())
    retryable = any(isinstance(e, ProviderError) and e.transient for e in group_failures)
    retry_after = max(
        (float(getattr(e, "retry_after_s", 0.0) or 0.0) for e in group_failures), default=0.0
    )
    run.record_count = int(
        db.scalar(select(func.count(WaveForecast.id)).where(WaveForecast.run_id == run_id)) or 0
    )
    run.spot_count = len(updated)
    run.request_count = int(total.get("requests") or 0)
    run.details = {
        **run.details,
        "spot_ids": updated,
        "spot_errors": {slugs.get(k, str(k)): v for k, v in sorted(failed.items())},
        "failed_spot_ids": sorted(failed),
        # By spot id, so a sync that reuses this run records each spot's own error.
        "failed_spot_errors": {str(k): v for k, v in sorted(failed.items())},
        "warnings": warnings[:50],
        "stats": total,
        "fetch_groups": {"+".join(sorted(g)): len(ps) for g, ps in groups.items()},
        "retryable": retryable,
        **({"map_fields": fields_summary} if fields_summary is not None else {}),
    }
    for c in sorted(COMPONENTS):
        if c in changed:
            run.details["components"][c]["retrieved_at"] = acquired.isoformat()
    if not updated:
        run.status = ForecastRunStatus.FAILED
        reasons = "; ".join(sorted(set(failed.values())))[:1500]
        run.error_message = redact(f"no spot returned usable data ({reasons})")[:2000]
    elif failed:
        run.status = ForecastRunStatus.PARTIAL
    else:
        run.status = ForecastRunStatus.SUCCESS
    run.completed_at = utcnow()
    run.details = dict(run.details)
    db.commit()
    logger.info(
        "Refreshed %s run %s (%s): %d spots updated, %d failed, %s bytes",
        source.code,
        run_key,
        "+".join(sorted(changed)) or "retry",
        len(updated),
        len(failed),
        total.get("bytes_downloaded"),
    )
    components_out = _version_components(
        info, available, stored, target, changed, main_source, acquired, split
    )
    return RefreshOutcome(
        source.code,
        token,
        run_key,
        run.status.value,
        run_id,
        message=run.error_message or "",
        records=run.record_count,
        changed=frozenset(changed),
        component_keys=target,
        components=components_out,
        updated_spots=updated,
        failed_spots=failed,
        retry={"attempt": attempt, "base_run_key": base_key} if retry else None,
        retryable=retryable and not updated,
        retry_after_s=retry_after or None,
        acquired_at=acquired,
        estimated=available.estimated,
        fallback_from=getattr(provider, "fallback_from", None),
    )


def _sum_stats(parts: list[dict[str, Any] | None]) -> dict[str, Any]:
    """The FetchStats of several fetches of one refresh, added up."""
    known = [x for x in parts if x]
    downloaded = [x["bytes_downloaded"] for x in known if x.get("bytes_downloaded") is not None]
    weighted = [x["weighted_calls"] for x in known if x.get("weighted_calls") is not None]
    statuses: Counter[str] = Counter()
    for x in known:
        statuses.update(x.get("http_status_counts") or {})
    requests: int = sum(int(x.get("requests") or 0) for x in known)
    return {
        "bytes_downloaded": sum(int(b) for b in downloaded) if downloaded else None,
        "requests": requests,
        "http_status_counts": dict(statuses),
        "weighted_calls": round(sum(float(w) for w in weighted), 3) if weighted else None,
    }


def _version_components(
    info: RunInfo,
    available: RunInfo,
    stored: Mapping[str, ComponentRun],
    target: Mapping[str, str],
    changed: Collection[str],
    main_source: Mapping[str, int | None],
    acquired: datetime,
    split: bool,
) -> dict[str, dict[str, Any]]:
    """The version's ``components`` for a refresh: a fetched component from the provider's
    run, a carried one with its ORIGINAL model run and retrieval time."""
    from app.services.forecasts.versions import ComponentRun

    out: dict[str, dict[str, Any]] = {}
    for c in sorted(COMPONENTS):
        if c in changed or c not in stored:
            refs = _component_refs(info, c) if split else list(available.model_runs)
            comp = ComponentRun.from_model_runs(
                c,
                refs,
                run_key=target[c],
                retrieved_at=acquired,
                changed=c in changed,
                estimated=available.estimated,
            )
            if comp.init_time is None:
                init = _component_init(available, c)
                comp = ComponentRun(
                    c,
                    model=comp.model,
                    run_key=comp.run_key,
                    init_time=init,
                    retrieved_at=acquired,
                    changed=c in changed,
                    estimated=available.estimated,
                )
        else:
            old = stored[c]
            comp = ComponentRun(
                c,
                model=old.model,
                run_key=target[c],
                init_time=old.init_time,
                available_at=old.available_at,
                retrieved_at=old.retrieved_at,
                changed=False,
                carried_from_run_id=main_source.get(c) or old.carried_from_run_id,
                estimated=old.estimated,
                update_interval_seconds=old.update_interval_seconds,
                domains=old.domains,
            )
        out[c] = comp.as_dict()
    return out


def stored_spot_errors(
    details: Mapping[str, Any] | None, default: str = "failed"
) -> dict[int, str]:
    """{spot id: its error} of the spots a stored refresh run failed (``failed_spot_errors``;
    a run without it gives its first ``spot_errors`` text, or ``default``)."""
    details = details or {}
    by_id = details.get("failed_spot_errors") or {}
    first = str(next(iter((details.get("spot_errors") or {}).values()), default))
    return {int(s): str(by_id.get(str(s)) or first) for s in details.get("failed_spot_ids") or []}


def _reused(
    run: ForecastRun,
    source_code: str,
    token: str,
    target: Mapping[str, str],
    changed: Collection[str],
) -> RefreshOutcome:
    """A run of the target key is stored already (a sync stopped before publishing it):
    nothing is fetched (status skipped, "run already ingested"); the sync publishes it as it
    is (``reused``, with its run id)."""
    details = run.details or {}
    return RefreshOutcome(
        source_code,
        token,
        run.run_key,
        "skipped",
        run.id,
        message="run already ingested",
        records=run.record_count,
        changed=frozenset(changed),
        component_keys=dict(target),
        updated_spots=[int(s) for s in details.get("spot_ids") or []],
        failed_spots=stored_spot_errors(details),
        retry=details.get("retry"),
        acquired_at=run.completed_at,
        reused=True,
    )


def acquire_all(
    db: Session,
    *,
    now: datetime | None = None,
    force: bool = False,
    only: Collection[str] | None = None,
) -> list[AcquisitionOutcome]:
    """Acquire from every configured provider, in FORECAST_PROVIDERS priority order.

    ``only``: FORECAST_PROVIDERS tokens to acquire (the others are not contacted; their
    priority stays their place in FORECAST_PROVIDERS); None = every provider.
    """
    settings = get_settings()
    now = now or utcnow()
    outcomes: list[AcquisitionOutcome] = []
    for priority, name in enumerate(settings.forecast_provider_list):
        if only is not None and name not in only:
            continue
        try:
            overrides = active_demo_overrides(db, now) if name == "demo" else None
            provider = build_provider(name, settings, overrides=overrides)
        except Exception as exc:  # a provider that cannot be built does not stop the others
            logger.error("Forecast provider %s unavailable: %s", name, _error_text(exc))
            outcomes.append(_failure(name, None, exc))
            continue
        extra = {"overrides": [o.to_json() for o in overrides]} if overrides else None
        try:
            outcome = acquire_from_provider(
                db, provider, now=now, force=force, extra_details=extra, priority=priority
            )
        except Exception as exc:  # e.g. the database refused the source row
            db.rollback()
            logger.exception("Forecast acquisition of %s failed", name)
            outcome = _failure(name, None, exc)
        outcomes.append(outcome)
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
        failed = _failure(source.code, None, exc)
        return {
            **out,
            "status": "failed",
            "message": failed.message,
            "retryable": failed.retryable,
            "retry_after_s": failed.retry_after_s,
        }
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
        if run.status == ForecastRunStatus.RUNNING and utcnow() - run.started_at < STUCK_RUN_AFTER:
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
    run.started_at = utcnow()
    run.completed_at = None
    run.status = ForecastRunStatus.RUNNING
    run.error_message = None
    run.predictions_computed_at = None
    run.details = {
        "metadata_source": info.metadata_source,
        "model_runs": model_runs_detail(info, provider),
        "fields_only": True,
        "spot_ids": [],
        "map_hours": hours,
    }
    db.commit()
    out["run_id"] = run.id

    try:
        result = provider.fetch([], info)
    except Exception as exc:
        db.rollback()
        failed = _failure(source.code, info.run_key, exc, out["run_id"])
        failed_run = db.get(ForecastRun, out["run_id"])
        if failed_run is not None:
            failed_run.status = ForecastRunStatus.FAILED
            failed_run.error_message = failed.message
            failed_run.completed_at = utcnow()
            failed_run.predictions_computed_at = failed_run.completed_at
            failed_run.details = {**(failed_run.details or {}), "retryable": failed.retryable}
            db.commit()
        if isinstance(exc, ProviderError):
            logger.error("Map-field fetch failed for %s: %s", info.run_key, failed.message)
        else:
            logger.exception("Map-field fetch of %s failed unexpectedly", info.run_key)
        return {
            **out,
            "status": "failed",
            "message": failed.message,
            "retryable": failed.retryable,
            "retry_after_s": failed.retry_after_s,
        }

    count = store_map_fields(db, run, result.fields)
    summary = summarise(result.fields, result.field_warnings)
    complete = len(summary["valid_times"])
    run.record_count = 0
    run.spot_count = 0
    run.request_count = result.request_count
    run.details = {
        **run.details,
        "warnings": result.warnings[:50],
        "map_fields": summary,
        "stats": fetch_stats_detail(result, provider),
    }
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
