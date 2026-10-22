"""Job H — retention policy and housekeeping.

* forecast runs older than RETENTION_FORECAST_DAYS are deleted (their wave forecasts and
  predictions cascade), always keeping the two newest usable runs per source;
* the surf-quality predictions of superseded runs of the blend's member sources are
  deleted (:func:`prune_superseded_predictions`): derived, recomputable data, about 2 kB
  per row and every member run scored as well as the blend. The newest usable run of every
  source and every run that is authoritative for some spot keep theirs; blend predictions
  are never pruned here;
* map fields (the map's ocean layers, about 0.1 MB per field and 0.5 deg) are kept for the
  two best servable runs and the run of the newest sync publication (the run the map
  serves under SYNC_PUBLISH_GATE) only (:func:`prune_map_fields`, also run after every
  acquisition that stores fields and by every sync's prune stage, :func:`prune_pass`);
  other map_fields rows are deleted (a deleted forecast run also takes its fields with it);
* flight searches older than RETENTION_FLIGHT_DAYS are deleted unless an offer is still
  referenced by an open opportunity;
* matches whose surf window has passed become EXPIRED;
* expired/used auth tokens, expired sessions and old job logs are removed; so are sync_runs
  rows started more than RETENTION_JOB_LOG_DAYS ago, except the newest publication (the
  map index reads it) and a row still marked running (the next sync settles its lease);
* notifications older than RETENTION_NOTIFICATION_DAYS are deleted;
* the prediction archive, forecasts at observation stations, buoy observations and
  verification pairs are kept for RETENTION_ARCHIVE_DAYS (they are the only record of
  past forecast skill, so they outlive forecast runs by far); quality-controlled buoy
  observations (qc_flag good or probably_good) are kept for OBSERVATION_RETENTION_DAYS
  instead (default 10 years; a measured record, not recomputable);
* past tide predictions are pruned by app.services.tides.maintenance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    BackgroundJobLog,
    BuoyObservation,
    FlightOffer,
    FlightSearch,
    ForecastRun,
    ForecastSource,
    Notification,
    OpportunityMatch,
    OpportunityOffer,
    PointForecast,
    PredictionArchive,
    PredictionValidation,
    SurfQualityPrediction,
    UserSession,
    UserToken,
)
from app.models.enums import ForecastRunStatus, MatchStatus
from app.models.forecast_engine import MapField
from app.models.sync import SyncRun
from app.services.forecasts.queries import PREDICTIONS_PRUNED_KEY, latest_runs_by_spot
from app.services.map_fields import servable_runs_query
from app.services.tides.maintenance import prune_tide_predictions

logger = logging.getLogger(__name__)

MAP_FIELD_RUNS_KEPT = 2
QC_PASSED = ("good", "probably_good")
BLEND_CODE = "blend"  # app.services.forecasts.blend.BLEND_CODE (not imported: cycle)
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)


def prune_superseded_predictions(db: Session, now: datetime | None = None) -> int:
    """Delete the predictions of superseded non-blend runs; returns the rows deleted.

    Kept: every blend run's predictions, the newest usable run of each source, and every
    run that is authoritative for at least one spot now (a partial newest run can leave an
    older run serving some spots). A pruned run keeps its forecasts and is marked
    ``details["predictions_pruned_at"]``; resetting its ``predictions_computed_at`` makes
    the prediction job score it again. The caller commits.
    """
    keep = {run.id for run in latest_runs_by_spot(db, now).values()}
    ranked = (
        select(
            ForecastRun.id,
            func.row_number()
            .over(
                partition_by=ForecastRun.source_id,
                order_by=(ForecastRun.issued_at.desc(), ForecastRun.id.desc()),
            )
            .label("rank"),
        )
        .where(ForecastRun.status.in_(USABLE))
        .subquery()
    )
    keep.update(db.scalars(select(ranked.c.id).where(ranked.c.rank == 1)).all())
    superseded = (
        select(ForecastRun)
        .join(ForecastSource)
        .where(
            ForecastSource.code != BLEND_CODE,
            ForecastRun.predictions_computed_at.is_not(None),
            exists().where(SurfQualityPrediction.run_id == ForecastRun.id),
        )
    )
    if keep:
        superseded = superseded.where(ForecastRun.id.notin_(keep))
    runs = list(db.scalars(superseded))
    if not runs:
        return 0
    stamp = utcnow().isoformat()  # wall clock, as predictions_computed_at (queries)
    for run in runs:
        run.details = {**(run.details or {}), PREDICTIONS_PRUNED_KEY: stamp}
    stmt = delete(SurfQualityPrediction).where(
        SurfQualityPrediction.run_id.in_([run.id for run in runs])
    )
    return db.execute(stmt).rowcount or 0  # type: ignore[attr-defined]


def _newest_publication_id() -> Select[int]:
    """The id of the newest sync publication, as app.services.sync.published orders them."""
    return (
        select(SyncRun.id)
        .where(SyncRun.published_at.is_not(None))
        .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
        .limit(1)
    )


def prune_map_fields(db: Session, keep: int = MAP_FIELD_RUNS_KEPT) -> int:
    """Delete the map fields of every run but the ``keep`` best servable ones and the map
    run of the newest sync publication.

    "Best" is the read side's order (``map_fields.servable_runs``: newest cycle, then the
    most complete valid times, then the newer run). The served run is always kept: it is
    the published run under SYNC_PUBLISH_GATE, otherwise the best one. Normally the
    published run is one of the two best anyway (a sync stores one new map run, then
    publishes it); only while newer runs wait for their publication is a third run kept.
    Rows of runs without a single complete valid time are never served and go too
    (a failed fields run that is retried re-fetches every hour anyway). Returns the
    number of rows deleted; the caller commits.

    One statement, so the runs kept and the rows deleted come from the same snapshot:
    fields another acquisition commits meanwhile are neither seen nor deleted.
    """
    best = servable_runs_query(limit=keep).subquery()
    published = select(SyncRun.map_run_id).where(
        SyncRun.id.in_(_newest_publication_id()), SyncRun.map_run_id.is_not(None)
    )
    stmt = delete(MapField).where(
        MapField.run_id.notin_(select(best.c.run_id)), MapField.run_id.notin_(published)
    )
    return db.execute(stmt).rowcount or 0  # type: ignore[attr-defined]


def prune_pass(db: Session, now: datetime | None = None) -> dict[str, int]:
    """The light prune a sync runs after publishing (its ``prune`` stage, when
    SYNC_PRUNE_EACH_PASS): the map fields of runs that are neither among the two best
    servable ones nor published (:func:`prune_map_fields`), then the predictions of
    superseded member runs (:func:`prune_superseded_predictions`, when
    PRUNE_SUPERSEDED_MEMBER_PREDICTIONS). Both are derived data the sync can recompute; the
    rest of the retention policy stays with the daily :func:`cleanup`. Commits; returns
    the rows deleted, ``{"superseded_predictions", "map_fields"}``."""
    out = {"superseded_predictions": 0, "map_fields": prune_map_fields(db)}
    if get_settings().prune_superseded_member_predictions:
        out["superseded_predictions"] = prune_superseded_predictions(db, now)
    db.commit()
    logger.info("Prune pass: %s", out)
    return out


def prune_sync_runs(db: Session, now: datetime) -> int:
    """Delete sync_runs rows started more than RETENTION_JOB_LOG_DAYS before ``now``, except
    the newest publication and rows still marked running. The caller commits."""
    cutoff = now - timedelta(days=get_settings().retention_job_log_days)
    stmt = delete(SyncRun).where(
        SyncRun.started_at < cutoff,
        SyncRun.status != "running",
        SyncRun.id.notin_(_newest_publication_id()),
    )
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
    if s.prune_superseded_member_predictions:
        out["superseded_predictions"] = prune_superseded_predictions(db, now)

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
    out["sync_runs"] = prune_sync_runs(db, now)
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
        ("prediction_validation", PredictionValidation, PredictionValidation.valid_time),
    ):
        out[key] = db.execute(delete(model).where(column < archive_cutoff)).rowcount or 0  # type: ignore[attr-defined]
    qc_cutoff = now - timedelta(days=s.observation_retention_days)
    out["buoy_observations"] = (
        db.execute(  # type: ignore[attr-defined]
            delete(BuoyObservation).where(
                or_(
                    and_(
                        BuoyObservation.qc_flag.in_(QC_PASSED),
                        BuoyObservation.observed_at < qc_cutoff,
                    ),
                    and_(
                        BuoyObservation.qc_flag.notin_(QC_PASSED),
                        BuoyObservation.observed_at < archive_cutoff,
                    ),
                )
            )
        ).rowcount
        or 0
    )
    out["tide_predictions"] = prune_tide_predictions(db, now)
    db.commit()
    logger.info("Cleanup: %s", out)
    return out
