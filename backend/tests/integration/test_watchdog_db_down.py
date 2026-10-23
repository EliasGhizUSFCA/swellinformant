"""The daily watchdog when the database cannot be reached (Upgrade 4 integration).

Admin recipients come from the users table, so with Postgres down (e.g. Neon's compute
suspended after its quota) nobody could be told. OPS_ALERT_EMAILS is the fallback: the
cron answers 503 and emails those addresses, at most once per UTC day. The database
failure is SIMULATED (the watchdog job raises SQLAlchemy's OperationalError); addresses and
the secret are synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import internal
from app.api.internal import CRON_DAILY_PATH
from app.core.config import get_settings
from app.services.notifications.ops_alerts import DB_UNREACHABLE_ALERT
from tests.conftest import outbox_messages

pytestmark = pytest.mark.integration

SECRET = "s3cret-token-for-the-watchdog"
AUTH = {"Authorization": f"Bearer {SECRET}"}
OPS = ["ops-one@example.com", "ops-two@example.org"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app.main import app

    monkeypatch.setattr(get_settings(), "cron_secret", SECRET)
    with TestClient(app) as c:
        yield c


def database_down() -> None:
    raise OperationalError("SELECT 1", {}, Exception("synthetic: connection refused"))


def ops_mail() -> list[dict[str, object]]:
    return [m for m in outbox_messages() if m.get("tag") == "ops_alert"]


def test_with_the_database_down_ops_alert_emails_are_told_once_a_day(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(get_settings(), "ops_alert_emails", ",".join(OPS))
    monkeypatch.setattr(internal, "run_watchdog_job", database_down)
    before = len(ops_mail())
    first = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert first.status_code == 503
    body = first.json()
    assert body["status"] == "error" and body["problems"] == ["watchdog_failed:OperationalError"]
    assert body["alerts_sent"] == [DB_UNREACHABLE_ALERT]
    sent = ops_mail()[before:]
    assert sorted(str(m["to"]) for m in sent) == sorted(OPS)
    assert all("database cannot be reached" in str(m["subject"]) for m in sent)
    assert not any(address in caplog.text for address in OPS)  # counts only in the logs

    # A second delivery the same day (Vercel may deliver a cron twice) sends nothing.
    again = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert again.status_code == 503 and again.json()["alerts_sent"] == []
    assert len(ops_mail()) == before + len(OPS)


def test_without_ops_alert_emails_or_for_other_errors_nothing_is_sent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(internal, "run_watchdog_job", database_down)
    before = len(ops_mail())
    unset = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert unset.status_code == 503 and unset.json()["alerts_sent"] == []

    monkeypatch.setattr(get_settings(), "ops_alert_emails", ",".join(OPS))

    def broken() -> None:
        raise RuntimeError("synthetic: not a database failure")

    monkeypatch.setattr(internal, "run_watchdog_job", broken)
    other = client.get(CRON_DAILY_PATH, headers=AUTH)
    assert other.status_code == 503 and other.json()["alerts_sent"] == []
    assert len(ops_mail()) == before
