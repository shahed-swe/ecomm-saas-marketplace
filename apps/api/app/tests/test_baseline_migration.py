import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_uuid_v7_generated_in_db(settings):
    eng = create_async_engine(settings.database_url)
    async with eng.connect() as c:
        u = uuid.UUID(str((await c.execute(text("select uuid_generate_v7()"))).scalar()))
        tenant = (await c.execute(text("select app_current_tenant()"))).scalar()
    await eng.dispose()
    assert u.version == 7
    assert tenant is None  # no session setting => NULL => RLS policies see nothing


async def test_app_role_cannot_bypass_rls(settings):
    eng = create_async_engine(settings.database_url)
    async with eng.connect() as c:
        bypass = (
            await c.execute(text("select rolbypassrls from pg_roles where rolname = current_user"))
        ).scalar()
    await eng.dispose()
    assert bypass is False
