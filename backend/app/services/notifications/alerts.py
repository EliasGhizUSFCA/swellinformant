"""Job F — decide which opportunities deserve an alert and queue notifications.

De-duplication: each notification has a ``dedup_key`` (unique in the database) built
from the match, channel, alert kind and a fingerprint of what the user is being told
(quality label, price bucket, travel dates). Re-running the job, or two workers racing,
can therefore never queue the same alert twice. A follow-up alert for the same swell is
only generated when (and if the search opts into updates):

* flights became available for a previously surf-only alert,
* predicted quality improved by ≥ 1 label or ≥ ALERT_SCORE_IMPROVEMENT points, or
* the fare dropped by ≥ ALERT_PRICE_DROP_PCT % and ≥ ALERT_PRICE_DROP_MIN_AMOUNT, or
* the swell timing shifted so recommended travel dates moved by ≥ 2 days.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    ForecastRun,
    Notification,
    NotificationPreference,
    OpportunityMatch,
    SavedSearch,
    SwellEvent,
    User,
)
from app.models.enums import (
    QUALITY_ORDER,
    MatchStatus,
    NotificationChannel,
    NotificationChannelPref,
    NotificationKind,
    NotificationStatus,
    OfferValidation,
    QualityLabel,
    SearchStatus,
    SwellEventStatus,
)
from app.services.flights.filtering import convert
from app.services.flights.links import google_flights_url
from app.services.flights.registry import build_flight_provider
from app.services.flights.service import revalidate_offer
from app.services.geo import compass_label
from app.services.http import ProviderError
from app.services.notifications.templates import fmt_range, render_alert_email, render_alert_sms

logger = logging.getLogger(__name__)

REVALIDATE_AFTER = timedelta(hours=2)


@dataclass
class AlertStats:
    considered: int = 0
    queued: int = 0
    duplicates: int = 0
    capped: int = 0
    no_channel: int = 0
    revalidated: int = 0
    invalidated: int = 0
    stale_forecast: int = 0

    def as_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


def label_rank(label: str | QualityLabel) -> int:
    return QUALITY_ORDER.index(QualityLabel(label))


def build_snapshot(match: OpportunityMatch, kind: NotificationKind) -> dict[str, Any]:
    event = match.swell_event
    spot = match.spot
    offer = match.best_offer if match.status == MatchStatus.FLIGHT_FOUND else None
    snap: dict[str, Any] = {
        "kind": kind.value,
        "match_id": str(match.id),
        "event_id": str(event.id),
        "spot_name": spot.name,
        "spot_slug": spot.slug,
        "country": spot.country,
        "window_start": match.window_start.isoformat(),
        "window_end": match.window_end.isoformat(),
        "window_label": fmt_range(
            match.window_start.isoformat(), match.window_end.isoformat(), spot.timezone
        ),
        "height_min_ft": match.breaking_height_min_ft,
        "height_max_ft": match.breaking_height_max_ft,
        "peak_label": match.peak_label.value,
        "peak_score": match.peak_score,
        "confidence": match.confidence,
        "confidence_label": match.confidence_label.value,
        "swell_height_m": event.peak_swell_height_m,
        "swell_period_s": round(event.peak_swell_period_s) if event.peak_swell_period_s else None,
        "swell_direction": compass_label(event.peak_swell_direction_deg),
        "wind_relation": event.peak_wind_relation,
        "wind_speed_kmh": round(event.peak_wind_speed_kmh)
        if event.peak_wind_speed_kmh is not None
        else None,
        "wind_direction": compass_label(event.peak_wind_direction_deg),
        "is_demo_forecast": event.is_demo,
        "arrival_date": match.recommended_arrival_date.isoformat()
        if match.recommended_arrival_date
        else None,
        "departure_date": match.recommended_departure_date.isoformat()
        if match.recommended_departure_date
        else None,
        "price": None,
        "currency": match.currency,
    }
    if offer is not None and match.best_price is not None:
        snap.update(
            {
                "price": str(match.best_price),
                "currency": match.currency,
                "origin": offer.origin_iata,
                "destination": offer.destination_iata,
                "airline": ", ".join(offer.airline_names[:2]),
                "outbound_hours": round(offer.outbound_duration_minutes / 60, 1),
                "inbound_hours": round(offer.inbound_duration_minutes / 60, 1),
                "offer_id": str(offer.id),
                "provider": offer.provider,
                "is_mock_flight": offer.is_mock,
                "validation": offer.validation_status.value,
                "price_checked_label": (
                    f"re-validated {offer.last_validated_at:%Y-%m-%d %H:%M} UTC"
                    if offer.last_validated_at
                    and offer.validation_status
                    in (OfferValidation.VALID, OfferValidation.PRICE_CHANGED)
                    else f"quoted {offer.quoted_at:%Y-%m-%d %H:%M} UTC (not re-validated)"
                ),
            }
        )
    return snap


def decide_kind(
    match: OpportunityMatch, search: SavedSearch, has_price: bool
) -> NotificationKind | None:
    settings = get_settings()
    prev = match.last_notified_snapshot
    if prev is None:
        if has_price:
            return NotificationKind.NEW_OPPORTUNITY
        return NotificationKind.SURF_ONLY if search.notify_surf_only else None
    if not search.notify_on_updates:
        return None
    if prev.get("price") is None and has_price:
        return NotificationKind.NEW_OPPORTUNITY
    if label_rank(match.peak_label) > label_rank(prev["peak_label"]) or (
        match.peak_score >= int(prev["peak_score"]) + settings.alert_score_improvement
    ):
        return NotificationKind.IMPROVED
    if has_price and prev.get("price") is not None and match.best_price is not None:
        old = Decimal(str(prev["price"]))
        new = Decimal(match.best_price)
        if prev.get("currency") == match.currency and (
            new <= old * (Decimal(1) - Decimal(str(settings.alert_price_drop_pct)) / 100)
            and old - new >= Decimal(str(settings.alert_price_drop_min_amount))
        ):
            return NotificationKind.PRICE_DROP
    if has_price and _dates_shifted(prev, match):
        return NotificationKind.SCHEDULE_CHANGE
    return None


SCHEDULE_SHIFT_DAYS = 2


def _dates_shifted(prev: dict[str, Any], match: OpportunityMatch) -> bool:
    """True when recommended arrival or departure moved by ≥ SCHEDULE_SHIFT_DAYS."""
    for key, current in (
        ("arrival_date", match.recommended_arrival_date),
        ("departure_date", match.recommended_departure_date),
    ):
        old = prev.get(key)
        if old and current and abs((current - date.fromisoformat(old)).days) >= SCHEDULE_SHIFT_DAYS:
            return True
    return False


def fingerprint(snapshot: dict[str, Any]) -> str:
    """What the user is being told. Same fingerprint ⇒ substantially identical alert."""
    price = snapshot.get("price")
    bucket = "none" if price is None else str(int(Decimal(price) // 10))
    return ":".join(
        [
            snapshot["kind"],
            snapshot["peak_label"],
            bucket,
            str(snapshot.get("origin")),
            str(snapshot.get("arrival_date")),
            str(snapshot.get("departure_date")),
        ]
    )


def dedup_key(user_id: object, channel: NotificationChannel, snapshot: dict[str, Any]) -> str:
    """Keyed by user + swell event (not by search), so overlapping searches that surface
    the same swell with the same trip never alert the user twice."""
    raw = f"{user_id}:{snapshot['event_id']}:{channel.value}:{fingerprint(snapshot)}"
    return f"alert:{hashlib.sha256(raw.encode()).hexdigest()[:48]}"


def channels_for(
    search: SavedSearch, user: User, prefs: NotificationPreference
) -> list[NotificationChannel]:
    if prefs.all_paused:
        return []
    wanted = {
        NotificationChannelPref.EMAIL: [NotificationChannel.EMAIL],
        NotificationChannelPref.SMS: [NotificationChannel.SMS],
        NotificationChannelPref.BOTH: [NotificationChannel.EMAIL, NotificationChannel.SMS],
        NotificationChannelPref.DASHBOARD: [],
    }[search.notification_channel]
    out: list[NotificationChannel] = []
    for ch in wanted:
        if ch == NotificationChannel.EMAIL and prefs.email_enabled and user.email_verified_at:
            out.append(ch)
        if ch == NotificationChannel.SMS and prefs.sms_enabled and prefs.sms_ready:
            out.append(ch)
    return out


def links_for(snapshot: dict[str, Any], prefs: NotificationPreference) -> dict[str, str]:
    base = get_settings().app_base_url
    links = {
        "opportunity": f"{base}/opportunities/{snapshot['match_id']}",
        "forecast": f"{base}/spots/{snapshot['spot_slug']}",
        "unsubscribe": f"{base}/unsubscribe?token={prefs.unsubscribe_token}",
    }
    if (
        snapshot.get("price") is not None
        and snapshot.get("arrival_date")
        and snapshot.get("origin")
    ):
        links["flights"] = google_flights_url(
            snapshot["origin"],
            snapshot["destination"],
            datetime.fromisoformat(snapshot["arrival_date"]).date(),
            datetime.fromisoformat(snapshot["departure_date"]).date(),
        )
    return links


def _alerts_today(db: Session, user_id: object, now: datetime) -> int:
    return int(
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.created_at >= now - timedelta(hours=24),
                Notification.status != NotificationStatus.SKIPPED,
            )
        )
        or 0
    )


def _revalidate_best(
    db: Session, match: OpportunityMatch, now: datetime, stats: AlertStats
) -> bool:
    """Returns False when the best offer is no longer usable (match goes back to search)."""
    offer = match.best_offer
    if offer is None:
        return False
    if (
        offer.last_validated_at
        and now - offer.last_validated_at < REVALIDATE_AFTER
        and (offer.offer_expires_at is None or offer.offer_expires_at > now)
    ):
        return offer.validation_status != OfferValidation.UNAVAILABLE
    try:
        provider = build_flight_provider(db)
        status = revalidate_offer(db, provider, offer, now)
    except ProviderError as exc:
        logger.warning("Offer revalidation failed for %s: %s", offer.id, exc)
        return offer.offer_expires_at is None or offer.offer_expires_at > now
    stats.revalidated += 1
    if status == OfferValidation.UNAVAILABLE:
        stats.invalidated += 1
        # Drop the cached search too, so the re-search asks the provider for fresh offers.
        offer.flight_search.expires_at = now
        match.status = MatchStatus.PENDING_FLIGHTS
        match.status_reason = "best offer expired or sold out; searching again"
        return False
    if status == OfferValidation.PRICE_CHANGED:
        travel = match.search.travel
        converted = convert(
            offer.total_price, offer.currency, travel.currency, get_settings().fx_rates_usd
        )
        if converted is None:
            match.status = MatchStatus.PENDING_FLIGHTS
            return False
        per_traveler = (converted / travel.travelers).quantize(Decimal("0.01"))
        match.best_price = per_traveler
        if per_traveler > Decimal(travel.max_price):
            stats.invalidated += 1
            match.status = MatchStatus.PENDING_FLIGHTS
            match.status_reason = "fare increased above your budget; searching again"
            return False
    return True


def generate_alerts(db: Session, now: datetime | None = None) -> AlertStats:
    settings = get_settings()
    now = now or utcnow()
    stats = AlertStats()
    fresh_after = now - timedelta(hours=settings.forecast_stale_hours * 2)
    rows = db.execute(
        select(OpportunityMatch, ForecastRun.issued_at)
        .join(SavedSearch, SavedSearch.id == OpportunityMatch.search_id)
        .join(User, User.id == OpportunityMatch.user_id)
        .join(SwellEvent, SwellEvent.id == OpportunityMatch.swell_event_id)
        .outerjoin(ForecastRun, ForecastRun.id == SwellEvent.latest_run_id)
        .where(
            SavedSearch.status == SearchStatus.ACTIVE,
            User.is_active,
            SwellEvent.status == SwellEventStatus.ACTIVE,
            OpportunityMatch.window_start > now,
            OpportunityMatch.status.in_((MatchStatus.FLIGHT_FOUND, MatchStatus.SURF_ONLY)),
        )
    ).all()
    for match, issued_at in rows:
        stats.considered += 1
        search = match.search
        if issued_at is None or issued_at < fresh_after:
            stats.stale_forecast += 1  # never alert on stale data
            continue
        if (
            match.status == MatchStatus.SURF_ONLY
            and match.flight_checked_at is None
            and match.destination_iata
        ):
            continue  # flights not searched yet
        has_price = match.status == MatchStatus.FLIGHT_FOUND
        if has_price and not _revalidate_best(db, match, now, stats):
            db.commit()
            continue
        kind = decide_kind(match, search, has_price)
        if kind is None:
            continue
        user = db.get(User, match.user_id)
        prefs = db.get(NotificationPreference, match.user_id)
        if user is None or prefs is None:
            continue
        channels = channels_for(search, user, prefs)
        if not channels:
            stats.no_channel += 1
            continue
        if _alerts_today(db, user.id, now) >= prefs.max_alerts_per_day:
            stats.capped += 1
            logger.info("Daily alert cap reached for user %s", user.id)
            continue
        snapshot = build_snapshot(match, kind)
        links = links_for(snapshot, prefs)
        subject, text, html = render_alert_email(snapshot, links)
        queued_any = False
        for channel in channels:
            key = dedup_key(user.id, channel, snapshot)
            values = {
                "user_id": user.id,
                "match_id": match.id,
                "channel": channel.value,
                "kind": kind.value,
                "status": NotificationStatus.QUEUED.value,
                "dedup_key": key,
                "recipient": user.email
                if channel == NotificationChannel.EMAIL
                else (prefs.phone_number or ""),
                "subject": subject,
                "body_text": text
                if channel == NotificationChannel.EMAIL
                else render_alert_sms(snapshot, links["opportunity"]),
                "body_html": html if channel == NotificationChannel.EMAIL else None,
                "snapshot": snapshot,
                "attempts": 0,
                "next_attempt_at": now,
                "created_at": now,
                "updated_at": now,
            }
            result = db.execute(
                insert(Notification)
                .values(values)
                .on_conflict_do_nothing(index_elements=["dedup_key"])
            )
            if result.rowcount:  # type: ignore[attr-defined]
                stats.queued += 1
                queued_any = True
            else:
                stats.duplicates += 1
        # Record the snapshot even when every insert was a duplicate: the user has already
        # been told about this exact opportunity (e.g. via an overlapping search).
        match.last_notified_snapshot = snapshot
        if queued_any:
            match.last_notified_at = now
            match.notification_count += 1
        db.commit()
    logger.info("Alert generation: %s", stats.as_dict())
    return stats
