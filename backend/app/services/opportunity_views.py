"""Read models for opportunities (matches) and notification history."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings
from app.core.database import utcnow
from app.core.errors import NotFound
from app.models import (
    FlightOffer,
    Notification,
    OpportunityMatch,
    SurfQualityPrediction,
    SwellEvent,
    User,
)
from app.models.enums import MatchStatus
from app.schemas.forecasts import SpotFreshnessOut
from app.schemas.opportunities import (
    LinkOut,
    MatchDetail,
    MatchSummary,
    NotificationOut,
    OfferOut,
    SegmentOut,
    SliceOut,
    TravelWindowOut,
)
from app.schemas.spots import AirportOut, AttributionLinkOut, SpotAirportOut
from app.services.flights.base import operating_carrier_names
from app.services.flights.links import inspection_links
from app.services.flights.registry import provider_label
from app.services.flights.windows import InfeasibleWindow, compute_travel_window
from app.services.forecasts.queries import attribution_links_by_run, run_id_is_stale
from app.services.matching.scoring import SHORT_NOTICE_BOOST, is_short_notice
from app.services.spot_views import (
    daily_summaries,
    event_out,
    events_freshness,
    forecast_points,
    served_freshness,
    spot_mini,
)
from app.services.surf_quality.breaking import (
    METHOD_APPROX_V1,
    METHOD_CALIBRATED,
    METHOD_PHYSICAL,
)
from app.services.swell_detection.service import peak_confidence

# Plain-language names of the stored breaking_height_method values.
METHOD_TEXT = {
    METHOD_PHYSICAL: "linear wave theory (shoaling, refraction and depth-limited breaking) "
    "with uncalibrated spot factors",
    METHOD_APPROX_V1: "Komar–Gaughan shoaling with uncalibrated spot factors",
    METHOD_CALIBRATED: "a spot model calibrated against observations",
}
CONFIDENCE_CLASSES = ("high", "moderate", "low")


def get_owned_match(db: Session, user: User, match_id: uuid.UUID) -> OpportunityMatch:
    match = db.get(OpportunityMatch, match_id)
    if match is None or match.user_id != user.id:
        raise NotFound("Opportunity not found.")
    return match


def event_confidence(event: SwellEvent, now: datetime) -> dict[str, object]:
    """Forecast confidence at the event's peak hour, exactly as EventOut shows it
    (``peak_confidence`` with the read-time staleness cap), plus ``stale``."""
    db = object_session(event)
    stale = db is not None and run_id_is_stale(db, event.latest_run_id, now)
    fields = peak_confidence(event, stale=stale)

    def cls(key: str) -> str | None:
        value = fields.get(key)
        return value if value in CONFIDENCE_CLASSES else None

    return {
        "forecast_confidence_class": cls("forecast_confidence_class"),
        "stale": stale,
    }


def match_summary(
    m: OpportunityMatch,
    now: datetime | None = None,
    freshness: Mapping[int, SpotFreshnessOut] | None = None,
    links: Mapping[int, list[AttributionLinkOut]] | None = None,
) -> MatchSummary:
    """``freshness``: {spot_id: freshness} of the served forecast already read for a list
    of matches (else read for the match's spot); ``links``: {run id: attribution links} of
    the swells' runs already read for the list (else read for the match's run)."""
    now = now or utcnow()
    db = object_session(m)
    if freshness is not None:
        spot_freshness = freshness.get(m.spot_id)
    else:
        spot_freshness = served_freshness(db, m.spot_id, now) if db is not None else None
    run_id = m.swell_event.latest_run_id
    if links is None:
        links = attribution_links_by_run(db, [run_id]) if db is not None else {}
    offer = m.best_offer if m.status == MatchStatus.FLIGHT_FOUND else None
    return MatchSummary(
        id=m.id,
        search_id=m.search_id,
        search_name=m.search.name,
        search_status=m.search.status.value,
        status=m.status.value,
        status_reason=m.status_reason,
        spot=spot_mini(m.spot),
        event_id=m.swell_event_id,
        event_status=m.swell_event.status.value,
        window_start=m.window_start,
        window_end=m.window_end,
        qualifying_hours=m.qualifying_hours,
        peak_score=m.peak_score,
        avg_score=m.avg_score,
        peak_label=m.peak_label.value,
        breaking_height_min_ft=m.breaking_height_min_ft,
        breaking_height_max_ft=m.breaking_height_max_ft,
        confidence=m.confidence,
        confidence_label=m.confidence_label.value,
        destination_iata=m.destination_iata,
        transfer_minutes=m.transfer_minutes,
        origin_iata=m.origin_iata,
        recommended_arrival_date=m.recommended_arrival_date,
        recommended_departure_date=m.recommended_departure_date,
        best_price=m.best_price,
        currency=m.currency,
        overall_score=m.overall_score,
        scores=m.scores or {},
        is_demo=m.is_demo,
        has_mock_flights=bool(m.best_offer and m.best_offer.is_mock),
        flight_checked_at=m.flight_checked_at,
        notification_count=m.notification_count,
        lead_days=round((m.window_start - now).total_seconds() / 86400, 1),
        created_at=m.created_at,
        updated_at=m.updated_at,
        short_notice=is_short_notice(m.window_start, now),
        best_offer_expired=bool(
            offer and offer.offer_expires_at is not None and offer.offer_expires_at <= now
        ),
        quoted_currency=offer.currency
        if offer is not None and m.currency and offer.currency != m.currency
        else None,
        freshness=spot_freshness,
        attribution_links=list(links.get(run_id, [])) if run_id is not None else [],
        **event_confidence(m.swell_event, now),  # type: ignore[arg-type]
    )


def list_matches(
    db: Session,
    user: User,
    *,
    statuses: list[MatchStatus] | None = None,
    search_id: uuid.UUID | None = None,
    spot_id: int | None = None,
    include_past: bool = False,
    limit: int = 50,
) -> list[MatchSummary]:
    now = utcnow()
    stmt = select(OpportunityMatch).where(OpportunityMatch.user_id == user.id)
    if statuses:
        stmt = stmt.where(OpportunityMatch.status.in_(statuses))
    if search_id:
        stmt = stmt.where(OpportunityMatch.search_id == search_id)
    if spot_id:
        stmt = stmt.where(OpportunityMatch.spot_id == spot_id)
    if not include_past:
        stmt = stmt.where(OpportunityMatch.window_end >= now)
    # Trips with flights first; within them short-notice swells get SHORT_NOTICE_BOOST
    # points (ranking only, matching/scoring.py), so a good swell 3-7 days out leads while
    # the whole forecast horizon is still listed.
    settings = get_settings()
    short = OpportunityMatch.window_start.between(
        now + timedelta(days=settings.short_notice_min_days),
        now + timedelta(days=settings.short_notice_max_days),
    )
    boosted = OpportunityMatch.overall_score + case((short, SHORT_NOTICE_BOOST), else_=0)
    stmt = stmt.order_by(
        (OpportunityMatch.status != MatchStatus.FLIGHT_FOUND),
        boosted.desc(),
        OpportunityMatch.window_start,
    ).limit(limit)
    rows = db.scalars(stmt).unique().all()
    freshness = events_freshness(db, now) if rows else {}
    links = attribution_links_by_run(db, [m.swell_event.latest_run_id for m in rows])
    return [match_summary(m, now, freshness, links) for m in rows]


def _slice(offer: FlightOffer, direction: str) -> SliceOut:
    segs = [s for s in offer.segments if s["direction"] == direction]
    return SliceOut(
        origin=segs[0]["origin"],
        destination=segs[-1]["destination"],
        departure_at=segs[0]["departure_at"],
        arrival_at=segs[-1]["arrival_at"],
        departure_local=segs[0]["departure_local"],
        arrival_local=segs[-1]["arrival_local"],
        duration_minutes=offer.outbound_duration_minutes
        if direction == "outbound"
        else offer.inbound_duration_minutes,
        stops=offer.outbound_stops if direction == "outbound" else offer.inbound_stops,
        # Offers stored before a field existed (operating carrier name) leave it empty.
        segments=[SegmentOut(**{k: s[k] for k in SegmentOut.model_fields if k in s}) for s in segs],
    )


def route_links(
    origin: str, destination: str, depart: date, ret: date, adults: int
) -> list[LinkOut]:
    """Route searches for the same trip on third-party sites (fares there may differ)."""
    return [
        LinkOut(label=link["label"], url=link["url"], kind="route_search", note=link["note"])
        for link in inspection_links(origin, destination, depart, ret, adults)
    ]


def offer_out(
    offer: FlightOffer,
    rank: int | None = None,
    price_converted: object | None = None,
    *,
    search_currency: str | None = None,
    now: datetime | None = None,
) -> OfferOut:
    now = now or utcnow()
    depart = offer.outbound_departure_at.astimezone(ZoneInfo(offer.origin_timezone)).date()
    ret = offer.inbound_departure_at.astimezone(ZoneInfo(offer.destination_timezone)).date()
    links = route_links(
        offer.origin_iata, offer.destination_iata, depart, ret, offer.flight_search.adults
    )
    expired = offer.offer_expires_at is not None and offer.offer_expires_at <= now
    if offer.booking_url and not expired:
        # An expired quote is never offered as bookable: only the route searches remain.
        links.insert(
            0,
            LinkOut(
                label=f"Book with {provider_label(offer.provider)}",
                url=offer.booking_url,
                kind="booking",
            ),
        )
    converted = search_currency if search_currency and search_currency != offer.currency else None
    return OfferOut(
        id=offer.id,
        rank=rank,
        provider=offer.provider,
        provider_label=provider_label(offer.provider),
        is_mock=offer.is_mock,
        total_price=offer.total_price,
        price_per_traveler=offer.price_per_traveler,
        currency=offer.currency,
        price_converted=price_converted,  # type: ignore[arg-type]
        airlines=list(offer.airlines),
        airline_names=list(offer.airline_names),
        origin=offer.origin_iata,
        destination=offer.destination_iata,
        origin_timezone=offer.origin_timezone,
        destination_timezone=offer.destination_timezone,
        outbound=_slice(offer, "outbound"),
        inbound=_slice(offer, "inbound"),
        total_duration_minutes=offer.total_duration_minutes,
        baggage=offer.baggage,
        quoted_at=offer.quoted_at,
        last_validated_at=offer.last_validated_at,
        validation_status=offer.validation_status.value,
        offer_expires_at=offer.offer_expires_at,
        links=links,
        operating_carriers=operating_carrier_names(offer.segments or []),
        expired=expired,
        converted_currency=converted if price_converted is not None else None,
    )


def _peak_method(db: Session, event: SwellEvent) -> str | None:
    """The breaking-height method our prediction at the event's peak actually used."""
    if event.latest_run_id is None:
        return None
    return db.scalar(
        select(SurfQualityPrediction.breaking_height_method).where(
            SurfQualityPrediction.run_id == event.latest_run_id,
            SurfQualityPrediction.spot_id == event.spot_id,
            SurfQualityPrediction.valid_time == event.peak_time,
        )
    )


