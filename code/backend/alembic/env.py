"""Alembic runtime environment.

The DSN comes from :mod:`app.core.config`, so migrations use exactly the same
connection settings, pool and credentials as the application.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings

# Importing the package registers every mapper on Base.metadata; without it
# `alembic revision --autogenerate` would produce an empty migration.
from app.db import models  # noqa: F401
from app.db.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` must be escaped: alembic.ini values pass through configparser interpolation.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata

# Detects column type/default drift that would otherwise stay invisible.
COMPARE_OPTS = {
    "compare_type": True,
    "compare_server_default": True,
}


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE_OPTS,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            **COMPARE_OPTS,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
