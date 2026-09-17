from fastapi import APIRouter, Request
from sqlalchemy import select

from app.core.deps import PlatformAdmin, PlatformDB
from app.core.tenancy import invalidate_host
from app.modules.platform import service
from app.modules.platform.models import Domain, Tenant
from app.modules.platform.schemas import TenantCreate, TenantCreated, TenantOut, TenantPatch

router = APIRouter(prefix="/platform/v1", tags=["platform"])


@router.post("/tenants", response_model=TenantCreated, status_code=201)
async def create_tenant(body: TenantCreate, request: Request, _: PlatformAdmin, db: PlatformDB):
    tenant, host, house_id = await service.create_tenant(db, request.app.state.settings, body)
    await invalidate_host(request.app.state.redis, host)
    return TenantCreated(
        **TenantOut.model_validate(tenant).model_dump(), primary_host=host, house_vendor_id=house_id
    )


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(_: PlatformAdmin, db: PlatformDB):
    return list((await db.execute(select(Tenant).order_by(Tenant.id.desc()).limit(200))).scalars())


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def patch_tenant(
    tenant_id: str, body: TenantPatch, request: Request, _: PlatformAdmin, db: PlatformDB
):
    tenant = await service.patch_tenant(db, tenant_id, body)
    hosts = (await db.execute(select(Domain.host).where(Domain.tenant_id == tenant.id))).scalars()
    await invalidate_host(request.app.state.redis, *hosts)
    return tenant
