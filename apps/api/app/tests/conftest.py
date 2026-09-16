import os
import subprocess
import sys
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/postgres"
)
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="session")
async def database_name():
    """Fresh database per test session, migrated to head, dropped afterwards."""
    name = f"test_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as c:
        await c.execute(text(f'CREATE DATABASE "{name}"'))
        # Roles mirror production (infra/postgres/init/01-roles.sql).
        await c.execute(
            text("""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='app') THEN
                CREATE ROLE app LOGIN PASSWORD 'app' NOBYPASSRLS; END IF;
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='platform') THEN
                CREATE ROLE platform LOGIN PASSWORD 'platform' BYPASSRLS; END IF;
            END $$;""")
        )
    base = ADMIN_URL.rsplit("/", 1)[0]
    env = {**os.environ, "APP_MIGRATION_DATABASE_URL": f"{base}/{name}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR, env=env, check=True
    )
    yield name
    async with admin.connect() as c:
        await c.execute(
            text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{name}'")
        )
        await c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    await admin.dispose()


@pytest.fixture(scope="session")
def settings(database_name) -> Settings:
    host = ADMIN_URL.split("@", 1)[1].rsplit("/", 1)[0]
    return Settings(
        env="test",
        database_url=f"postgresql+asyncpg://app:app@{host}/{database_name}",
        platform_database_url=f"postgresql+asyncpg://platform:platform@{host}/{database_name}",
        redis_url=REDIS_URL,
        jwt_secret="test-secret-test-secret",
    )


@pytest.fixture(scope="session")
async def app(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
