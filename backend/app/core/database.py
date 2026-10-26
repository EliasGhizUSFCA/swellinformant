"""Database engine, session factory and declarative base.

The engine connects to :func:`engine_url`: DATABASE_URL (the pooled URL on Neon) unless
DB_USE_UNPOOLED is set, as in the sync daemon image. Migrations use :func:`migration_url`
(the unpooled URL when there is one). DATABASE_URL_UNPOOLED is used only while it names the
same database as DATABASE_URL (Settings.unpooled_database_url, which the settings'
engine_database_url and migration_database_url apply too): a test or tool that points
DATABASE_URL at a scratch database must never migrate the database a stale unpooled URL
names. Connections are checked before use (pre-ping) and replaced after
DB_POOL_RECYCLE_SECONDS, because a pooler or a scaled-to-zero database closes idle
connections on its side. With DB_PREPARED_STATEMENTS=false psycopg never prepares
statements (``prepare_threshold=None``), which a PgBouncer in transaction mode without
prepared-statement support needs.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def connect_args(settings: Settings) -> dict[str, Any]:
    """psycopg connection arguments: ``prepare_threshold=None`` (never prepare) when
    DB_PREPARED_STATEMENTS is false. Alembic's env.py uses them too."""
    return {} if settings.db_prepared_statements else {"prepare_threshold": None}


def engine_options(settings: Settings) -> dict[str, Any]:
    """Keyword arguments of :func:`create_engine` for the application engine."""
    return {
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "connect_args": connect_args(settings),
    }


def engine_url(settings: Settings) -> str:
    """The application engine's URL: settings.engine_database_url (the unpooled URL only
    with DB_USE_UNPOOLED, and only for the same database)."""
    return settings.engine_database_url


def migration_url(settings: Settings) -> str:
    """Alembic's URL: settings.migration_database_url (the unpooled URL, a direct
    connection for DDL, when it names the same database)."""
    return settings.migration_database_url


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(engine_url(settings), **engine_options(settings))
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests and forked workers)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def SessionLocal() -> Session:  # factory mimics sessionmaker naming
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> bool:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
