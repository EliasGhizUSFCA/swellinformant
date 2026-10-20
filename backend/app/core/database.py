"""Database engine, session factory and declarative base.

The engine connects to :func:`engine_url`: DATABASE_URL (the pooled URL on Neon) unless
DB_USE_UNPOOLED is set, as in the sync daemon image. Migrations use :func:`migration_url`
(the unpooled URL when there is one). DATABASE_URL_UNPOOLED is used only while it names the
same database as DATABASE_URL (:func:`unpooled_url`): a test or tool that points
DATABASE_URL at a scratch database must never migrate the database a stale unpooled URL
names. Connections are checked before use (pre-ping) and replaced after
DB_POOL_RECYCLE_SECONDS, because a pooler or a scaled-to-zero database closes idle
connections on its side. With DB_PREPARED_STATEMENTS=false psycopg never prepares
statements (``prepare_threshold=None``), which a PgBouncer in transaction mode without
prepared-statement support needs.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
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


def _database_name(url: str) -> str | None:
    try:
        return make_url(url).database
    except ArgumentError:
        return None


def unpooled_url(settings: Settings) -> str | None:
    """DATABASE_URL_UNPOOLED when it is set and names the same database as DATABASE_URL.

    The direct and the pooled URL of one database differ in host (and, on some hosts, user)
    but never in database name. Another name means the unpooled URL does not lead to this
    process's database (a stale variable, or a test or tool that pointed DATABASE_URL
    elsewhere): it is ignored with a warning and DATABASE_URL is used.
    """
    unpooled = settings.database_url_unpooled
    if not unpooled:
        return None
    direct_name, pooled_name = _database_name(unpooled), _database_name(settings.database_url)
    if direct_name != pooled_name:
        logger.warning(
            "DATABASE_URL_UNPOOLED names database %r but DATABASE_URL names %r; "
            "ignoring the unpooled URL",
            direct_name,
            pooled_name,
        )
        return None
    return unpooled


def engine_url(settings: Settings) -> str:
    """The application engine's URL: settings.engine_database_url (the unpooled URL only
    with DB_USE_UNPOOLED), guarded by :func:`unpooled_url`."""
    if settings.db_use_unpooled and (direct := unpooled_url(settings)):
        return direct
    return settings.database_url


def migration_url(settings: Settings) -> str:
    """Alembic's URL: settings.migration_database_url (the unpooled URL, a direct
    connection for DDL), guarded by :func:`unpooled_url`."""
    return unpooled_url(settings) or settings.database_url


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


def SessionLocal() -> Session:  # noqa: N802 - factory mimics sessionmaker naming
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


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs: commit on success, rollback on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> bool:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
