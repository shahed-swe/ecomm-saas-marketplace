"""Layer 2 proof: with the application filter removed, Postgres RLS alone returns zero rows."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

TABLES = ["tenants", "domains", "vendors", "tenant_settings", "vendor_storefronts"]


@pytest.fixture
async def app_conn(settings):
    eng = create_async_engine(settings.database_url)  # app role: NOBYPASSRLS
    async with eng.connect() as conn:
        yield conn
    await eng.dispose()


async def _scope(conn, tenant="", vendor=""):
    await conn.execute(
        text("SELECT set_config('app.tenant_id', :t, true), set_config('app.vendor_id', :v, true)"),
        {"t": tenant, "v": vendor},
    )


@pytest.mark.parametrize("table", TABLES)
async def test_no_tenant_context_sees_nothing(app_conn, world, table):
    async with app_conn.begin():
        n = (await app_conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar()
    assert n == 0


async def test_unfiltered_query_under_tenant_a_never_returns_tenant_b(app_conn, world):
    A, B = world["A"], world["B"]
    async with app_conn.begin():
        await _scope(app_conn, A.id)
        # deliberately NO tenant filter in SQL: RLS is the only defence here
        rows = (
            (await app_conn.execute(text("SELECT tenant_id FROM vendor_storefronts")))
            .scalars()
            .all()
        )
        leaked = (
            await app_conn.execute(
                text("SELECT count(*) FROM vendor_storefronts WHERE id = :id"),
                {"id": B.storefronts["B1"]},
            )
        ).scalar()
        tenants = (await app_conn.execute(text("SELECT id FROM tenants"))).scalars().all()
    assert rows and all(str(t) == A.id for t in rows)
    assert leaked == 0
    assert [str(t) for t in tenants] == [A.id]


async def test_vendor_context_hides_sibling_vendor(app_conn, world):
    A = world["A"]
    async with app_conn.begin():
        await _scope(app_conn, A.id, A.vendors["A1"])
        ids = (
            (await app_conn.execute(text("SELECT vendor_id FROM vendor_storefronts")))
            .scalars()
            .all()
        )
        vendors = (await app_conn.execute(text("SELECT id FROM vendors"))).scalars().all()
    assert [str(i) for i in ids] == [A.vendors["A1"]]
    assert [str(i) for i in vendors] == [A.vendors["A1"]]


async def test_write_into_other_tenant_is_rejected(app_conn, world):
    A, B = world["A"], world["B"]
    with pytest.raises(DBAPIError):
        async with app_conn.begin():
            await _scope(app_conn, A.id)
            await app_conn.execute(
                text("INSERT INTO tenant_settings (tenant_id) VALUES (:b)"), {"b": B.id}
            )


async def test_update_cannot_move_row_to_other_tenant(app_conn, world):
    A, B = world["A"], world["B"]
    with pytest.raises(DBAPIError):
        async with app_conn.begin():
            await _scope(app_conn, A.id)
            await app_conn.execute(text("UPDATE domains SET tenant_id = :b"), {"b": B.id})
