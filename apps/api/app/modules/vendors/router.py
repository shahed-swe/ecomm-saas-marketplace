from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.deps import CurrentVendor, Tenant, TenantDB, require_tenant_staff
from app.core.errors import Conflict, NotFound
from app.modules.platform.models import Tenant as TenantModel
from app.modules.vendors.models import Vendor, VendorStorefront
from app.modules.vendors.repository import (
    StorefrontRepository,
    TenantStorefrontRepository,
    VendorRepository,
)
from app.modules.vendors.schemas import StorefrontOut, StorefrontUpdate, VendorCreate, VendorOut

vendor_router = APIRouter(prefix="/api/v1/vendor", tags=["vendor"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin:vendors"])


# ---- vendor boundary ---------------------------------------------------------------
@vendor_router.get("/storefront", response_model=StorefrontOut)
async def my_storefront(p: CurrentVendor, tenant: Tenant, db: TenantDB):
    sf = await StorefrontRepository(db, tenant.id, p.vid).mine()
    if sf is None:
        raise NotFound("Not found")
    return sf


@vendor_router.get("/storefronts/{storefront_id}", response_model=StorefrontOut)
async def get_storefront(storefront_id: str, p: CurrentVendor, tenant: Tenant, db: TenantDB):
    sf = await StorefrontRepository(db, tenant.id, p.vid).get(storefront_id)
    if sf is None:
        raise NotFound("Not found")  # 404, never 403: do not confirm the id exists
    return sf


@vendor_router.patch("/storefronts/{storefront_id}", response_model=StorefrontOut)
async def update_storefront(
    storefront_id: str, body: StorefrontUpdate, p: CurrentVendor, tenant: Tenant, db: TenantDB
):
    sf = await StorefrontRepository(db, tenant.id, p.vid).get(storefront_id)
    if sf is None:
        raise NotFound("Not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(sf, k, v)
    await db.flush()
    return sf


# ---- tenant staff boundary -------------------------------------------------------------
@admin_router.get("/vendors", response_model=list[VendorOut])
async def list_vendors(
    tenant: Tenant, db: TenantDB, _=Depends(require_tenant_staff("vendors.read"))
):
    return await VendorRepository(db, tenant.id).list()


@admin_router.get("/vendors/{vendor_id}", response_model=VendorOut)
async def get_vendor(
    vendor_id: str, tenant: Tenant, db: TenantDB, _=Depends(require_tenant_staff("vendors.read"))
):
    v = await VendorRepository(db, tenant.id).get(vendor_id)
    if v is None:
        raise NotFound("Not found")
    return v


@admin_router.post("/vendors", response_model=VendorOut, status_code=201)
async def create_vendor(
    body: VendorCreate,
    tenant: Tenant,
    db: TenantDB,
    _=Depends(require_tenant_staff("vendors.write")),
):
    mode = (
        await db.execute(select(TenantModel.store_mode).where(TenantModel.id == tenant.id))
    ).scalar_one()
    if mode != "multi":
        raise Conflict("Enable multi-vendor mode to add vendors")
    repo = VendorRepository(db, tenant.id)
    if await repo.slug_taken(body.slug):
        raise Conflict("Slug unavailable")  # never mentions who holds it
    v = repo.add(Vendor(slug=body.slug, display_name=body.display_name, status="registered"))
    await db.flush()
    TenantStorefrontRepository(db, tenant.id).add(VendorStorefront(vendor_id=v.id))
    await db.flush()
    await db.refresh(v)
    return v


@admin_router.get("/storefronts/{storefront_id}", response_model=StorefrontOut)
async def admin_get_storefront(
    storefront_id: str,
    tenant: Tenant,
    db: TenantDB,
    _=Depends(require_tenant_staff("vendors.read")),
):
    sf = await TenantStorefrontRepository(db, tenant.id).get(storefront_id)
    if sf is None:
        raise NotFound("Not found")
    return sf
