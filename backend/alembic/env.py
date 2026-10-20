"""Alembic environment: the database URL comes from application settings.

Migrations connect through ``settings.migration_database_url``: DATABASE_URL_UNPOOLED (a
direct connection; Vercel's Neon integration sets it) when it is set, else DATABASE_URL.
DDL belongs on a direct connection, not on a transaction-mode pooler. NullPool: one
connection for the run, closed at the end. With DB_PREPARED_STATEMENTS=false psycopg never
prepares statements, as in the application engine (app.core.database.connect_args).
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (registers all tables)
from app.core.config import get_settings
from app.core.database import Base, connect_args

settings = get_settings()
config = context.config
config.set_main_option("sqlalchemy.url", settings.migration_database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args(settings),
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
