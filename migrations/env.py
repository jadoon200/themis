"""Alembic environment. The database URL comes from THEMIS settings, not alembic.ini."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from themis.config import load_settings
from themis.db.base import Base
from themis.db import models  # noqa: F401  — imported for its side effect of registering tables

config = context.config
config.set_main_option("sqlalchemy.url", load_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # batch mode keeps SQLite able to run ALTERs, which matters because the
            # tests use it.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
