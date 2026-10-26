"""Database engine and Redis client options for a pooled, serverless deployment.

* The application engine connects to ``engine_database_url`` with pre-ping,
  DB_POOL_RECYCLE_SECONDS and, with DB_PREPARED_STATEMENTS=false, psycopg's
  ``prepare_threshold=None`` (Neon's pooler is PgBouncer in transaction mode).
* Alembic connects to ``migration_database_url`` (the unpooled URL) with NullPool and the
  same connect arguments.
* Redis clients take their timeouts from the REDIS_*_TIMEOUT_SECONDS settings, ping idle
  connections and retry only connection errors, once.

Owned by the api-serverless package.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import redis
from sqlalchemy import pool

from app.core import database
from app.core import redis as app_redis
from app.core.config import Settings, get_settings

BACKEND = Path(__file__).resolve().parents[2]
POOLED = "postgresql+psycopg://u:p@ep-x-pooler.us-east-1.aws.neon.tech/db?sslmode=require"
DIRECT = "postgresql+psycopg://u:p@ep-x.us-east-1.aws.neon.tech/db?sslmode=require"


def make(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_engine_options_default_profile() -> None:
    options = database.engine_options(make())
    assert options == {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,
        "connect_args": {},  # psycopg's default prepare_threshold (5) applies
    }


def test_engine_options_hosted_profile() -> None:
    settings = make(
        db_pool_size=2,
        db_max_overflow=3,
        db_pool_recycle_seconds=240,
        db_prepared_statements=False,
    )
    options = database.engine_options(settings)
    assert options["connect_args"] == {"prepare_threshold": None}
    assert (options["pool_size"], options["max_overflow"]) == (2, 3)
    assert options["pool_recycle"] == 240 and options["pool_pre_ping"] is True


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """create_engine calls of get_engine (nothing connects)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_create_engine(url: str, **kwargs: Any) -> object:
        calls.append((url, kwargs))

        class Engine:
            def dispose(self) -> None:
                pass

        return Engine()

    database.reset_engine()
    monkeypatch.setattr(database, "create_engine", fake_create_engine)
    yield calls
    monkeypatch.undo()
    database.reset_engine()


