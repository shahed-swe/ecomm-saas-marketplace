"""Scoped repositories: there is no way to build one without its scope (ADR 0001, layer 1).

Filters are applied here, never in routers. RLS (layer 2) is the backstop.
"""

import uuid
from typing import Any, ClassVar, Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base

M = TypeVar("M", bound=Base)


def _uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError("scope id must be a UUID") from exc


class TenantScopedRepository(Generic[M]):
    model: ClassVar[type]

    def __init__(self, session: AsyncSession, tenant_id: Any):
        if tenant_id is None:
            raise ValueError("tenant_id is required")
        self.session = session
        self.tenant_id = _uuid(tenant_id)

    def _select(self) -> Select:
        return select(self.model).where(self.model.tenant_id == self.tenant_id)

    async def get(self, id_: Any) -> M | None:
        try:
            pk = _uuid(id_)
        except ValueError:
            return None
        return (
            await self.session.execute(self._select().where(self.model.id == pk))
        ).scalar_one_or_none()

    async def list(self, limit: int = 50) -> list[M]:
        stmt = self._select().order_by(self.model.id.desc()).limit(min(limit, 200))
        return list((await self.session.execute(stmt)).scalars())

    def add(self, obj: M) -> M:
        if getattr(obj, "tenant_id", None) not in (None, self.tenant_id):
            raise ValueError("object belongs to another tenant")
        obj.tenant_id = self.tenant_id
        self.session.add(obj)
        return obj


class VendorScopedRepository(TenantScopedRepository[M]):
    def __init__(self, session: AsyncSession, tenant_id: Any, vendor_id: Any):
        super().__init__(session, tenant_id)
        if vendor_id is None:
            raise ValueError("vendor_id is required")
        self.vendor_id = _uuid(vendor_id)

    def _select(self) -> Select:
        return super()._select().where(self.model.vendor_id == self.vendor_id)

    def add(self, obj: M) -> M:
        if getattr(obj, "vendor_id", None) not in (None, self.vendor_id):
            raise ValueError("object belongs to another vendor")
        obj.vendor_id = self.vendor_id
        return super().add(obj)
