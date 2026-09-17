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
        jwt_secret="test-secret-test-secret-0123456789ab",
        platform_root_domain="test.local",
        edge_ips=["203.0.113.10"],
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


# ---- tenancy world: 2 tenants x 2 vendors (architecture §3.2) ------------------------------
from dataclasses import dataclass, field  # noqa: E402

from app.core.security import Principal, create_access_token  # noqa: E402


@dataclass
class Actor:
    token: str
    host: str

    def headers(self, host: str | None = None) -> dict:
        return {"authorization": f"Bearer {self.token}", "host": host or self.host}


@dataclass
class TenantWorld:
    id: str
    host: str
    staff: Actor
    vendors: dict = field(default_factory=dict)  # name -> vendor id
    storefronts: dict = field(default_factory=dict)  # name -> storefront id
    vendor_actors: dict = field(default_factory=dict)
    domain_id: str = ""


@pytest.fixture(scope="session")
def platform_headers(settings):
    tok = create_access_token(settings, Principal(sub="root", kind="platform", roles=("super",)))
    return {"authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
async def world(app, settings, platform_headers):
    from sqlalchemy import text as sql

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        out = {}
        for key, slug in (("A", "alpha"), ("B", "bravo")):
            r = await c.post(
                "/platform/v1/tenants",
                headers=platform_headers,
                json={
                    "slug": f"{slug}{uuid.uuid4().hex[:6]}",
                    "name": f"Store {key}",
                    "store_mode": "multi",
                },
            )
            assert r.status_code == 201, r.text
            body = r.json()
            staff_tok = create_access_token(
                settings,
                Principal(
                    sub=f"staff-{key}", kind="tenant_staff", tid=body["id"], roles=("owner",)
                ),
            )
            tw = TenantWorld(
                id=body["id"],
                host=body["primary_host"],
                staff=Actor(staff_tok, body["primary_host"]),
            )
            for n in (1, 2):
                name = f"{key}{n}"
                r = await c.post(
                    "/api/v1/admin/vendors",
                    headers=tw.staff.headers(),
                    json={"slug": f"v-{name.lower()}", "display_name": f"Vendor {name}"},
                )
                assert r.status_code == 201, r.text
                vid = r.json()["id"]
                tw.vendors[name] = vid
                tw.vendor_actors[name] = Actor(
                    create_access_token(
                        settings,
                        Principal(
                            sub=f"u-{name}",
                            kind="vendor_staff",
                            tid=tw.id,
                            vid=vid,
                            roles=("owner",),
                        ),
                    ),
                    tw.host,
                )
            r = await c.post(
                "/api/v1/admin/domains",
                headers=tw.staff.headers(),
                json={"host": f"shop-{key.lower()}{uuid.uuid4().hex[:4]}.example.com"},
            )
            assert r.status_code == 201, r.text
            tw.domain_id = r.json()["id"]
            out[key] = tw
        eng = app.state.platform_db.engine
        async with eng.connect() as conn:
            rows = (await conn.execute(sql("SELECT id, vendor_id FROM vendor_storefronts"))).all()
        by_vendor = {str(v): str(i) for i, v in rows}
        for tw in out.values():
            for name, vid in tw.vendors.items():
                tw.storefronts[name] = by_vendor[vid]
        return out