def test_get_engine_honours_engine_database_url(
    captured: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", POOLED)
    monkeypatch.setattr(settings, "database_url_unpooled", DIRECT)
    monkeypatch.setattr(settings, "db_prepared_statements", False)
    monkeypatch.setattr(settings, "db_pool_recycle_seconds", 240)
    database.get_engine()
    url, kwargs = captured[-1]
    assert url == POOLED  # the API keeps the pooled URL
    assert kwargs["connect_args"] == {"prepare_threshold": None}
    assert kwargs["pool_recycle"] == 240 and kwargs["pool_pre_ping"] is True

    database.reset_engine()
    monkeypatch.setattr(settings, "db_use_unpooled", True)  # the daemon image
    database.get_engine()
    assert captured[-1][0] == DIRECT


def test_a_real_engine_never_prepares_when_prepared_statements_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The option reaches psycopg: a connection of the engine has prepare_threshold None
    (needs the test PostgreSQL; skipped without it)."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    settings = make(database_url=get_settings().database_url, db_prepared_statements=False)
    engine = create_engine(settings.engine_database_url, **database.engine_options(settings))
    try:
        try:
            conn = engine.connect()
        except OperationalError:
            pytest.skip("PostgreSQL not available")
        with conn:
            for _ in range(8):  # more executions than psycopg's default threshold (5)
                conn.execute(text("SELECT 1"))
            raw = conn.connection.dbapi_connection
            assert raw is not None and raw.prepare_threshold is None  # type: ignore[attr-defined]
    finally:
        engine.dispose()


class Stop(Exception):
    """Raised by the fake engine factory: the migration never connects."""


def test_alembic_uses_the_unpooled_url_with_nullpool(monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlalchemy
    from alembic import command
    from alembic.config import Config

    seen: dict[str, Any] = {}

    def fake_engine_from_config(section: dict[str, str], **kwargs: Any) -> object:
        seen.update(url=section["sqlalchemy.url"], **kwargs)
        raise Stop

    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", POOLED)
    monkeypatch.setattr(settings, "database_url_unpooled", DIRECT)
    monkeypatch.setattr(settings, "db_prepared_statements", False)
    monkeypatch.setattr(sqlalchemy, "engine_from_config", fake_engine_from_config)
    cfg = Config(str(BACKEND / "alembic.ini"))
    with pytest.raises(Stop):
        command.upgrade(cfg, "head")
    assert seen["url"] == DIRECT
    assert seen["poolclass"] is pool.NullPool
    assert seen["connect_args"] == {"prepare_threshold": None}

    monkeypatch.setattr(settings, "database_url_unpooled", None)  # no direct URL: DATABASE_URL
    monkeypatch.setattr(settings, "db_prepared_statements", True)
    with pytest.raises(Stop):
        command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")
    assert seen["url"] == POOLED and seen["connect_args"] == {}


def test_an_unpooled_url_of_another_database_is_ignored(
    captured: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A process that points DATABASE_URL at a scratch database (the migration tests do,
    by environment) while DATABASE_URL_UNPOOLED still names another database (a stale
    variable, a pulled .env) uses DATABASE_URL for the engine and for migrations."""
    scratch = "postgresql+psycopg://u:p@localhost:5432/swell_test_scratch_1"
    other = "postgresql+psycopg://u:p@ep-x.us-east-1.aws.neon.tech/neondb"
    settings = make(database_url=scratch, database_url_unpooled=other, db_use_unpooled=True)
    assert settings.unpooled_database_url is None
    assert database.engine_url(settings) == scratch
    assert database.migration_url(settings) == scratch
    assert "'neondb' but DATABASE_URL names 'swell_test_scratch_1'" in caplog.text
    assert "u:p" not in caplog.text  # no credentials in the log

    same = make(database_url=POOLED, database_url_unpooled=DIRECT)  # Neon: same name "db"
    assert same.unpooled_database_url == DIRECT
    assert database.migration_url(same) == DIRECT and database.engine_url(same) == POOLED
    assert database.migration_url(make(database_url=POOLED)) == POOLED

    monkeypatch.setattr(get_settings(), "database_url", scratch)
    monkeypatch.setattr(get_settings(), "database_url_unpooled", other)
    monkeypatch.setattr(get_settings(), "db_use_unpooled", True)
    database.get_engine()
    assert captured[-1][0] == scratch


def test_alembic_ignores_an_unpooled_url_of_another_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy
    from alembic import command
    from alembic.config import Config

    seen: dict[str, Any] = {}

    def fake_engine_from_config(section: dict[str, str], **kwargs: Any) -> object:
        seen.update(url=section["sqlalchemy.url"])
        raise Stop

    scratch = "postgresql+psycopg://u:p@localhost:5432/swell_test_scratch_2"
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", scratch)
    monkeypatch.setattr(settings, "database_url_unpooled", DIRECT)  # database "db"
    monkeypatch.setattr(sqlalchemy, "engine_from_config", fake_engine_from_config)
    with pytest.raises(Stop):
        command.downgrade(Config(str(BACKEND / "alembic.ini")), "base")
    assert seen["url"] == scratch


# --------------------------------------------------------------------------- Redis
def test_redis_clients_use_the_timeout_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", "rediss://default:token@example.upstash.io:6379")
    monkeypatch.setattr(settings, "redis_connect_timeout_seconds", 1.5)
    monkeypatch.setattr(settings, "redis_socket_timeout_seconds", 2.0)
    app_redis.reset_redis()
    try:
        for client in (app_redis.get_redis(), app_redis.get_redis_bytes()):
            kwargs = client.connection_pool.connection_kwargs
            assert kwargs["socket_connect_timeout"] == 1.5
            assert kwargs["socket_timeout"] == 2.0
            assert kwargs["health_check_interval"] == app_redis.HEALTH_CHECK_INTERVAL_SECONDS
            assert kwargs["host"] == "example.upstash.io"  # rediss:// = TLS
            assert client.connection_pool.connection_class.__name__ == "SSLConnection"
            retry = kwargs["retry"]
            assert retry.get_retries() == 1
            assert redis.ConnectionError in retry._supported_errors
            assert redis.TimeoutError not in retry._supported_errors  # no second wait
        assert app_redis.get_redis().get_encoder().decode_responses is True
        assert app_redis.get_redis_bytes().get_encoder().decode_responses is False
    finally:
        app_redis.reset_redis()


def test_kv_url_reaches_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vercel's Upstash integration sets KV_URL (REDIS_URL wins when both are set)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("KV_URL", "rediss://default:token@kv.upstash.io:6379")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.redis_url == "rediss://default:token@kv.upstash.io:6379"
    client = redis.Redis.from_url(settings.redis_url, **app_redis.client_options(settings))
    assert client.connection_pool.connection_kwargs["host"] == "kv.upstash.io"
