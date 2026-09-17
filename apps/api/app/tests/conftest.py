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
    from app.modules.identity.sms import FakeSms

    application = create_app(settings)
    application.state.sms = FakeSms()
    import tempfile

    from app.core.storage.private import LocalPrivateStorage

    application.state.private_storage = LocalPrivateStorage(
        tempfile.mkdtemp(prefix="priv-"), settings.jwt_secret
    )
    async with application.router.lifespan_context(application):
        await application.state.redis.flushdb()  # rate-limit counters must not leak between runs
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
    vendor_owner_memberships: dict = field(default_factory=dict)  # name -> vendor_users.id
    owner_staff_member_id: str = ""
    theme_version_id: str = ""
    invoice_id: str = ""
    document_id: str = ""
    catalog: dict = field(default_factory=dict)  # kind -> {vendor name or "tenant": id}
    payout_hold_id: str = ""
    domain_id: str = ""


@pytest.fixture(scope="session")
def platform_headers(settings):
    tok = create_access_token(settings, Principal(sub="root", kind="platform", roles=("super",)))
    return {"authorization": f"Bearer {tok}"}


PASSWORD = "correct-horse-battery"


async def set_password(app, email: str, tenant_id: str):
    from sqlalchemy import text as sql

    from app.core.passwords import hash_password

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            sql("UPDATE users SET password_hash=:h WHERE tenant_id=:t AND email=:e"),
            {"h": hash_password(PASSWORD), "t": tenant_id, "e": email},
        )


