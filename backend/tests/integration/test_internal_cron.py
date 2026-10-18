"""GET /api/internal/cron/daily, the daily watchdog (contract F8). Owned by the
api-serverless package.

Created by the foundation: the 404 / 401 behaviour is final; an authorised call answers
503 "unavailable" until api-serverless builds the watchdog and changes that test to the
200 body {status, problems, alerts_sent, heartbeat_age_minutes}.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.internal import CRON_DAILY_PATH
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_the_path_is_the_vercel_cron_path() -> None:
    assert CRON_DAILY_PATH == "/api/internal/cron/daily"


def test_cron_route_is_absent_without_a_secret(client: TestClient) -> None:
    assert get_settings().cron_secret is None
    unknown = client.get("/api/internal/no-such-route")
    assert unknown.status_code == 404
    for headers in ({}, {"Authorization": "Bearer anything-at-all-123"}):
        r = client.get(CRON_DAILY_PATH, headers=headers)
        assert r.status_code == 404 and r.json() == unknown.json()
    assert CRON_DAILY_PATH not in client.get("/openapi.json").json()["paths"]


def test_cron_route_checks_the_bearer_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s3cret-token-for-the-watchdog"
    monkeypatch.setattr(get_settings(), "cron_secret", secret)
    for value in (
        None,
        secret,
        f"Basic {secret}",
        "Bearer wrong-token-1234567",
        "Bearer ",
        f"Bearer {secret}x",
        f"Bearer {secret[:-1]}",
    ):
        headers = {"Authorization": value} if value is not None else {}
        assert client.get(CRON_DAILY_PATH, headers=headers).status_code == 401, value
    ok = client.get(CRON_DAILY_PATH, headers={"Authorization": f"Bearer {secret}"})
    # Authorised, but the watchdog is not built yet: an explicit unavailable state.
    assert ok.status_code == 503
    assert ok.headers["cache-control"] == "private, no-store"
    body = ok.json()
    assert body["status"] == "unavailable" and body["problems"] == []
    assert body["alerts_sent"] == [] and body["heartbeat_age_minutes"] is None
