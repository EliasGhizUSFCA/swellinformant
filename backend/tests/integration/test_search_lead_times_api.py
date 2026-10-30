"""Saved searches with per-search alert lead times, five origins and flight preferences
(spec 26/28), and the delivery details of spec 29 (idempotency key, unsubscribe)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import EmailMessage, Notification, NotificationPreference
from app.models.enums import EmailPurpose, NotificationStatus
from app.services.notifications import delivery, transactional
from app.services.notifications.providers import EmailMessageData, SendResult
from app.services.pipeline import run_pipeline
from tests.conftest import ApiClient, search_payload

pytestmark = pytest.mark.integration

FIVE = ["SYD", "MEL", "BNE", "PER", "OOL"]


def test_create_edit_delete_with_lead_times_five_origins_and_flight_preferences(
    app_client: ApiClient,
) -> None:
    app_client.register("planner@example.com")
    payload = search_payload(
        name="Bali at short notice",
        destinations={"spot_slugs": ["uluwatu"]},
        origins=FIVE,
        alert_lead_min_days=1,
        alert_lead_max_days=7,
        travel={
            "max_layovers": 3,
            "travelers": 2,
            "cabin_class": "premium_economy",
            "preferred_airlines": ["qf", "JQ"],
            "currency": "AUD",
            "max_price": 1400,
            "arrival_buffer_days": 1,
            "departure_buffer_days": 0,
        },
    )
    created = app_client.post("/api/searches", payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["origins"] == FIVE  # all five kept, in order
    assert (body["alert_lead_min_days"], body["alert_lead_max_days"]) == (1, 7)
    travel = body["travel"]
    assert travel["max_layovers"] == 3 and travel["travelers"] == 2
    assert travel["cabin_class"] == "premium_economy" and travel["currency"] == "AUD"
    assert travel["preferred_airlines"] == ["JQ", "QF"]
    assert (travel["arrival_buffer_days"], travel["departure_buffer_days"]) == (1, 0)
    sid = body["id"]
    stored = app_client.get(f"/api/searches/{sid}").json()  # persisted as returned
    for key in ("origins", "alert_lead_min_days", "alert_lead_max_days", "destinations"):
        assert stored[key] == body[key], key
    assert {k: v for k, v in stored["travel"].items() if k != "max_price"} == {
        k: v for k, v in travel.items() if k != "max_price"
    }

    # Editing without the lead fields keeps them; with them, they change.
    edit = {**payload, "name": "Bali, renamed"}
    del edit["alert_lead_min_days"], edit["alert_lead_max_days"]
    kept = app_client.put(f"/api/searches/{sid}", edit).json()
    assert kept["name"] == "Bali, renamed"
    assert (kept["alert_lead_min_days"], kept["alert_lead_max_days"]) == (1, 7)
    changed = app_client.put(
        f"/api/searches/{sid}", {**edit, "alert_lead_min_days": 3, "alert_lead_max_days": 5}
    ).json()
    assert (changed["alert_lead_min_days"], changed["alert_lead_max_days"]) == (3, 5)
    copy = app_client.post(f"/api/searches/{sid}/duplicate").json()
    assert (copy["alert_lead_min_days"], copy["alert_lead_max_days"]) == (3, 5)
    assert copy["origins"] == FIVE

    assert app_client.delete(f"/api/searches/{sid}").status_code == 200
    assert app_client.get(f"/api/searches/{sid}").status_code == 404


def test_lead_times_are_validated_and_default_from_the_settings(
    app_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_client.register("validator@example.com")
    for bad in (
        {"alert_lead_min_days": 6, "alert_lead_max_days": 3},  # earliest after latest
        {"alert_lead_min_days": -1},
        {"alert_lead_max_days": 17},  # the database CHECK allows 0-16
        {"origins": [*FIVE, "SFO"]},  # six origins
        {"travel": {"max_layovers": 4}},
    ):
        r = app_client.post("/api/searches", search_payload(**bad))
        assert r.status_code == 422, (bad, r.text)
    # Only one end given: the other comes from the defaults, and the pair must still hold.
    r = app_client.post("/api/searches", search_payload(alert_lead_min_days=12))
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_lead_time"

    monkeypatch.setattr(get_settings(), "alert_lead_default_min_days", 3)
    monkeypatch.setattr(get_settings(), "alert_lead_default_max_days", 8)
    plain = app_client.post("/api/searches", search_payload()).json()
    assert (plain["alert_lead_min_days"], plain["alert_lead_max_days"]) == (3, 8)
    defaults = app_client.get("/api/searches/defaults").json()
    assert defaults["alert_lead_min_days"] == 3 and defaults["alert_lead_max_days"] == 8
    assert defaults["max_origins"] == 5 and defaults["max_layovers"] == 3
    assert defaults["max_alert_lead_days"] == 16
    assert (defaults["short_notice_min_days"], defaults["short_notice_max_days"]) == (3.0, 7.0)
    assert defaults["forecast_days"] == get_settings().forecast_days
    assert defaults["flight_searches_per_day"] == get_settings().flight_user_daily_quota


def test_the_defaults_need_a_session(app_client: ApiClient) -> None:
    assert app_client.get("/api/searches/defaults").status_code == 401


# --------------------------------------------------------------------------- delivery
class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[EmailMessageData] = []

    def send(self, message: EmailMessageData) -> SendResult:
        self.sent.append(message)
        return SendResult(self.name, f"msg-{len(self.sent)}")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def test_alerts_carry_an_idempotency_key_and_unsubscribe_still_works(
    db: Session,
    few_spots: list[str],
    only_simulated_swells: None,
    make_client: Callable[[], ApiClient],
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    client.register("alerts@example.com")
    monkeypatch.setattr(delivery, "build_email_provider", lambda: recorder)
    assert client.post("/api/searches", search_payload()).status_code == 201
    run_pipeline(db)
    sim = client.post("/api/dev/simulate-swell", {"spot_slug": "jeffreys-bay", "days_ahead": 7})
    assert sim.status_code == 200, sim.text
    delivery.deliver_pending(db)
    alerts = [m for m in recorder.sent if m.tag == "alert"]
    assert alerts, "an alert was delivered"
    sent = db.scalars(
        select(Notification).where(Notification.status == NotificationStatus.ACCEPTED)
    ).all()
    # One key per notification: a retry or a reclaimed send repeats it (Resend dedupes).
    assert sorted(m.idempotency_key or "" for m in alerts) == sorted(
        f"notification-{n.id}" for n in sent
    )
    message = alerts[0]
    assert message.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/api/notifications/unsubscribe?token=" in message.headers["List-Unsubscribe"]
    assert "Unsubscribe from email alerts:" in message.text
    # One-click unsubscribe (RFC 8058) still works and turns email alerts off.
    with SessionLocal() as s:
        token = s.scalar(select(NotificationPreference.unsubscribe_token))
    r = client.client.post(
        f"/api/notifications/unsubscribe?token={token}", data={"List-Unsubscribe": "One-Click"}
    )
    assert r.status_code == 200
    assert client.get("/api/auth/me").json()["notification_preferences"]["email_enabled"] is False


def test_account_emails_carry_their_own_idempotency_key(
    db: Session, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import accounts

    monkeypatch.setattr(transactional, "build_email_provider", lambda: recorder)
    user = accounts.register(db, "Key Holder", "keys@example.com", "Barrels4Days!")
    rows: list[Any] = db.scalars(
        select(EmailMessage).where(
            EmailMessage.user_id == user.id, EmailMessage.purpose == EmailPurpose.VERIFY_EMAIL
        )
    ).all()
    assert rows and recorder.sent
    assert recorder.sent[-1].idempotency_key == f"account-email-{rows[-1].id}"
    # A redelivered task for the same row is never sent again (and would reuse the key).
    transactional.deliver_account_email(rows[-1].id, "text", None)
    assert len(recorder.sent) == 1
