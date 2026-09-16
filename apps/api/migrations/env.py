"""Alembic env. Migrations run as the MIGRATOR role (owner), never as the app role.

URL comes from APP_MIGRATION_DATABASE_URL (sync or async driver accepted).
"""

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get(
        "APP_MIGRATION_DATABASE_URL", "postgresql+asyncpg://postgres@localhost/ecomm"
    )
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = async_engine_from_config(
        {"sqlalchemy.url": _url()}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
