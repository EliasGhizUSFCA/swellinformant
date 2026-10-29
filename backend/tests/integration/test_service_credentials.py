"""Admin alert when the flight or email provider's credentials expire (spec 16; review
r1-spec-SPEC-7).

The providers answer through httpx.MockTransport (no request leaves the test): Duffel and
Resend refuse the key with HTTP 401, then accept it again. Accounts are SYNTHETIC.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.jobs.runner import run_job
from app.jobs.schedule import DATA_HEALTH_TASK
from app.models import User
from app.models.enums import CabinClass
from app.services import service_credentials
from app.services.flights.base import FlightQuery
from app.services.flights.duffel import DuffelProvider
from app.services.health import admin_problems
from app.services.http import PermanentProviderError
from app.services.notifications.ops_alerts import run_data_health_check
from app.services.notifications.providers import EmailMessageData, ResendEmailProvider
from tests.conftest import outbox_messages

pytestmark = pytest.mark.integration

QUERY = FlightQuery(
    origin="LHR",
    destination="LIS",
    departure_date=date(2026, 11, 20),
    return_date=date(2026, 11, 27),
    adults=1,
    cabin_class=CabinClass.ECONOMY,
    currency="EUR",
    max_connections=1,
)


def keys(db: Session) -> dict[str, str]:
    return {p.key: p.severity for p in admin_problems(db)}


def answer(status: int, body: dict[str, Any]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


def health_job() -> dict[str, Any]:
    """One ops.data_health run through the job runner (its alert state persists)."""
    return run_job(DATA_HEALTH_TASK, run_data_health_check, chain=False)


def make_admin(db: Session) -> None:
    db.add(
        User(
            email="creds-admin@example.com",
            password_hash="synthetic-hash",
            full_name="Synthetic Admin",
            is_admin=True,
            is_active=True,
            email_verified_at=utcnow(),
        )
    )
    db.commit()


def test_a_refused_flight_token_alerts_the_admins_until_a_search_succeeds(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "flight_provider", "duffel")
    monkeypatch.setattr(settings, "duffel_access_token", "duffel_test_synthetic")
    make_admin(db)
    own = Settings(duffel_access_token="duffel_test_synthetic", flight_provider="duffel")
    refused = DuffelProvider(
        lambda code: "UTC",  # type: ignore[arg-type,return-value]
        own,
        client=answer(401, {"errors": [{"message": "The access token is not valid"}]}),
        backoff_seconds=0,
    )
    with pytest.raises(PermanentProviderError, match="authentication failed"):
        refused.search(QUERY)
    assert keys(db).get("provider_credentials:flights_duffel") == "failing"
    report = health_job()
    assert "new:provider_credentials:flights_duffel" in report["alerts"]
    sent = [m for m in outbox_messages() if m.get("tag") == "ops_alert"]
    assert sent and {m["to"] for m in sent} == {"creds-admin@example.com"}
    assert "DUFFEL_ACCESS_TOKEN" in str(sent[-1]["text"])
    # The token is renewed: the next search is accepted and the problem clears.
    accepted = DuffelProvider(
        lambda code: "UTC",  # type: ignore[arg-type,return-value]
        own,
        client=answer(200, {"data": {"offers": []}}),
        backoff_seconds=0,
    )
    assert accepted.search(QUERY) == []
    assert "provider_credentials:flights_duffel" not in keys(db)
    recovered = health_job()
    assert "recovered:provider_credentials:flights_duffel" in recovered["alerts"]


def test_a_refused_email_key_is_a_problem_until_a_message_is_accepted(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "email_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_synthetic")
    message = EmailMessageData(
        to="someone@example.com", subject="Synthetic", text="Synthetic body", tag="test"
    )
    with pytest.raises(PermanentProviderError, match="authentication failed"):
        ResendEmailProvider(settings, client=answer(401, {}), backoff_seconds=0).send(message)
    problems = admin_problems(db)
    (problem,) = [p for p in problems if p.key == "provider_credentials:email_resend"]
    assert problem.severity == "failing" and "RESEND_API_KEY" in problem.message
    ResendEmailProvider(settings, client=answer(200, {"id": "re_1"}), backoff_seconds=0).send(
        message
    )
    assert "provider_credentials:email_resend" not in keys(db)


def test_another_providers_record_is_not_reported(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the configured providers count: a record left by a provider the deployment no
    longer uses (FLIGHT_PROVIDER changed) is no problem."""
    service_credentials.record_refused("flights", "amadeus", "HTTP 401")
    monkeypatch.setattr(get_settings(), "flight_provider", "duffel")
    assert not any(k.startswith("provider_credentials:flights_") for k in keys(db))
