"""Job H — retention policy and housekeeping.

* forecast runs older than RETENTION_FORECAST_DAYS are deleted (their wave forecasts and
  predictions cascade), always keeping the two newest usable runs per source;
* map fields (the map's ocean layers, about 0.1 MB per field and 0.5 deg) are kept for the
  two best servable runs only (:func:`prune_map_fields`, also run after every acquisition
  that stores fields); other map_fields rows are deleted (a deleted forecast run also
  takes its fields with it);
* flight searches older than RETENTION_FLIGHT_DAYS are deleted unless an offer is still
  referenced by an open opportunity;
* matches whose surf window has passed become EXPIRED;
* expired/used auth tokens, expired sessions and old job logs are removed;
* notifications older than RETENTION_NOTIFICATION_DAYS are deleted;
* the prediction archive, forecasts at observation stations, buoy observations and
  verification pairs are kept for RETENTION_ARCHIVE_DAYS (they are the only record of
  past forecast skill, so they outlive forecast runs by far);
* past tide predictions are pruned by app.services.tides.maintenance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, exists, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    BackgroundJobLog,
    BuoyObservation,
    FlightOffer,
    FlightSearch,
    ForecastRun,
    Notification,
    OpportunityMatch,
    OpportunityOffer,
    PointForecast,
    PredictionArchive,
    PredictionValidation,
    UserSession,
    UserToken,
)
from app.models.enums import ForecastRunStatus, MatchStatus
from app.models.forecast_engine import MapField
from app.services.map_fields import servable_runs_query
from app.services.tides.maintenance import prune_tide_predictions

logger = logging.getLogger(__name__)

MAP_FIELD_RUNS_KEPT = 2


def prune_map_fields(db: Session, keep: int = MAP_FIELD_RUNS_KEPT) -> int:
    """Delete the map fields of every run but the ``keep`` best servable ones.

    "Best" is the read side's order (``map_fields.servable_runs``: newest cycle, then the
    most complete valid times, then the newer run), so the served run is always kept.
    Rows of runs without a single complete valid time are never served and go too
    (a failed fields run that is retried re-fetches every hour anyway). Returns the
    number of rows deleted; the caller commits.

    One statement, so the runs kept and the rows deleted come from the same snapshot:
    fields another acquisition commits meanwhile are neither seen nor deleted.
    """
    best = servable_runs_query(limit=keep).subquery()
    stmt = delete(MapField).where(MapField.run_id.notin_(select(best.c.run_id)))
    return db.execute(stmt).rowcount or 0  # type: ignore[attr-defined]


def cleanup(db: Session, now: datetime | None = None) -> dict[str, int]:
    s = get_settings()
    now = now or utcnow()
    out: dict[str, int] = {}

    out["map_fields"] = prune_map_fields(db)

    keep_ids: set[int] = set()
    for source_id in db.scalars(select(ForecastRun.source_id).distinct()).all():
        keep_ids.update(
            db.scalars(
                select(ForecastRun.id)
                .where(
                    ForecastRun.source_id == source_id,
                    ForecastRun.status.in_((ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)),
                )
                .order_by(ForecastRun.issued_at.desc())
                .limit(2)
            ).all()
        )
    stmt = delete(ForecastRun).where(
        ForecastRun.started_at < now - timedelta(days=s.retention_forecast_days)
    )
    if keep_ids:
        stmt = stmt.where(ForecastRun.id.notin_(keep_ids))
    out["forecast_runs"] = db.execute(stmt).rowcount or 0  # type: ignore[attr-defined]

    referenced = exists().where(
        and_(
            FlightOffer.flight_search_id == FlightSearch.id,
            exists().where(
                and_(
                    OpportunityOffer.offer_id == FlightOffer.id,
                    OpportunityOffer.match_id == OpportunityMatch.id,
                    OpportunityMatch.status.in_(
                        (MatchStatus.FLIGHT_FOUND, MatchStatus.PENDING_FLIGHTS)
                    ),
                )
            ),
        )
    )
    out["flight_searches"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(FlightSearch).where(
                FlightSearch.searched_at < now - timedelta(days=s.retention_flight_days),
                ~referenced,
            )
        ).rowcount
        or 0
    )

    out["matches_expired"] = (
        db.execute(  # type: ignore[attr-defined]
            update(OpportunityMatch)
            .where(
                OpportunityMatch.window_end < now,
                OpportunityMatch.status.notin_((MatchStatus.EXPIRED, MatchStatus.DISMISSED)),
            )
            .values(
                status=MatchStatus.EXPIRED, status_reason="surf window has passed", updated_at=now
            )
        ).rowcount
        or 0
    )

    out["tokens"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(UserToken).where(
                (UserToken.expires_at < now - timedelta(days=1))
                | (UserToken.used_at < now - timedelta(days=1))
            )
        ).rowcount
        or 0
    )
    out["sessions"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(UserSession).where(
                (UserSession.expires_at < now) | (UserSession.revoked_at < now - timedelta(days=1))
            )
        ).rowcount
        or 0
    )
    out["job_logs"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(BackgroundJobLog).where(
                BackgroundJobLog.started_at < now - timedelta(days=s.retention_job_log_days)
            )
        ).rowcount
        or 0
    )
    out["notifications"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(Notification).where(
                Notification.created_at < now - timedelta(days=s.retention_notification_days)
            )
        ).rowcount
        or 0
    )
    archive_cutoff = now - timedelta(days=s.retention_archive_days)
    for key, model, column in (
        ("prediction_archive", PredictionArchive, PredictionArchive.valid_time),
        ("point_forecasts", PointForecast, PointForecast.valid_time),
        ("buoy_observations", BuoyObservation, BuoyObservation.observed_at),
        ("prediction_validation", PredictionValidation, PredictionValidation.valid_time),
    ):
        out[key] = db.execute(delete(model).where(column < archive_cutoff)).rowcount or 0  # type: ignore[attr-defined]
    out["tide_predictions"] = prune_tide_predictions(db, now)
    db.commit()
    logger.info("Cleanup: %s", out)
    return out
