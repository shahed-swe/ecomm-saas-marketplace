"""Tenant lifecycle (architecture §12). Runs on the platform DB role, only from app/platform."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, NotFound
from app.modules.platform.models import Domain, Tenant, TenantSettings
from app.modules.platform.schemas import RESERVED_SLUGS, TenantCreate, TenantPatch
from app.modules.vendors.models import Vendor, VendorStorefront


async def create_tenant(db: AsyncSession, settings: Settings, body: TenantCreate):
    if body.slug in RESERVED_SLUGS:
        raise Conflict("Slug unavailable")
    tenant = Tenant(
        slug=body.slug,
        name=body.name,
        store_mode=body.store_mode,
        default_locale=body.default_locale,
    )
    db.add(tenant)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("Slug unavailable") from exc
    host = f"{body.slug}.{settings.platform_root_domain}"
    db.add(
        Domain(
            tenant_id=tenant.id,
            host=host,
            kind="subdomain",
            status="active",
            is_primary=True,
            verified_at=func.now(),
        )
    )
    db.add(TenantSettings(tenant_id=tenant.id))
    # ADR 0002: every tenant has exactly one house vendor, in both store modes.
    house = Vendor(
        tenant_id=tenant.id, slug="house", display_name=body.name, status="approved", is_house=True
    )
    db.add(house)
    await db.flush()
    db.add(VendorStorefront(tenant_id=tenant.id, vendor_id=house.id))
    await db.flush()
    await db.refresh(tenant)
    return tenant, host, house.id


async def patch_tenant(db: AsyncSession, tenant_id: str, body: TenantPatch) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("store_mode") == "single" and tenant.store_mode == "multi":
        others = await db.scalar(
            select(func.count())
            .select_from(Vendor)
            .where(
                Vendor.tenant_id == tenant.id,
                Vendor.is_house.is_(False),
                Vendor.status.notin_(("closed", "rejected")),
            )
        )
        if others:
            raise Conflict("Close other vendors before switching to single mode")
    for k, v in data.items():
        setattr(tenant, k, v)
    await db.flush()
    await db.refresh(tenant)
    return tenant
