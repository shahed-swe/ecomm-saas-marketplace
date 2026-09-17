"""Dashboards: tenant, vendor (its own slice only) and platform, plus CSV exports."""

import uuid
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text

from app.core.deps import (
    PlatformAdmin,
    PlatformDB,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import NotFound
from app.core.security import Principal
from app.modules.reporting import service

admin = APIRouter(prefix="/api/v1/admin", tags=["admin:reports"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:reports"])
platform = APIRouter(prefix="/platform/v1", tags=["platform:reports"])

ReportsRead = Annotated[Principal, Depends(require_tenant_staff("reports.read"))]
VendorFinance = Annotated[Principal, Depends(require_vendor_role("finance.read"))]


def _window(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or service._today()
    start = start or end - timedelta(days=29)
    if start > end:
        start, end = end, start
    return start, min(end, service._today())


@admin.get("/reports/overview")
async def tenant_overview(
    p: ReportsRead,
    tenant: Tenant,
    db: TenantDB,
    start: date | None = None,
    end: date | None = None,
):
    start, end = _window(start, end)
    rows = await service.series(db, tenant.id, start=start, end=end)
    previous = await service.series(
        db,
        tenant.id,
        start=start - (end - start) - timedelta(days=1),
        end=start - timedelta(days=1),
    )
    return {
        "start": start,
        "end": end,
        "summary": service.summarise(rows),
        "previous": service.summarise(previous),
        "series": rows,
        "top_products": await service.top_products(db, tenant.id, start=start, end=end),
        "top_vendors": await service.top_vendors(db, tenant.id, start=start, end=end),
    }


@vendor.get("/reports/overview")
async def vendor_overview(
    p: VendorFinance,
    tenant: Tenant,
    db: TenantDB,
    start: date | None = None,
    end: date | None = None,
):
    """The same report, bound to one vendor — a vendor never sees another's numbers."""
    start, end = _window(start, end)
    rows = await service.series(db, tenant.id, start=start, end=end, vendor_id=p.vid)
    return {
        "start": start,
        "end": end,
        "summary": service.summarise(rows),
        "series": rows,
        "top_products": await service.top_products(
            db, tenant.id, start=start, end=end, vendor_id=p.vid
        ),
    }


@admin.post("/reports/exports", status_code=201)
async def create_export(
    request: Request,
    p: ReportsRead,
    tenant: Tenant,
    db: TenantDB,
    kind: Literal["orders", "payouts", "products", "ledger"] = "orders",
    start: date | None = None,
    end: date | None = None,
):
    start, end = _window(start, end)
    storage = request.app.state.private_storage
    out = await service.export_csv(
        db,
        storage,
        tenant.id,
        kind=kind,
        start=start,
        end=end,
        vendor_id=None,
        actor_id=p.sub,
    )
    return {**out, "url": storage.presign_get(out["object_key"])}


@vendor.post("/reports/exports", status_code=201)
async def vendor_export(
    request: Request,
    p: VendorFinance,
    tenant: Tenant,
    db: TenantDB,
    kind: Literal["orders", "payouts", "products"] = "orders",
    start: date | None = None,
    end: date | None = None,
):
    start, end = _window(start, end)
    storage = request.app.state.private_storage
    out = await service.export_csv(
        db,
        storage,
        tenant.id,
        kind=kind,
        start=start,
        end=end,
        vendor_id=p.vid,
        actor_id=p.sub,
    )
    return {**out, "url": storage.presign_get(out["object_key"])}


@admin.get("/reports/exports")
async def list_exports(p: ReportsRead, tenant: Tenant, db: TenantDB, limit: int = 25):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, kind, params, status, rows, created_at, requested_by
                       FROM report_exports WHERE tenant_id = :t ORDER BY created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "l": min(limit, 100)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/reports/exports/{export_id}/url")
async def export_url(
    export_id: uuid.UUID, request: Request, p: ReportsRead, tenant: Tenant, db: TenantDB
):
    key = (
        await db.execute(
            text(
                "SELECT object_key FROM report_exports WHERE tenant_id = :t AND id = :i "
                "AND status = 'ready'"
            ),
            {"t": tenant.id, "i": export_id},
        )
    ).scalar()
    if key is None:
        raise NotFound("Not found")
    return {"url": request.app.state.private_storage.presign_get(key)}


@platform.get("/reports/overview")
async def platform_overview(p: PlatformAdmin, db: PlatformDB, days: int = 30):
    """MRR, GMV fees and tenant health. Platform-only, and 404 to everyone else."""
    return await service.platform_overview(db, days=min(max(days, 1), 365))