async def login(c, host: str, email: str, surface: str, vendor_id: str | None = None) -> str:
    r = await c.post(
        "/api/v1/auth/login",
        headers={"host": host},
        json={"email": email, "password": PASSWORD, "surface": surface, "vendor_id": vendor_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def make_tenant(c, app, platform_headers, *, slug: str, name: str, store_mode="single"):
    owner = f"owner@{slug}.example.com"
    r = await c.post(
        "/platform/v1/tenants",
        headers=platform_headers,
        json={
            "slug": slug,
            "name": name,
            "store_mode": store_mode,
            "owner_email": owner,
            "owner_password": PASSWORD,
            "plan_code": "growth" if store_mode == "multi" else "starter",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    body["owner_email"] = owner
    body["staff_token"] = await login(c, body["primary_host"], owner, "staff")
    return body


def jpeg_bytes(w=1200, h=900, color=(30, 120, 200)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def build_catalog(c, app, tw, key):
    """Per tenant: category + attribute + brand; per vendor X1/X2: approved, product, media, question, import."""
    from sqlalchemy import text as sql

    async with app.state.platform_db.engine.begin() as conn:
        await conn.execute(
            sql("UPDATE vendors SET status='approved' WHERE tenant_id=:t"), {"t": tw.id}
        )
        await conn.execute(
            sql("UPDATE tenant_settings SET moderation_mode='none' WHERE tenant_id=:t"),
            {"t": tw.id},
        )
    staff = tw.staff.headers()
    cat = (
        await c.post(
            "/api/v1/admin/catalog/categories",
            headers=staff,
            json={"slug": "fashion", "name_en": "Fashion", "name_bn": "ফ্যাশন"},
        )
    ).json()
    attr = (
        await c.post(
            f"/api/v1/admin/catalog/categories/{cat['id']}/attributes",
            headers=staff,
            json={
                "key": "material",
                "label_en": "Material",
                "type": "select",
                "options": ["cotton", "silk"],
            },
        )
    ).json()
    brand = (
        await c.post(
            "/api/v1/admin/catalog/brands", headers=staff, json={"slug": "aarong", "name": "Aarong"}
        )
    ).json()
    tw.catalog = {
        "category": {"tenant": cat["id"]},
        "attribute": {"tenant": attr["id"]},
        "brand": {"tenant": brand["id"]},
        "product": {},
        "variant": {},
        "asset": {},
        "product_media": {},
        "question": {},
        "import_job": {},
    }
    buyer = await c.post(
        "/api/v1/auth/register",
        headers={"host": tw.host},
        json={"email": f"buyer-{key.lower()}@example.com", "password": PASSWORD},
    )
    buyer_h = {"authorization": f"Bearer {buyer.json()['access_token']}", "host": tw.host}
    c.cookies.clear()
    for n in (1, 2):
        name = f"{key}{n}"
        vh = tw.vendor_actors[name].headers()
        asset = await c.post(
            "/api/v1/vendor/media",
            headers=vh,
            files={"file": ("p.jpg", jpeg_bytes(color=(n * 60, 80, 120)), "image/jpeg")},
        )
        assert asset.status_code == 202 and asset.json()["status"] == "ready", asset.text
        prod = await c.post(
            "/api/v1/vendor/products",
            headers=vh,
            json={
                "slug": f"kurti-{name.lower()}",
                "title_en": f"Cotton Kurti {name}",
                "category_id": cat["id"],
                "brand_id": brand["id"],
                "attributes": {"material": "cotton"},
                "variants": [
                    {
                        "sku": f"KURTI-{name}-M",
                        "options": {"Size": "M"},
                        "price": "1250.00",
                        "stock": 5,
                    }
                ],
            },
        )
        assert prod.status_code == 201, prod.text
        pid = prod.json()["id"]
        media = await c.post(
            f"/api/v1/vendor/products/{pid}/media",
            headers=vh,
            json={"asset_id": asset.json()["id"]},
        )
        assert media.status_code == 201, media.text
        assert (
            await c.patch(f"/api/v1/vendor/products/{pid}", headers=vh, json={"status": "active"})
        ).status_code == 200
        q = await c.post(
            f"/api/v1/catalog/products/kurti-{name.lower()}/questions",
            headers=buyer_h,
            json={"question": "Is this true to size?"},
        )
        assert q.status_code == 201, q.text
        imp = await c.post(
            "/api/v1/vendor/imports",
            headers=vh,
            files={"file": ("p.csv", b"handle,title_en,category,sku,price\n", "text/csv")},
        )
        assert imp.status_code == 202, imp.text
        tw.catalog["product"][name] = pid
        tw.catalog["variant"][name] = prod.json()["variants"][0]["id"]
        tw.catalog["asset"][name] = asset.json()["id"]
        tw.catalog["product_media"][name] = media.json()["id"]
        tw.catalog["question"][name] = q.json()["id"]
        tw.catalog["import_job"][name] = imp.json()["id"]


@pytest.fixture(scope="session")
async def world(app, settings, platform_headers):
    from sqlalchemy import text as sql

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        out = {}
        for key, slug in (("A", "alpha"), ("B", "bravo")):
            body = await make_tenant(
                c,
                app,
                platform_headers,
                slug=f"{slug}{uuid.uuid4().hex[:6]}",
                name=f"Store {key}",
                store_mode="multi",
            )
            tw = TenantWorld(
                id=body["id"],
                host=body["primary_host"],
                staff=Actor(body["staff_token"], body["primary_host"]),
            )
            for n in (1, 2):
                name = f"{key}{n}"
                email = f"{name.lower()}@vendors.example.com"
                r = await c.post(
                    "/api/v1/admin/vendors",
                    headers=tw.staff.headers(),
                    json={
                        "slug": f"v-{name.lower()}",
                        "display_name": f"Vendor {name}",
                        "owner_email": email,
                    },
                )
                assert r.status_code == 201, r.text
                vid = r.json()["id"]
                tw.vendors[name] = vid
                await set_password(app, email, tw.id)
                tw.vendor_actors[name] = Actor(
                    await login(c, tw.host, email, "vendor", vid), tw.host
                )
            r = await c.post(
                "/api/v1/admin/domains",
                headers=tw.staff.headers(),
                json={"host": f"shop-{key.lower()}{uuid.uuid4().hex[:4]}.example.com"},
            )
            assert r.status_code == 201, r.text
            tw.domain_id = r.json()["id"]
            versions = await c.get("/api/v1/admin/theme/versions", headers=tw.staff.headers())
            tw.theme_version_id = versions.json()[0]["id"]
            async with app.state.platform_db.sessionmaker() as ps, ps.begin():
                from datetime import date, timedelta

                from app.modules.billing.service import generate_invoice

                await ps.execute(
                    sql(
                        "UPDATE tenant_subscriptions SET status='active', "
                        "current_period_start=:s, current_period_end=:e WHERE tenant_id=:t"
                    ),
                    {"s": date.today() - timedelta(days=31), "e": date.today(), "t": tw.id},
                )
                inv = await generate_invoice(ps, tw.id)
                tw.invoice_id = str(inv["id"])
                tw.payout_hold_id = str(
                    (
                        await ps.execute(
                            sql(
                                "INSERT INTO payout_holds (tenant_id, vendor_id, reason, created_by) "
                                "VALUES (:t, :v, 'manual', 'test') RETURNING id"
                            ),
                            {"t": tw.id, "v": tw.vendors[f"{key}1"]},
                        )
                    ).scalar()
                )
            v1 = tw.vendor_actors[f"{key}1"].headers()
            up = (
                await c.post(
                    "/api/v1/vendor/documents/upload-url",
                    headers=v1,
                    json={"doc_type": "nid", "content_type": "application/pdf", "byte_size": 12},
                )
            ).json()
            assert (
                await c.put(up["url"], content=b"%PDF-1.4 nid", headers=up["headers"])
            ).status_code == 201
            doc = await c.post(
                "/api/v1/vendor/documents",
                headers=v1,
                json={
                    "doc_type": "nid",
                    "storage_key": up["storage_key"],
                    "document_number": "1990123456",
                },
            )
            assert doc.status_code == 201, doc.text
            tw.document_id = doc.json()["id"]
            await build_catalog(c, app, tw, key)
            out[key] = tw
        eng = app.state.platform_db.engine
        async with eng.connect() as conn:
            rows = (await conn.execute(sql("SELECT id, vendor_id FROM vendor_storefronts"))).all()
            vus = (
                await conn.execute(sql("SELECT id, vendor_id FROM vendor_users WHERE role='owner'"))
            ).all()
            members = (
                await conn.execute(
                    sql(
                        "SELECT m.id, m.tenant_id FROM staff_members m JOIN staff_roles r ON r.id = m.role_id "
                        "WHERE r.key = 'owner'"
                    )
                )
            ).all()
        by_vendor = {str(v): str(i) for i, v in rows}
        vu_by_vendor = {str(v): str(i) for i, v in vus}
        member_by_tenant = {str(t): str(i) for i, t in members}
        for tw in out.values():
            for name, vid in tw.vendors.items():
                tw.storefronts[name] = by_vendor[vid]
                tw.vendor_owner_memberships[name] = vu_by_vendor[vid]
            tw.owner_staff_member_id = member_by_tenant[tw.id]
        return out
