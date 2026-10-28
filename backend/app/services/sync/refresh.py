"""Spot refreshes per component and their forecast versions (design D6, D7, D9; docs/sync.md).

The spot stage of a sync calls :func:`refresh_spots` with the plan's due spot components:
for each provider it refreshes just the due components (``ingestion.refresh_source``: the
other component is carried forward from the served run with its original model times), or
retries only the spots that failed in the published version. The publish stage then stages
one forecast version per stored refresh (:func:`stage_refresh_version`: updated spots point
at the new run, failed spots keep their previous run as ``stale``, the other spots are
carried over) and publishes it in one transaction (:func:`publish_staged`); a version that
cannot be published is marked failed and the previous one stays published.

With several providers (FORECAST_PROVIDERS, the blend path, design D5) and outside a sync
(the developer pipeline, ``sync publish``) the version is the run selection the public reads
made before versions existed (:func:`publish_selection_version`): one run per spot,
``queries.latest_runs_by_spot``.

After any publication :func:`write_marker` stores the version payload (C4) with the
published map run in Redis, so the version endpoint answers without the database.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
from app.services.forecasts.base import COMPONENTS, ModelRunRef
from app.services.forecasts.carry import run_component_keys
from app.services.forecasts.versions import (
    STALE,
    UPDATED,
    ComponentRun,
    MapRunRef,
    SpotRun,
    VersionPublishError,
    component_runs_of,
    current_version,
    fail_version,
    publish_version,
    serving_runs,
    stage_version,
    version_payload,
    write_version_marker,
)
from app.services.sync.types import ComponentStatus

logger = logging.getLogger(__name__)

BLEND_CODE = "blend"
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
RETRY_SUFFIX = "#retry"
# forecast_versions.timings["kind"]: a component refresh, a retry of failed spots, or the
# run selection (several providers, the developer pipeline).
KIND_REFRESH = "refresh"
KIND_RETRY = "retry"
KIND_SELECTION = "selection"


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def token_for_source(code: str | None, settings: Settings | None = None) -> str:
    """The FORECAST_PROVIDERS token of a source code (the version's ``provider``)."""
    from app.services.forecasts.registry import SOURCE_CODES

    settings = settings or get_settings()
    for token, source in SOURCE_CODES.items():
        if source == code:
            return token
    providers = settings.forecast_provider_list
    return providers[0] if providers else str(code or "unknown")


def components_from_run(run: ForecastRun) -> dict[str, ComponentRun]:
    """The component runs a stored run records (``details.model_runs`` per domain with its
    component, ``details.component_keys``, ``details.components``); a run stored before
    components existed gives each component its run time and the source's model name."""
    details = run.details or {}
    keys = run_component_keys(details)
    parts = details.get("components") if isinstance(details.get("components"), dict) else {}
    refs = [
        ModelRunRef(
            str(m["model"]),
            init,
            _time(m.get("available_at")),
            m.get("component") or None,
        )
        for m in details.get("model_runs") or []
        if isinstance(m, dict) and m.get("model") and (init := _time(m.get("init_time")))
    ]
    tagged = any(r.component for r in refs)
    out: dict[str, ComponentRun] = {}
    for c in sorted(COMPONENTS):
        part = parts.get(c) if isinstance(parts, dict) else None
        part = part if isinstance(part, dict) else {}
        retrieved = _time(part.get("retrieved_at")) or run.completed_at
        carried = part.get("carried_from_run_id")
        if tagged:
            out[c] = ComponentRun.from_model_runs(
                c,
                refs,
                run_key=keys.get(c) or run.run_key,
                retrieved_at=retrieved,
                changed=bool(part.get("changed", True)),
                carried_from_run_id=int(carried) if carried is not None else None,
            )
            continue
        source = run.source
        out[c] = ComponentRun(
            c,
            model=(source.wave_model if c == "wave" else source.atmosphere_model)
            if source is not None
            else None,
            run_key=keys.get(c) or run.run_key,
            init_time=run.wave_model_run_at if c == "wave" else run.atmosphere_model_run_at,
            retrieved_at=retrieved,
            changed=bool(part.get("changed", True)),
            carried_from_run_id=int(carried) if carried is not None else None,
        )
    return out


def version_is_simulated(db: Session, version: ForecastVersion) -> bool:
    """Whether the version's run is a simulated demo run (``simulate-swell``): its key is
    never a provider's, so it never counts as the stored run (the detection would plan a
    refresh at every check otherwise)."""
    if version.forecast_run_id is None:
        return False
    flag = db.scalar(
        select(ForecastRun.details["simulated"].astext).where(
            ForecastRun.id == version.forecast_run_id
        )
    )
    return flag == "true"


def published_components(
    db: Session, source_code: str
) -> tuple[ForecastVersion | None, dict[str, ComponentRun]]:
    """The published version of this source and its component runs; before any version
    of it, the newest usable published run's (the legacy rule of ``detect.load_known``)."""
    version = current_version(db)
    if (
        version is not None
        and version.source_code == source_code
        and version.components
        and not version_is_simulated(db, version)
    ):
        return version, component_runs_of(version)
    from sqlalchemy import func

    published_at = db.scalar(select(func.max(SyncRun.published_at)))
    if published_at is None:
        return None, {}
    run = db.scalar(
        select(ForecastRun)
        .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
        .where(
            ForecastSource.code == source_code,
            ForecastRun.status.in_(USABLE),
            ForecastRun.completed_at <= published_at,
            ForecastRun.details["simulated"].astext.is_distinct_from("true"),
        )
        .ext(distinct_on(ForecastRun.source_id))
        .order_by(ForecastRun.source_id, ForecastRun.issued_at.desc(), ForecastRun.id.desc())
    )
    return None, components_from_run(run) if run is not None else {}


# --------------------------------------------------------------------------- the spot stage
def refresh_spots(
    db: Session,
    comps: Iterable[ComponentStatus],
    *,
    forced: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Refresh the spot components a plan wants, provider by provider (in FORECAST_PROVIDERS
    order); returns each provider's ``RefreshOutcome.as_dict()`` plus ``components``
    (the version's component runs) and ``detected_at`` (the first sighting)."""
    from app.services.forecasts import ingestion
    from app.services.sync.detect import with_simulated_runs

    settings = get_settings()
    now = now or utcnow()
    by_token: dict[str, list[ComponentStatus]] = {}
    for c in comps:
        by_token.setdefault(c.provider, []).append(c)
    out: list[dict[str, Any]] = []
    for priority, name in enumerate(settings.forecast_provider_list):
        group = by_token.get(name)
        if not group:
            continue
        due: set[str] = set()
        retry_ids: set[int] = set()
        for c in group:
            if c.retry_spot_ids and c.state != "disabled" and (c.state == "new" or forced):
                retry_ids |= set(c.retry_spot_ids)
            if c.state == "new" and not c.retry_spot_ids or forced:
                due |= {c.component} if c.component else set(COMPONENTS)
        firsts = [c.first_seen_at for c in group if c.first_seen_at is not None]
        try:
            overrides = ingestion.active_demo_overrides(db, now) if name == "demo" else None
            provider = with_simulated_runs(
                ingestion.build_provider(name, settings, overrides=overrides)
            )
        except Exception as exc:  # a provider that cannot be built does not stop the others
            logger.error("Forecast provider %s unavailable: %s", name, type(exc).__name__)
            out.append(
                {
                    "source": name,
                    "provider": name,
                    "status": "failed",
                    "message": f"{type(exc).__name__}: provider not available",
                    "run_key": None,
                }
            )
            continue
        code = provider.source_info().code
        _, stored = published_components(db, code)
        extra = {"overrides": [o.to_json() for o in overrides]} if overrides else None
        try:
            outcome = ingestion.refresh_source(
                db,
                provider,
                token=name,
                due=due,
                stored=stored,
                serving=serving_runs(db, now=now),
                retry_spot_ids=retry_ids or None,
                now=now,
                priority=priority,
                extra_details=extra,
            )
        except Exception as exc:  # e.g. the database refused the source row
            db.rollback()
            logger.exception("Forecast refresh of %s failed", name)
            out.append(
                {
                    "source": code,
                    "provider": name,
                    "status": "failed",
                    "message": f"{type(exc).__name__}: refresh failed",
                    "run_key": None,
                }
            )
            continue
        finally:
            client = getattr(provider, "client", None)
            if client is not None and callable(getattr(client, "close", None)):
                client.close()
        entry = outcome.as_dict()
        entry["components"] = outcome.components
        entry["detected_at"] = min(firsts).isoformat() if firsts else None
        entry["acquired_at"] = outcome.acquired_at.isoformat() if outcome.acquired_at else None
        entry["failed_spots"] = {str(k): v for k, v in sorted(outcome.failed_spots.items())}
        entry["updated_spot_ids"] = list(outcome.updated_spots)
        entry["estimated"] = outcome.estimated
        entry["fallback_from"] = outcome.fallback_from
        out.append(entry)
    return out


# --------------------------------------------------------------------------- versions
def stage_refresh_version(
    db: Session,
    refresh: Mapping[str, Any],
    *,
    sync_run_id: int | None,
    scored_at: datetime | None = None,
    timings: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ForecastVersion | None:
    """Stage the version of one stored refresh (an outcome dict of :func:`refresh_spots`),
    or None when it stored nothing to publish. A retry keeps the published version's
    component runs (its keys did not change); a reused run (stored by a sync that stopped
    before publishing) is staged from what the run records."""
    stored = refresh.get("status") in ("success", "partial") or refresh.get("reused")
    if not stored or refresh.get("run_id") is None:
        return None
    run_id = int(refresh["run_id"])
    run = db.get(ForecastRun, run_id)
    if run is None or run.status not in USABLE:
        return None
    source_code = str(refresh.get("source") or run.source.code)
    published = current_version(db)
    retry = bool(refresh.get("retry"))
    components: dict[str, ComponentRun]
    if retry and published is not None and published.source_code == source_code:
        components = component_runs_of(published)
    elif refresh.get("components"):
        components = {
            str(c): ComponentRun.from_dict(str(c), data)
            for c, data in dict(refresh["components"]).items()
        }
    else:
        components = components_from_run(run)
    acquired = _time(refresh.get("acquired_at")) or run.completed_at
    updated = [
        int(s) for s in refresh.get("updated_spot_ids") or (run.details or {}).get("spot_ids") or []
    ]
    failed = {int(s): str(err) for s, err in dict(refresh.get("failed_spots") or {}).items()}
    if not failed:
        from app.services.forecasts.ingestion import stored_spot_errors

        failed = stored_spot_errors(run.details, "the refresh failed")
    spot_runs: dict[int, SpotRun] = {s: SpotRun(run_id, UPDATED, acquired) for s in updated}
    for spot_id, error in failed.items():
        spot_runs.setdefault(spot_id, SpotRun(None, STALE, None, error))
    detected = _time(refresh.get("detected_at"))
    if retry and published is not None:
        detected = published.detected_at
    kind = KIND_RETRY if retry else KIND_REFRESH
    retry_info = refresh.get("retry") if isinstance(refresh.get("retry"), dict) else None
    upstream = None
    if retry and published is not None:
        upstream = published.upstream_available_at
    return stage_version(
        db,
        provider=str(refresh.get("provider") or token_for_source(source_code)),
        source_code=source_code,
        forecast_run_id=run_id,
        components=components,
        spot_runs=spot_runs,
        sync_run_id=sync_run_id,
        timings={
            "kind": kind,
            **({"retry_attempt": retry_info.get("attempt")} if retry_info else {}),
            "changed": list(refresh.get("changed") or []),
            **dict(timings or {}),
        },
        fallback_from=refresh.get("fallback_from"),
        detected_at=detected,
        upstream_available_at=upstream,
        acquired_at=acquired,
        scored_at=scored_at,
        now=now,
    )


def publish_staged(
    db: Session, version: ForecastVersion, *, now: datetime | None = None
) -> ForecastVersion:
    """Publish a staged version in one transaction; when that fails the version is marked
    failed (redacted reason) and the previous version stays published, then the error is
    raised."""
    try:
        return publish_version(db, version, now=now)
    except Exception as exc:
        db.rollback()
        try:
            fail_version(db, version, f"{type(exc).__name__}: {exc}")
        except Exception:  # never hides the publication error
            db.rollback()
            logger.exception("Version %s could not be marked failed", version.id)
        raise


def publish_selection_version(
    db: Session,
    *,
    sync_run_id: int | None = None,
    timings: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ForecastVersion | None:
    """Stage and publish the run selection as a version (several providers, the developer
    pipeline, ``sync publish``): every spot of ``queries.latest_runs_by_spot`` (runs with
    predictions) as updated; the version's provider, source and components are those of
    the run most spots are served from. None when no spot has a scored run."""
    from app.services.forecasts.queries import latest_runs_by_spot

    runs = latest_runs_by_spot(db, now)
    if not runs:
        return None
    counts = Counter(run.id for run in runs.values())
    primary_id = max(counts, key=lambda rid: (counts[rid], rid))
    primary = next(run for run in runs.values() if run.id == primary_id)
    spot_runs = {
        spot_id: SpotRun(run.id, UPDATED, run.completed_at or run.started_at)
        for spot_id, run in runs.items()
    }
    version = stage_version(
        db,
        provider=token_for_source(primary.source.code),
        source_code=primary.source.code,
        forecast_run_id=primary.id,
        components=components_from_run(primary),
        spot_runs=spot_runs,
        sync_run_id=sync_run_id,
        timings={"kind": KIND_SELECTION, **dict(timings or {})},
        acquired_at=primary.completed_at,
        scored_at=primary.predictions_computed_at,
        now=now,
    )
    try:
        return publish_staged(db, version, now=now)
    except VersionPublishError as exc:
        logger.warning("Selection version %s not published: %s", version.id, exc)
        return None


def map_run_ref(db: Session) -> MapRunRef | None:
    """The map run the version payload names: ``sync_runs.map_run_id`` of the newest
    publication that has one, with that run's cycle (``issued_at``). The same rule as the
    serving side's marker re-seed, so a marker rebuilt after a Redis loss is identical."""
    newest = (
        select(SyncRun.map_run_id)
        .where(SyncRun.published_at.is_not(None), SyncRun.map_run_id.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = db.execute(
        select(ForecastRun.id, ForecastRun.issued_at).where(ForecastRun.id == newest)
    ).first()
    return MapRunRef(int(row[0]), row[1]) if row is not None else None


def write_marker(db: Session) -> bool:
    """Store the published version's payload (C4) with the published map run as the
    version marker in Redis. Never raises (False when it was not stored)."""
    try:
        payload = version_payload(current_version(db), map_run_ref(db))
    except Exception:
        logger.warning("Version marker not built", exc_info=True)
        return False
    return write_version_marker(payload)