def limitations(
    m: OpportunityMatch, *, method: str | None = None, forecast_class: str | None = None
) -> list[str]:
    """Uncertainty notes for one opportunity. ``method`` is the stored breaking-height
    method of the peak prediction and ``forecast_class`` the event's forecast-confidence
    class (with the staleness cap); the low-confidence note follows that class, not the
    legacy 0-100 index."""
    how = METHOD_TEXT.get(method or "")
    notes = [
        "Breaking wave heights are approximate estimates derived from offshore model output"
        + (f" ({how})" if how else "")
        + ", not observations.",
        "Quality scores use a documented heuristic and are not statistically calibrated "
        "probabilities.",
        "Forecast skill drops with lead time; swells more than 5 days out often shift in size "
        "and timing.",
        "Fares are provider quotes at search time and can change or sell out before booking.",
    ]
    if m.is_demo:
        notes.insert(
            0,
            "DEMO DATA: this forecast comes from the synthetic demo provider, not a real model.",
        )
    if m.best_offer and m.best_offer.is_mock:
        notes.insert(
            0, "MOCK FARES: flight offers come from the demo provider and are not real prices."
        )
    if forecast_class == "low":
        notes.append("Forecast confidence is low for this window; treat it as an early heads-up.")
    return notes


def match_detail(db: Session, m: OpportunityMatch) -> MatchDetail:
    now = utcnow()
    fresh = served_freshness(db, m.spot_id, now)
    freshness = {m.spot_id: fresh} if fresh is not None else {}
    summary = match_summary(m, now, freshness)
    event: SwellEvent = m.swell_event
    travel = m.search.travel
    daily = []
    if event.latest_run_id is not None:
        start = m.window_start - timedelta(days=travel.arrival_buffer_days + 1)
        end = m.window_end + timedelta(days=travel.departure_buffer_days + 1)
        daily = daily_summaries(
            forecast_points(
                db, event.latest_run_id, m.spot_id, max(start, now - timedelta(hours=3)), end
            ),
            m.spot.timezone,
            m.spot.latitude,
            m.spot.longitude,
        )
    offers = [
        offer_out(o.offer, o.rank, o.price_converted, search_currency=travel.currency, now=now)
        for o in m.offers
    ]
    window_out = None
    links: list[LinkOut] = []
    if m.destination_iata:
        airport = next((a for a in m.spot.airports if a.airport_iata == m.destination_iata), None)
        if airport is not None:
            try:
                tw = compute_travel_window(
                    surf_start=m.window_start,
                    surf_end=m.window_end,
                    spot_tz=m.spot.timezone,
                    destination_iata=m.destination_iata,
                    destination_tz=airport.airport.timezone,
                    transfer_minutes=airport.transfer_minutes,
                    arrival_buffer_days=travel.arrival_buffer_days,
                    departure_buffer_days=travel.departure_buffer_days,
                    min_days_at_destination=travel.min_days_at_destination,
                    max_trip_days=travel.max_trip_days,
                    now=now,
                )
                window_out = TravelWindowOut(
                    arrive_after=tw.arrive_after,
                    arrive_by=tw.arrive_by,
                    depart_after=tw.depart_after,
                    depart_before=tw.depart_before,
                    destination_timezone=tw.destination_tz,
                    nights=tw.nights,
                )
            except InfeasibleWindow:
                window_out = None
        if m.recommended_arrival_date and m.recommended_departure_date:
            origin = m.origin_iata or (
                m.search.origins[0].airport_iata if m.search.origins else None
            )
            if origin:
                links = route_links(
                    origin,
                    m.destination_iata,
                    m.recommended_arrival_date - timedelta(days=1),
                    m.recommended_departure_date,
                    travel.travelers,
                )
    return MatchDetail(
        **summary.model_dump(),
        event=event_out(event, now, include_history=True, freshness=freshness),
        spot_airports=[
            SpotAirportOut(
                airport=AirportOut.model_validate(a.airport),
                is_primary=a.is_primary,
                transfer_minutes=a.transfer_minutes,
                transfer_mode=a.transfer_mode,
                notes=a.notes,
            )
            for a in m.spot.airports
        ],
        accessibility_notes=m.spot.accessibility_notes,
        daily=daily,
        offers=offers,
        travel_window=window_out,
        inspection_links=links,
        limitations=limitations(
            m,
            method=_peak_method(db, event),
            forecast_class=summary.forecast_confidence_class,
        ),
        flight_search_error=m.flight_search_error,
    )


