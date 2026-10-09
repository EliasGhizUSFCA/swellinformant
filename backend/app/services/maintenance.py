"""Job H — retention policy and housekeeping.

* forecast runs older than RETENTION_FORECAST_DAYS are deleted (their wave forecasts and
  predictions cascade), always keeping the two newest usable runs per source;
* flight searches older than RETENTION_FLIGHT_DAYS are deleted unless an offer is still
  referenced by an open opportunity;
* matches whose surf window has passed become EXPIRED;
* expired/used auth tokens, expired sessions and old job logs are removed;
* notifications older than RETENTION_NOTIFICATION_DAYS are deleted.
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
    FlightOffer,
    FlightSearch,
    ForecastRun,
    Notification,
    OpportunityMatch,
    OpportunityOffer,
    UserSession,
    UserToken,
)
from app.models.enums import ForecastRunStatus, MatchStatus

logger = logging.getLogger(__name__)


def cleanup(db: Session, now: datetime | None = None) -> dict[str, int]:
    s = get_settings()
    now = now or utcnow()
    out: dict[str, int] = {}

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
    db.commit()
    logger.info("Cleanup: %s", out)
    return out
