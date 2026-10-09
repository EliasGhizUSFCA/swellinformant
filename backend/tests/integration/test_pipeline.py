"""End-to-end pipeline: forecasts → predictions → events → matches → flights → alerts."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import utcnow
from app.models import (
    FlightOffer,
    ForecastRun,
    Notification,
    NotificationPreference,
    OpportunityMatch,
    SpotAirport,
    SurfQualityPrediction,
    SurfSpot,
    SwellEvent,
    User,
)
from app.models.enums import (
    ForecastRunStatus,
    MatchStatus,
    NotificationKind,
    NotificationStatus,
    SwellEventStatus,
)
from app.schemas.searches import SearchIn
from app.services import accounts, searches
from app.services.flights.demo import DemoFlightProvider
from app.services.forecasts.demo import DemoForecastProvider
from app.services.forecasts.ingestion import acquire_all, simulate_swell
from app.services.forecasts.queries import is_stale, latest_runs_by_spot
from app.services.http import PermanentProviderError, TransientProviderError
from app.services.matching.service import match_all
from app.services.notifications.alerts import generate_alerts
from app.services.notifications.delivery import deliver_pending
from app.services.notifications.providers import ConsoleEmailProvider
from app.services.pipeline import run_pipeline
from app.services.swell_detection.service import detect_events
from tests.conftest import search_payload

pytestmark = pytest.mark.integration


def make_user(db: Session, email: str = "surfer@example.com", verified: bool = True) -> User:
    user = accounts.register(db, "Test Surfer", email, "Barrels4Days!")
    if verified:
        user.email_verified_at = utcnow()
        db.commit()
    return user


def make_search(db: Session, user: User, **overrides: object):  # type: ignore[no-untyped-def]
    return searches.create_search(db, user, SearchIn.model_validate(search_payload(**overrides)))


def simulate(db: Session, slug: str = "jeffreys-bay", days: float = 7.0, hours: int = 60):  # type: ignore[no-untyped-def]
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    start = (utcnow() + timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    outcome = simulate_swell(db, spot, start=start, duration_hours=hours)  # type: ignore[arg-type]
    assert outcome.status == "success"
    result = run_pipeline(db, acquire=False)
    db.expire_all()
    return start, start + timedelta(hours=hours), result


def sim_match(db: Session, start, end) -> OpportunityMatch:  # type: ignore[no-untyped-def]
    rows = db.scalars(
        select(OpportunityMatch).join(SurfSpot).where(SurfSpot.slug == "jeffreys-bay")
    ).all()
    hits = [m for m in rows if m.window_start < end and m.window_end > start]
    assert hits, [(m.window_start, m.window_end, m.status) for m in rows]
    return hits[0]


def alert_count(db: Session, **filters: object) -> int:
    stmt = select(func.count(Notification.id))
    for k, v in filters.items():
        stmt = stmt.where(getattr(Notification, k) == v)
    return int(db.scalar(stmt) or 0)


# ---------------------------------------------------------------- happy path
def test_qualifying_swell_produces_flight_opportunity_and_alert(
    db: Session, few_spots: list[str]
) -> None:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db)
    start, end, result = simulate(db)

    event = db.scalar(
        select(SwellEvent)
        .join(SurfSpot)
        .where(
            SurfSpot.slug == "jeffreys-bay",
            SwellEvent.status == SwellEventStatus.ACTIVE,
            SwellEvent.start_time < end,
            SwellEvent.end_time > start,
        )
    )
    assert event is not None and event.is_demo and event.peak_score >= 72
    assert event.peak_breaking_height_min_ft < event.peak_breaking_height_max_ft

    match = sim_match(db, start, end)
    assert match.status == MatchStatus.FLIGHT_FOUND, match.status_reason
    assert match.best_offer is not None and match.best_offer.is_mock
    assert match.best_price is not None and match.best_price <= Decimal("5000")
    assert match.origin_iata == "SFO" and match.destination_iata == "PLZ"
    zone = ZoneInfo("Africa/Johannesburg")
    assert match.recommended_arrival_date == match.window_start.astimezone(zone).date() - timedelta(
        days=2
    )
    assert match.recommended_departure_date >= (match.window_end - timedelta(minutes=1)).astimezone(
        zone
    ).date() + timedelta(days=1)
    # the offer actually lands before the deadline and leaves after the swell
    offer = match.best_offer
    assert offer.outbound_arrival_at <= match.window_start
    assert offer.inbound_departure_at >= match.window_end
    assert set(match.scores) == {"surf", "confidence", "affordability", "convenience"}

    sent = db.scalars(select(Notification).where(Notification.match_id == match.id)).all()
    assert sent and all(n.status == NotificationStatus.SENT for n in sent)
    assert sent[0].kind == NotificationKind.NEW_OPPORTUNITY
    assert "[DEMO]" in sent[0].subject and "Jeffreys Bay" in sent[0].subject
    assert sent[0].snapshot["price"] is not None

    # Re-running the whole pipeline never duplicates the alert.
    before = alert_count(db)
    run_pipeline(db)
    run_pipeline(db, acquire=False)
    assert alert_count(db) == before


def test_paused_search_does_not_generate_notifications(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    search = make_search(db, user)
    searches.set_paused(db, search, True)
    run_pipeline(db)
    simulate(db)
    assert alert_count(db) == 0

    searches.set_paused(db, search, False)
    run_pipeline(db, acquire=False)
    assert alert_count(db, user_id=user.id) >= 1


def test_pausing_withdraws_queued_alerts(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    search = make_search(db, user)
    run_pipeline(db, deliver=False)
    start, end, _ = (lambda r: r)(simulate_without_delivery(db))
    queued = db.scalars(
        select(Notification).where(Notification.status == NotificationStatus.QUEUED)
    ).all()
    assert queued
    searches.set_paused(db, search, True)
    db.expire_all()
    assert all(
        n.status == NotificationStatus.SKIPPED for n in db.scalars(select(Notification)).all()
    )


def simulate_without_delivery(db: Session):  # type: ignore[no-untyped-def]
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "jeffreys-bay"))
    start = (utcnow() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
    simulate_swell(db, spot, start=start, duration_hours=60)  # type: ignore[arg-type]
    run_pipeline(db, acquire=False, deliver=False)
    db.expire_all()
    return start, start + timedelta(hours=60), None


def test_overlapping_searches_send_one_alert(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user, name="A")
    make_search(db, user, name="B (same criteria)")
    run_pipeline(db)
    start, end, _ = simulate(db)
    matches = [
        m
        for m in db.scalars(select(OpportunityMatch)).all()
        if m.window_start < end and m.window_end > start
    ]
    assert len(matches) == 2
    alerts = db.scalars(select(Notification).where(Notification.channel == "email")).all()
    per_event = {(n.snapshot["event_id"], n.kind) for n in alerts}
    assert len(alerts) == len(per_event)  # one alert per swell event, not per search


# ---------------------------------------------------------------- surf-only outcomes
def test_budget_too_low_gives_surf_only_without_implying_flights(
    db: Session, few_spots: list[str]
) -> None:
    user = make_user(db)
    make_search(db, user, travel={"max_price": 60})
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    assert match.status == MatchStatus.SURF_ONLY
    assert "over budget" in (match.status_reason or "")
    assert match.best_offer_id is None and match.best_price is None
    assert alert_count(db) == 0  # surf-only alerts are opt-in


def test_surf_only_alert_when_opted_in(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user, travel={"max_price": 60}, notify_surf_only=True)
    run_pipeline(db)
    start, end, _ = simulate(db)
    alert = db.scalars(
        select(Notification).where(Notification.kind == NotificationKind.SURF_ONLY)
    ).first()
    assert alert is not None and "No flight within your budget" in alert.body_text
    assert alert.snapshot["price"] is None


def test_destination_without_practical_airport(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user, max_transfer_minutes=5)
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    assert match.status == MatchStatus.SURF_ONLY
    assert "no practical airport within your transfer limit" in (match.status_reason or "")

    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "jeffreys-bay"))
    db.execute(delete(SpotAirport).where(SpotAirport.spot_id == spot.id))  # type: ignore[union-attr]
    db.commit()
    try:
        db.expire_all()  # the raw DELETE bypassed the ORM; reload spot.airports
        match_all(db)
        db.expire_all()
        assert "no practical airport is configured" in (
            db.get(OpportunityMatch, match.id).status_reason or ""
        )  # type: ignore[union-attr]
    finally:
        from app.services.seed import seed_reference_data

        seed_reference_data(db)  # restore reference data for later tests


def test_fixed_dates_outside_swell_do_not_match(db: Session, few_spots: list[str]) -> None:
    from datetime import date

    user = make_user(db)
    far = date.today() + timedelta(days=60)
    make_search(
        db,
        user,
        date_mode="fixed",
        date_start=far.isoformat(),
        date_end=(far + timedelta(days=10)).isoformat(),
    )
    run_pipeline(db)
    simulate(db)
    assert db.scalar(select(func.count(OpportunityMatch.id))) == 0


# ---------------------------------------------------------------- provider failures
def test_transient_flight_failure_keeps_match_pending(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(self, query):  # type: ignore[no-untyped-def]
        raise TransientProviderError("demo provider timed out")

    monkeypatch.setattr(DemoFlightProvider, "search", down)
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    assert match.status == MatchStatus.PENDING_FLIGHTS
    assert "temporary provider error" in (match.flight_search_error or "")
    assert alert_count(db) == 0


def test_permanent_flight_failure_is_reported(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(self, query):  # type: ignore[no-untyped-def]
        raise PermanentProviderError("route not supported")

    monkeypatch.setattr(DemoFlightProvider, "search", unsupported)
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    assert match.status == MatchStatus.SURF_ONLY
    assert "route not supported" in (match.status_reason or "")


def test_expired_offer_is_never_alerted_and_triggers_research(
    db: Session, few_spots: list[str]
) -> None:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db, deliver=False)
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "jeffreys-bay"))
    start = (utcnow() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
    simulate_swell(db, spot, start=start, duration_hours=60)  # type: ignore[arg-type]
    # Stop after flight discovery: expire every offer before alerts are generated.
    from app.services.flights.service import discover_flights
    from app.services.surf_quality.predict import compute_pending_predictions

    db.execute(delete(Notification))
    compute_pending_predictions(db)
    detect_events(db)
    match_all(db)
    discover_flights(db)
    db.execute(update(FlightOffer).values(offer_expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    stats = generate_alerts(db)
    assert stats.invalidated >= 1 and stats.queued == 0
    db.expire_all()
    match = sim_match(db, start, start + timedelta(hours=60))
    assert match.status == MatchStatus.PENDING_FLIGHTS
    discover_flights(db)  # cache was invalidated → fresh offers
    db.expire_all()
    assert db.get(OpportunityMatch, match.id).status == MatchStatus.FLIGHT_FOUND  # type: ignore[union-attr]


# ---------------------------------------------------------------- notification preferences
def test_dashboard_only_and_unverified_users_get_no_messages(
    db: Session, few_spots: list[str]
) -> None:
    dash = make_user(db, "dash@example.com")
    make_search(db, dash, notification_channel="dashboard")
    unverified = make_user(db, "new@example.com", verified=False)
    make_search(db, unverified)
    run_pipeline(db)
    start, end, _ = simulate(db)
    assert alert_count(db) == 0
    assert (
        db.scalar(
            select(func.count(OpportunityMatch.id)).where(
                OpportunityMatch.status == MatchStatus.FLIGHT_FOUND
            )
        )
        >= 2
    )


def test_daily_alert_cap(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    prefs = db.get(NotificationPreference, user.id)
    prefs.max_alerts_per_day = 1  # type: ignore[union-attr]
    db.commit()
    make_search(db, user, destination_mode="all")
    run_pipeline(db)
    simulate(db)
    simulate(db, slug="uluwatu")
    assert alert_count(db, user_id=user.id) <= 1


def test_price_drop_triggers_follow_up(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    snap = dict(match.last_notified_snapshot or {})
    snap["price"] = str((match.best_price or Decimal(0)) + Decimal("400"))
    match.last_notified_snapshot = snap
    db.commit()
    stats = generate_alerts(db)
    assert stats.queued >= 1
    assert (
        db.scalar(
            select(func.count(Notification.id)).where(
                Notification.kind == NotificationKind.PRICE_DROP
            )
        )
        >= 1
    )


# ---------------------------------------------------------------- forecasts and events
def test_forecast_ingestion_is_idempotent(db: Session, few_spots: list[str]) -> None:
    first = acquire_all(db)
    second = acquire_all(db)
    assert first[0].status == "success" and first[0].records == 5 * 12 * 24
    assert second[0].status == "skipped"
    assert db.scalar(select(func.count(ForecastRun.id))) == 1


def test_failed_forecast_provider_is_recorded(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self, spots, run):  # type: ignore[no-untyped-def]
        raise PermanentProviderError("upstream returned HTTP 500 for every request")

    monkeypatch.setattr(DemoForecastProvider, "fetch", broken)
    outcome = acquire_all(db)[0]
    assert outcome.status == "failed"
    run = db.scalar(select(ForecastRun))
    assert run.status == ForecastRunStatus.FAILED and "HTTP 500" in run.error_message  # type: ignore[union-attr, operator]
    # pipeline stages keep working with no data
    result = run_pipeline(db, acquire=False)
    assert result["detection"]["spots"] == 0


def test_missing_forecast_data_for_some_spots(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch, app_client
) -> None:  # type: ignore[no-untyped-def]
    original = DemoForecastProvider.fetch

    def partial(self, spots, run):  # type: ignore[no-untyped-def]
        result = original(self, spots, run)
        nazare = next(s.spot_id for s in spots if s.slug == "nazare")
        result.records.pop(nazare)
        result.errors[nazare] = "no wave data at forecast point"
        return result

    monkeypatch.setattr(DemoForecastProvider, "fetch", partial)
    outcome = acquire_all(db)[0]
    assert outcome.status == "partial" and outcome.spot_errors == 1
    run_pipeline(db, acquire=False)
    forecast = app_client.get("/api/spots/nazare/forecast").json()
    assert forecast["available"] is False and forecast["message"]
    assert app_client.get("/api/spots/pipeline/forecast").json()["available"] is True


def test_stale_forecasts_are_flagged_and_never_alerted(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db, deliver=False)
    start, end, _ = simulate_without_delivery(db)
    db.execute(delete(Notification))
    db.execute(update(ForecastRun).values(issued_at=utcnow() - timedelta(days=3)))
    for m in db.scalars(select(OpportunityMatch)).all():
        m.last_notified_snapshot = None
    db.commit()
    run = db.scalar(select(ForecastRun))
    assert is_stale(run)  # type: ignore[arg-type]
    stats = generate_alerts(db)
    assert stats.queued == 0 and stats.stale_forecast >= 1


def test_events_update_in_place_and_never_duplicate(db: Session, few_spots: list[str]) -> None:
    run_pipeline(db)
    start, end, _ = simulate(db)
    first = db.scalar(
        select(SwellEvent)
        .join(SurfSpot)
        .where(
            SurfSpot.slug == "jeffreys-bay",
            SwellEvent.status == SwellEventStatus.ACTIVE,
            SwellEvent.start_time < end,
            SwellEvent.end_time > start,
        )
    )
    assert first is not None
    simulate(db)  # a newer model run of the same swell
    db.expire_all()
    same = db.get(SwellEvent, first.id)
    assert same.status == SwellEventStatus.ACTIVE and same.version >= 2  # type: ignore[union-attr]
    assert len(same.history) >= 2  # type: ignore[union-attr]
    actives = db.scalars(
        select(SwellEvent).where(
            SwellEvent.spot_id == first.spot_id, SwellEvent.status == SwellEventStatus.ACTIVE
        )
    ).all()
    for a in actives:
        for b in actives:
            if a.id != b.id:
                assert not (a.start_time <= b.end_time and b.start_time <= a.end_time)

    # The database itself refuses an overlapping duplicate active event.
    db.execute(
        text(
            "INSERT INTO swell_events (id, spot_id, status, start_time, end_time, peak_time, peak_score, avg_score, "
            "peak_label, peak_breaking_height_min_ft, peak_breaking_height_max_ft, peak_wind_relation, qualifying_hours, "
            "confidence, confidence_label, source_code, is_demo, first_detected_at, last_updated_at, version, history) "
            "SELECT gen_random_uuid(), spot_id, 'active', start_time, end_time, peak_time, peak_score, avg_score, peak_label, "
            "peak_breaking_height_min_ft, peak_breaking_height_max_ft, peak_wind_relation, qualifying_hours, confidence, "
            "confidence_label, source_code, is_demo, now(), now(), 1, '[]' FROM swell_events WHERE id = :id"
        ),
        {"id": first.id},
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_downgraded_forecast_expires_matches(db: Session, few_spots: list[str]) -> None:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db)
    start, end, _ = simulate(db)
    match = sim_match(db, start, end)
    run_id = latest_runs_by_spot(db)[match.spot_id].id
    db.execute(
        update(SurfQualityPrediction)
        .where(
            SurfQualityPrediction.run_id == run_id, SurfQualityPrediction.spot_id == match.spot_id
        )
        .values(score=5)
    )
    db.commit()
    detect_events(db)
    match_all(db)
    db.expire_all()
    assert db.get(SwellEvent, match.swell_event_id).status == SwellEventStatus.DOWNGRADED  # type: ignore[union-attr]
    assert db.get(OpportunityMatch, match.id).status == MatchStatus.EXPIRED  # type: ignore[union-attr]


# ---------------------------------------------------------------- delivery
def _queue_one(db: Session) -> Notification:
    user = make_user(db)
    make_search(db, user)
    run_pipeline(db, deliver=False)
    simulate_without_delivery(db)
    n = db.scalars(
        select(Notification).where(Notification.status == NotificationStatus.QUEUED)
    ).first()
    assert n is not None
    return n


def test_transient_delivery_failures_are_retried_then_fail(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    n = _queue_one(db)

    def flaky(self, message):  # type: ignore[no-untyped-def]
        raise TransientProviderError("SMTP 421 try again later")

    monkeypatch.setattr(ConsoleEmailProvider, "send", flaky)
    stats = deliver_pending(db)
    assert stats.retried >= 1
    db.refresh(n)
    assert (
        n.status == NotificationStatus.QUEUED and n.attempts == 1 and n.next_attempt_at > utcnow()
    )
    for _ in range(get_settings().notification_max_attempts):
        db.execute(update(Notification).values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        deliver_pending(db)
    db.refresh(n)
    assert n.status == NotificationStatus.FAILED and "421" in (n.last_error or "")


def test_unavailable_sms_service_is_reported(
    db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "sms_provider", "twilio")  # Twilio without credentials
    user = make_user(db)
    prefs = db.get(NotificationPreference, user.id)
    now = utcnow()
    prefs.phone_number, prefs.phone_verified_at, prefs.sms_opt_in_at, prefs.sms_enabled = (
        "+14155550123",
        now,
        now,
        True,
    )  # type: ignore[union-attr]
    db.commit()
    make_search(db, user, notification_channel="sms")
    run_pipeline(db)
    simulate(db)
    sms = db.scalars(select(Notification).where(Notification.channel == "sms")).all()
    assert sms and all(n.status == NotificationStatus.FAILED for n in sms)
    assert "SMS service unavailable" in (sms[0].last_error or "")


def test_preferences_are_rechecked_at_send_time(db: Session, few_spots: list[str]) -> None:
    n = _queue_one(db)
    prefs = db.get(NotificationPreference, n.user_id)
    prefs.all_paused = True  # type: ignore[union-attr]
    db.commit()
    deliver_pending(db)
    db.refresh(n)
    assert n.status == NotificationStatus.SKIPPED


def test_worker_crash_mid_send_is_recovered(db: Session, few_spots: list[str]) -> None:
    n = _queue_one(db)
    # Simulate a worker that claimed the notification and died.
    db.execute(
        update(Notification)
        .where(Notification.id == n.id)
        .values(status=NotificationStatus.SENDING, updated_at=utcnow() - timedelta(hours=1))
    )
    db.commit()
    stats = deliver_pending(db)
    db.refresh(n)
    assert stats.reclaimed == 1 and n.status == NotificationStatus.SENT


def test_interrupted_forecast_run_is_retried(db: Session, few_spots: list[str]) -> None:
    acquire_all(db)
    db.execute(
        update(ForecastRun).values(
            status=ForecastRunStatus.RUNNING, started_at=utcnow() - timedelta(hours=2)
        )
    )
    db.commit()
    outcome = acquire_all(db)[0]
    assert outcome.status == "success"
    assert db.scalar(select(func.count(ForecastRun.id))) == 1
