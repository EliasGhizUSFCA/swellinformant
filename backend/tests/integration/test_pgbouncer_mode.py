"""The API through a PgBouncer in transaction mode, as on Neon's pooled DATABASE_URL. Owned
by the api-serverless package.

Skipped unless TEST_PGBOUNCER_URL is set: a PgBouncer with ``pool_mode = transaction`` in
front of the test database (the database TEST_DATABASE_URL names, already migrated by the
session fixture). The application engine is pointed at it with the hosted profile's pool
settings, DB_PREPARED_STATEMENTS=false and JOB_RUNNER=daemon, then the auth and spots flows
run from several threads, each query far more often than psycopg's default prepare
threshold (5). Behind a transaction pooler without prepared-statement support, a prepared
statement lands on one server connection and the next transaction may run on another,
which fails with 'prepared statement "_pg3_0" does not exist'; with
``prepare_threshold=None`` nothing is prepared.

The suite's scratch-database tests (migrations, backups) cannot run with TEST_DATABASE_URL
on the pooler: PgBouncer keeps idle server connections to each database it served (for
server_idle_timeout, 600 s by default), so their DROP DATABASE fails with "is being
accessed by other users". Run them on a direct URL.

Example (PgBouncer 1.22 on port 6439)::

    TEST_PGBOUNCER_URL=postgresql+psycopg://swell:swell@127.0.0.1:6439/swell_test_x \\
        pytest tests/integration/test_pgbouncer_mode.py

Accounts here are synthetic.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import get_settings, normalize_database_url
from app.core.database import get_engine, reset_engine
from app.core.profiles import HOSTED_PROFILE
from tests.conftest import ApiClient, latest_link_token, search_payload

PGBOUNCER_URL = os.environ.get("TEST_PGBOUNCER_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PGBOUNCER_URL, reason="TEST_PGBOUNCER_URL is not set"),
]

PASSWORD = "Barrels4Days!"
THREADS = 3
ROUNDS = 7  # > psycopg's default prepare_threshold of 5
SPOT_PATHS = (
    "/api/spots",
    "/api/spots/pipeline",
    "/api/spots/pipeline/timeline",
    "/api/spots/pipeline/tides",
    "/api/map/conditions",
)


@pytest.fixture
def pooled(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The application engine on TEST_PGBOUNCER_URL with the hosted profile's pool and job
    runner (JOB_RUNNER=daemon: account emails are sent inline, as on Vercel).

    The daemon runner also keeps Celery out of the threaded flows below: Celery's eager
    mode marks "inside a task" in a process-wide flag that it saves and restores around
    each eager task, so eager tasks in concurrent threads can leave it set, after which
    every later ``AsyncResult.get()`` in the session raises "Never call result.get()
    within a task!" (observed: 5 Celery tests failed after this module under eager
    Celery).
    """
    direct = get_engine().url
    url = normalize_database_url(PGBOUNCER_URL)
    if make_url(url).database != direct.database:
        pytest.fail("TEST_PGBOUNCER_URL must point at the TEST_DATABASE_URL database")
    settings = get_settings()
    with monkeypatch.context() as patch:
        patch.setattr(settings, "job_runner", HOSTED_PROFILE["JOB_RUNNER"])
        patch.setattr(settings, "database_url", url)
        patch.setattr(settings, "db_use_unpooled", False)
        patch.setattr(settings, "db_prepared_statements", False)
        patch.setattr(settings, "db_pool_size", int(HOSTED_PROFILE["DB_POOL_SIZE"]))
        patch.setattr(settings, "db_max_overflow", int(HOSTED_PROFILE["DB_MAX_OVERFLOW"]))
        patch.setattr(
            settings, "db_pool_recycle_seconds", int(HOSTED_PROFILE["DB_POOL_RECYCLE_SECONDS"])
        )
        reset_engine()
        yield url
        reset_engine()
    reset_engine()


def test_the_engine_goes_through_the_pooler_without_preparing(pooled: str) -> None:
    engine = get_engine()
    assert engine.url.port == make_url(pooled).port
    with engine.connect() as conn:
        for _ in range(ROUNDS):
            assert conn.execute(text("SELECT 1")).scalar() == 1
        raw = conn.connection.dbapi_connection
        assert raw is not None and raw.prepare_threshold is None  # type: ignore[attr-defined]


def test_a_statement_runs_on_any_server_connection(pooled: str) -> None:
    """The failure transaction pooling causes, made deterministic: connection A runs one
    statement in more transactions than psycopg's prepare threshold, then B holds the
    server connection A last used (PgBouncer reuses servers last-in first-out), so A's next
    transaction runs on another server connection. A statement prepared on the first one
    would fail there ('prepared statement "_pg3_0" does not exist', with PgBouncer's
    max_prepared_statements = 0); unprepared, it runs. Needs a pool of at least two server
    connections (default_pool_size >= 2)."""
    engine = get_engine()
    statement = text("SELECT count(*) FROM surf_spots WHERE is_active")
    with engine.connect() as a, engine.connect() as b:
        for _ in range(ROUNDS):
            a.execute(statement).scalar_one()
            a.commit()
        b_server = b.execute(text("SELECT pg_backend_pid()")).scalar_one()  # B's transaction
        a_server = a.execute(text("SELECT pg_backend_pid()")).scalar_one()
        assert a_server != b_server  # A is on another server connection now
        assert a.execute(statement).scalar_one() > 0
        a.commit()
        b.commit()


def account_flow(client: ApiClient, n: int) -> list[tuple[str, int]]:
    email = f"pooled-{n}@example.com"
    seen: list[tuple[str, int]] = []
    resp = client.post(
        "/api/auth/register",
        {
            "full_name": "Pooled Surfer",
            "email": email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        },
    )
    seen.append(("register", resp.status_code))
    for round_ in range(ROUNDS):
        seen.append(("me", client.get("/api/auth/me").status_code))
        seen.append(("logout", client.post("/api/auth/logout").status_code))
        resp = client.post("/api/auth/login", {"email": email, "password": PASSWORD})
        seen.append((f"login {round_}", resp.status_code))
    return seen


def spot_flow(client: ApiClient) -> list[tuple[str, int]]:
    return [(path, client.get(path).status_code) for _ in range(ROUNDS) for path in SPOT_PATHS]


def test_auth_and_spot_flows_through_the_pooler(pooled: str, db: Session) -> None:
    from app.main import app

    results: dict[int, list[tuple[str, int]]] = {}
    failures: list[BaseException] = []

    def run(n: int) -> None:
        try:
            with TestClient(app) as raw:
                client = ApiClient(raw)
                results[n] = account_flow(client, n) + spot_flow(client)
        except BaseException as exc:  # reported below
            failures.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert not failures, failures
    assert sorted(results) == list(range(THREADS))
    for n, seen in results.items():
        expected = {"register": 201}
        bad = [(step, code) for step, code in seen if code != expected.get(step, 200)]
        assert not bad, (n, bad)

    # Email verification and a saved search through the pooler too.
    with TestClient(app) as raw:
        client = ApiClient(raw)
        user = client.register(email="pooled-verify@example.com", verify=False)
        token = latest_link_token("verify-email")
        assert client.post("/api/auth/verify-email", {"token": token}).status_code == 200
        assert client.get("/api/auth/me").json()["email_verified"] is True, user
        created = client.post("/api/searches", search_payload())
        assert created.status_code == 201, created.text
        listed = client.get("/api/searches")
        assert [s["id"] for s in listed.json()] == [created.json()["id"]]
