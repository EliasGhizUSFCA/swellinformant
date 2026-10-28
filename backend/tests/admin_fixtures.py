"""SYNTHETIC rows for the admin freshness dashboard and the admin alerts (design D12).

Nothing here was observed: the rows are written by the tests. The shapes follow what the
pipeline records (app.services.forecasts.versions, app.services.sync, the ingestion's
``details["stats"]``), and the timings are modelled on the Open-Meteo GFS publication
delays measured on 2026-10-26 (research r-updates: GFS-Wave about 317 min and GFS 0.13 about
322 min after the cycle), so the figures look like a real deployment's.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource, SurfSpot, User
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.models.version import ForecastVersion
from app.services.forecasts import versions
from app.services.forecasts.base import ATMOSPHERE, WAVE
from app.services.forecasts.versions import ComponentRun, DomainRun, SpotRun

SOURCE = "open_meteo_noaa"
WAVE_LAG = timedelta(minutes=317)
WIND_LAG = timedelta(minutes=322)
INTERVAL_S = 21600


def source(db: Session, code: str = SOURCE, provider: str = "open_meteo") -> ForecastSource:
    row = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
    if row is None:
        row = ForecastSource(code=code, name=f"Synthetic {code}", provider=provider)
        db.add(row)
        db.commit()
    return row


def cycle_of(when: datetime) -> datetime:
    """The newest 6-hourly GFS cycle at or before ``when``."""
    return when.replace(hour=when.hour - when.hour % 6, minute=0, second=0, microsecond=0)


def forecast_run(
    db: Session,
    issued: datetime,
    key: str,
    *,
    status: ForecastRunStatus = ForecastRunStatus.SUCCESS,
    started: datetime | None = None,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    code: str = SOURCE,
    spots: int = 50,
    wave_run: datetime | None = None,
    wind_run: datetime | None = None,
) -> ForecastRun:
    """A run of ``issued``'s cycle; ``wave_run`` / ``wind_run``: the component model runs
    it holds (default ``issued``; a one-component refresh carries the other's older run)."""
    started = started or issued + WAVE_LAG + timedelta(minutes=12)
    run = ForecastRun(
        source_id=source(db, code).id,
        run_key=key,
        issued_at=issued,
        wave_model_run_at=wave_run or issued,
        atmosphere_model_run_at=wind_run or issued,
        started_at=started,
        completed_at=started + timedelta(seconds=2),
        status=status,
        spot_count=spots if status != ForecastRunStatus.FAILED else 0,
        request_count=int((stats or {}).get("requests") or 0),
        error_message=error,
        details={"stats": stats or {}},
        predictions_computed_at=(
            started + timedelta(seconds=11) if status != ForecastRunStatus.FAILED else None
        ),
    )
    db.add(run)
    db.commit()
    return run


def wave(
    init: datetime,
    *,
    changed: bool = True,
    retrieved: datetime | None = None,
    lag: timedelta = WAVE_LAG,
    carried_from: int | None = None,
) -> ComponentRun:
    return ComponentRun(
        component=WAVE,
        model="ncep_gfswave025",
        run_key=f"ncep_gfswave025:{init:%Y%m%d%H}",
        init_time=init,
        available_at=init + lag,
        retrieved_at=retrieved or init + lag + timedelta(minutes=12),
        changed=changed,
        carried_from_run_id=carried_from,
        update_interval_seconds=INTERVAL_S,
        domains=(DomainRun("ncep_gfswave025", init, init + lag),),
    )


def wind(
    init: datetime,
    *,
    changed: bool = True,
    retrieved: datetime | None = None,
    lag: timedelta = WIND_LAG,
    carried_from: int | None = None,
) -> ComponentRun:
    return ComponentRun(
        component=ATMOSPHERE,
        model="ncep_gfs013,ncep_gfs025",
        run_key=f"ncep_gfs013:{init:%Y%m%d%H}",
        init_time=init,
        available_at=init + lag,
        retrieved_at=retrieved or init + lag + timedelta(minutes=12),
        changed=changed,
        carried_from_run_id=carried_from,
        update_interval_seconds=INTERVAL_S,
        domains=(
            DomainRun("ncep_gfs013", init, init + lag - timedelta(minutes=41)),
            DomainRun("ncep_gfs025", init, init + lag),
        ),
    )


def active_spot_ids(db: Session) -> list[int]:
    return list(
        db.scalars(select(SurfSpot.id).where(SurfSpot.is_active.is_(True)).order_by(SurfSpot.id))
    )


def publish(
    db: Session,
    run: ForecastRun,
    components: Sequence[ComponentRun],
    *,
    published_at: datetime,
    stale: dict[int, str] | None = None,
    sync_run_id: int | None = None,
    fallback_from: str | None = None,
    provider: str = "open_meteo",
    kind: str = "refresh",
    updated: Sequence[int] | None = None,
) -> ForecastVersion:
    """Stage and publish a version of ``run``: every active spot updated (or ``updated``),
    the ``stale`` ones failed with their error (they keep their previous run)."""
    changed = [c for c in components if c.changed]
    detected = min(c.available_at for c in changed if c.available_at) if changed else None
    spot_runs: dict[int, SpotRun] = {
        sid: SpotRun(None) for sid in (updated if updated is not None else active_spot_ids(db))
    }
    for sid, error in (stale or {}).items():
        spot_runs[sid] = SpotRun(None, versions.STALE, None, error)
    acquired = published_at - timedelta(seconds=12)
    version = versions.stage_version(
        db,
        provider=provider,
        source_code=SOURCE,
        forecast_run_id=run.id,
        components=list(components),
        spot_runs=spot_runs,
        sync_run_id=sync_run_id,
        timings={"kind": kind, "changed": [c.component for c in changed]},
        fallback_from=fallback_from,
        detected_at=(detected + timedelta(minutes=10, seconds=40)) if detected else None,
        acquired_at=acquired,
        scored_at=published_at - timedelta(seconds=2),
        now=published_at - timedelta(seconds=1),
    )
    return versions.publish_version(db, version, now=published_at)


def sync_run(
    db: Session,
    started: datetime,
    *,
    trigger: str = "http",
    status: str = "success",
    kinds: Sequence[str] = ("spots",),
    stages: Sequence[tuple[str, int, float]] = (
        ("spots", 1900, 1.62),
        ("predict", 8950, 7.18),
        ("publish", 50, 0.03),
        ("detect", 1040, 0.65),
    ),
    error: str | None = None,
    published: bool = True,
) -> SyncRun:
    total_ms = sum(ms for _, ms, _ in stages)
    row = SyncRun(
        trigger=trigger,
        status=status,
        started_at=started,
        finished_at=started + timedelta(milliseconds=total_ms + 150),
        plan={"due": True, "due_kinds": list(kinds), "components": []},
        stages=[
            {
                "name": name,
                "status": "failed" if status == "failed" and i == len(stages) - 1 else "success",
                "started_at": started.isoformat(),
                "duration_ms": ms,
                "cpu_s": cpu,
                "detail": {},
            }
            for i, (name, ms, cpu) in enumerate(stages)
        ],
        published_at=started + timedelta(milliseconds=total_ms) if published else None,
        error_message=error,
    )
    db.add(row)
    db.commit()
    return row


def make_user(
    db: Session, email: str, *, admin: bool, verified: bool = True, active: bool = True
) -> User:
    user = User(
        email=email,
        password_hash="synthetic-hash",
        full_name="Synthetic Person",
        is_admin=admin,
        is_active=active,
        email_verified_at=utcnow() if verified else None,
    )
    db.add(user)
    db.commit()
    return user
