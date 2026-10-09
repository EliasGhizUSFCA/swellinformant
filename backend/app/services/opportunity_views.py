"""Read models for opportunities (matches) and notification history."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.core.errors import NotFound
from app.models import FlightOffer, Notification, OpportunityMatch, SwellEvent, User
from app.models.enums import MatchStatus
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
from app.schemas.spots import AirportOut, SpotAirportOut
from app.services.flights.links import inspection_links
from app.services.flights.registry import provider_label
from app.services.flights.windows import InfeasibleWindow, compute_travel_window
from app.services.spot_views import daily_summaries, event_out, forecast_points, spot_mini


def get_owned_match(db: Session, user: User, match_id: uuid.UUID) -> OpportunityMatch:
    match = db.get(OpportunityMatch, match_id)
    if match is None or match.user_id != user.id:
        raise NotFound("Opportunity not found.")
    return match


def match_summary(m: OpportunityMatch, now: datetime | None = None) -> MatchSummary:
    now = now or utcnow()
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
    stmt = stmt.order_by(
        (OpportunityMatch.status != MatchStatus.FLIGHT_FOUND),
        OpportunityMatch.overall_score.desc(),
        OpportunityMatch.window_start,
    ).limit(limit)
    return [match_summary(m, now) for m in db.scalars(stmt).unique().all()]


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
        segments=[SegmentOut(**{k: s[k] for k in SegmentOut.model_fields}) for s in segs],
    )


def offer_out(
    offer: FlightOffer, rank: int | None = None, price_converted: object | None = None
) -> OfferOut:
    depart = offer.outbound_departure_at.astimezone(ZoneInfo(offer.origin_timezone)).date()
    ret = offer.inbound_departure_at.astimezone(ZoneInfo(offer.destination_timezone)).date()
    links = [
        LinkOut(**link)
        for link in inspection_links(
            offer.origin_iata, offer.destination_iata, depart, ret, offer.flight_search.adults
        )
    ]
    if offer.booking_url:
        links.insert(
            0, LinkOut(label=f"Book with {provider_label(offer.provider)}", url=offer.booking_url)
        )
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
    )


def limitations(m: OpportunityMatch) -> list[str]:
    notes = [
        "Breaking wave heights are approximate estimates derived from offshore model output "
        "(Komar–Gaughan shoaling with uncalibrated spot factors), not observations.",
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
    if m.confidence_label.value == "low":
        notes.append("Forecast confidence is low for this window; treat it as an early heads-up.")
    return notes


def match_detail(db: Session, m: OpportunityMatch) -> MatchDetail:
    now = utcnow()
    summary = match_summary(m, now)
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
        )
    offers = [offer_out(o.offer, o.rank, o.price_converted) for o in m.offers]
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
                links = [
                    LinkOut(**link)
                    for link in inspection_links(
                        origin,
                        m.destination_iata,
                        m.recommended_arrival_date - timedelta(days=1),
                        m.recommended_departure_date,
                        travel.travelers,
                    )
                ]
    return MatchDetail(
        **summary.model_dump(),
        event=event_out(event, now, include_history=True),
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
        limitations=limitations(m),
        flight_search_error=m.flight_search_error,
    )


def mask_recipient(value: str) -> str:
    if "@" in value:
        name, domain = value.split("@", 1)
        return (name[:1] + "***@" + domain) if name else "***@" + domain
    if len(value) > 4:
        return value[:2] + "*" * (len(value) - 6) + value[-4:]
    return "****"


def notification_out(n: Notification) -> NotificationOut:
    snap = n.snapshot or {}
    match = n.match
    return NotificationOut(
        id=n.id,
        channel=n.channel.value,
        kind=n.kind.value,
        status=n.status.value,
        subject=n.subject,
        recipient_masked=mask_recipient(n.recipient),
        created_at=n.created_at,
        sent_at=n.sent_at,
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
    return int(total), [notification_out(n) for n in rows]