def mask_recipient(value: str) -> str:
    if "@" in value:
        name, domain = value.split("@", 1)
        return (name[:1] + "***@" + domain) if name else "***@" + domain
    if len(value) > 4:
        return value[:2] + "*" * (len(value) - 6) + value[-4:]
    return "****"


def notification_out(n: Notification, now: datetime | None = None) -> NotificationOut:
    """One alert. Its confidence is the alerted event's current peak-hour confidence (as
    EventOut and the opportunity show it, with the staleness cap); none once the match is
    gone. The email itself keeps the classes at sending time."""
    snap = n.snapshot or {}
    match = n.match
    confidence: dict[str, object] = {}
    if match is not None:
        confidence = event_confidence(match.swell_event, now or utcnow())
        confidence.pop("stale")
    return NotificationOut(
        id=n.id,
        channel=n.channel.value,
        kind=n.kind.value,
        status=n.status.value,
        subject=n.subject,
        recipient_masked=mask_recipient(n.recipient),
        created_at=n.created_at,
        sent_at=n.sent_at,
        delivered_at=n.delivered_at,
        provider=n.provider,
        attempts=n.attempts,
        last_error=n.last_error,
        match_id=n.match_id,
        match_status=match.status.value if match else None,
        event_status=match.swell_event.status.value if match else None,
        spot_name=snap.get("spot_name"),
        spot_slug=snap.get("spot_slug"),
        price=snap.get("price"),
        currency=snap.get("currency"),
        peak_label=snap.get("peak_label"),
        window_label=snap.get("window_label"),
        is_demo=bool(snap.get("is_demo_forecast") or snap.get("is_mock_flight")),
        **confidence,  # type: ignore[arg-type]
    )


def list_notifications(
    db: Session, user: User, limit: int = 50, offset: int = 0
) -> tuple[int, list[NotificationOut]]:
    total = (
        db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id)) or 0
    )
    rows = db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    now = utcnow()
    return int(total), [notification_out(n, now) for n in rows]
