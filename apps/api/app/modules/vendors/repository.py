from sqlalchemy import Select, select

from app.core.repository import TenantScopedRepository, VendorScopedRepository
from app.modules.vendors.models import Vendor, VendorStorefront


class VendorRepository(TenantScopedRepository[Vendor]):
    """Tenant-staff view of vendors in one tenant."""

    model = Vendor

    async def house_vendor(self) -> Vendor | None:
        stmt: Select = self._select().where(Vendor.is_house.is_(True))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def slug_taken(self, slug: str) -> bool:
        stmt = select(Vendor.id).where(Vendor.tenant_id == self.tenant_id, Vendor.slug == slug)
        return (await self.session.execute(stmt)).first() is not None


class StorefrontRepository(VendorScopedRepository[VendorStorefront]):
    model = VendorStorefront

    async def mine(self) -> VendorStorefront | None:
        return (await self.session.execute(self._select())).scalar_one_or_none()


class TenantStorefrontRepository(TenantScopedRepository[VendorStorefront]):
    model = VendorStorefront
