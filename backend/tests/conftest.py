"""Test configuration.

Integration tests use a real PostgreSQL database (TEST_DATABASE_URL, default
postgresql+psycopg://swell:swell@localhost:5432/swell_test) migrated with Alembic, and
Redis database 15. Reference data (airports + spots) is seeded once per session;
everything else is truncated after each test.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_OUTBOX = tempfile.mkdtemp(prefix="swell-test-outbox-")
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "postgresql+psycopg://swell:swell@localhost:5432/swell_test"
        ),
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "FORECAST_PROVIDERS": "demo",
        "FORECAST_DAYS": "12",
        "FLIGHT_PROVIDER": "demo",
        "EMAIL_PROVIDER": "console",
        "SMS_PROVIDER": "console",
        "ENABLE_DEV_ENDPOINTS": "true",
        "CELERY_TASK_ALWAYS_EAGER": "true",
        "RATE_LIMIT_ENABLED": "false",
        "OUTBOX_DIR": _OUTBOX,
        "SECRET_KEY": "test-secret-key-0123456789",
        "COOKIE_SECURE": "false",
        "LOG_LEVEL": "WARNING",
    }
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal, get_engine  # noqa: E402
from app.core.redis import get_redis  # noqa: E402

MUTABLE_TABLES = [
    "notifications",
    "opportunity_offers",
    "opportunity_matches",
    "flight_offers",
    "flight_searches",
    "travel_preferences",
    "search_destinations",
    "search_origins",
    "saved_searches",
    "user_airports",
    "user_tokens",
    "user_sessions",
    "notification_preferences",
    "user_profiles",
    "users",
    "surf_quality_predictions",
    "wave_forecasts",
    "swell_events",
    "forecast_runs",
    "forecast_sources",
    "background_job_logs",
]


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[None]:
    if not _db_available():
        pytest.skip("PostgreSQL test database not available")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    command.upgrade(cfg, "head")
    from app.services.seed import seed_reference_data

    with SessionLocal() as db:
        seed_reference_data(db)
    yield


@pytest.fixture
def db(migrated_db: None) -> Iterator[Session]:
    get_redis().flushdb()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with get_engine().begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(MUTABLE_TABLES)} RESTART IDENTITY CASCADE"))
            conn.execute(text("UPDATE surf_spots SET is_active = true, calibration = NULL"))
        get_redis().flushdb()
        outbox = Path(get_settings().outbox_dir) / "outbox.jsonl"
        if outbox.exists():
            outbox.unlink()


@pytest.fixture
def few_spots(db: Session) -> list[str]:
    """Deactivate all but a handful of spots so pipeline tests run quickly."""
    keep = ["jeffreys-bay", "pipeline", "uluwatu", "nazare", "skeleton-bay"]
    db.execute(text("UPDATE surf_spots SET is_active = (slug = ANY(:keep))"), {"keep": keep})
    db.commit()
    return keep


class ApiClient:
    """TestClient wrapper that handles the CSRF double-submit token."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf = client.get("/api/auth/csrf").json()["csrf_token"]

    def _headers(self) -> dict[str, str]:
        token = self.client.cookies.get(get_settings().csrf_cookie_name) or self.csrf
        return {"X-CSRF-Token": token}

    def get(self, url: str, **kw: object):  # type: ignore[no-untyped-def]
        return self.client.get(url, **kw)  # type: ignore[arg-type]

    def post(self, url: str, json: object | None = None, **kw: object):  # type: ignore[no-untyped-def]
        return self.client.post(url, json=json, headers=self._headers(), **kw)  # type: ignore[arg-type]

    def put(self, url: str, json: object | None = None):  # type: ignore[no-untyped-def]
        return self.client.put(url, json=json, headers=self._headers())

    def patch(self, url: str, json: object | None = None):  # type: ignore[no-untyped-def]
        return self.client.patch(url, json=json, headers=self._headers())

    def delete(self, url: str, json: object | None = None):  # type: ignore[no-untyped-def]
        return self.client.request("DELETE", url, json=json, headers=self._headers())

    def register(
        self,
        email: str = "surfer@example.com",
        password: str = "Barrels4Days!",
        name: str = "Test Surfer",
        verify: bool = True,
    ) -> dict[str, object]:
        resp = self.post(
            "/api/auth/register",
            {"full_name": name, "email": email, "password": password, "confirm_password": password},
        )
        assert resp.status_code == 201, resp.text
        if verify:
            token = latest_link_token("verify-email")
            assert self.post("/api/auth/verify-email", {"token": token}).status_code == 200
        return resp.json()["user"]  # type: ignore[no-any-return]


def outbox_messages() -> list[dict[str, object]]:
    from app.services.notifications.providers import read_outbox

    return read_outbox(limit=200)


def latest_link_token(path: str) -> str:
    import re

    for msg in outbox_messages():
        match = re.search(rf"/{path}\?token=([\w-]+)", str(msg.get("text", "")))
        if match:
            return match.group(1)
    raise AssertionError(f"no {path} link in outbox")


@pytest.fixture
def app_client(db: Session) -> Iterator[ApiClient]:
    from app.main import app

    with TestClient(app) as client:
        yield ApiClient(client)


@pytest.fixture
def make_client(db: Session) -> Iterator[object]:
    from app.main import app

    clients: list[TestClient] = []

    def factory() -> ApiClient:
        c = TestClient(app)
        clients.append(c)
        return ApiClient(c)

    yield factory
    for c in clients:
        c.close()


def search_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "J-Bay from SFO",
        "date_mode": "flexible",
        "horizon_days": 30,
        "units": "ft",
        "wave_min": 4,
        "wave_max": 20,
        "min_quality": "good",
        "destination_mode": "spots",
        "destinations": {"spot_slugs": ["jeffreys-bay"]},
        "origins": ["SFO"],
        "notification_channel": "email",
        "priority": "balanced",
        "min_window_hours": 3,
        "travel": {
            "max_price": 5000,
            "currency": "USD",
            "max_layovers": 2,
            "max_flight_hours": 45,
            "min_days_at_destination": 3,
            "max_trip_days": 14,
            "travelers": 1,
            "arrival_buffer_days": 2,
            "departure_buffer_days": 1,
        },
    }
    for key, value in overrides.items():
        if key == "travel" and isinstance(value, dict):
            payload["travel"] = {**payload["travel"], **value}  # type: ignore[dict-item]
        else:
            payload[key] = value
    return payload


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_OUTBOX, ignore_errors=True)
