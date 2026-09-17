from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.core.deps import Tenant, TenantDB
from app.core.errors import NotFound
from app.modules.platform.models import Tenant as TenantModel

router = APIRouter(prefix="/api/v1/store", tags=["store"])


class StoreOut(BaseModel):
    name: str
    store_mode: str
    default_locale: str
    primary_host: str | None
    status: str


@router.get("", response_model=StoreOut)
async def store_info(tenant: Tenant, db: TenantDB):
    """Public: what the storefront needs to boot. RLS makes other tenants invisible."""
    t = (await db.execute(select(TenantModel))).scalars().all()
    if len(t) != 1:
        raise NotFound("Store not found")
    return StoreOut(
        name=t[0].name,
        store_mode=t[0].store_mode,
        default_locale=t[0].default_locale,
        primary_host=tenant.primary_host,
        status=t[0].status,
    )
