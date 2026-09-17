import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.deps import PlatformAdmin, PlatformDB, Tenant, TenantDB, require_tenant_staff
from app.core.errors import NotFound
from app.core.security import Principal
from app.core.tenancy import invalidate_host
from app.modules.billing import service

platform = APIRouter(prefix="/platform/v1", tags=["platform:billing"])
admin = APIRouter(prefix="/api/v1/admin/billing", tags=["admin:billing"])
Owner = Annotated[Principal, Depends(require_tenant_staff("*"))]


class PlanOut(BaseModel):
    code: str
    name: str
    monthly_price: Decimal
    yearly_price: Decimal
    gmv_fee_rate: Decimal
    setup_fee: Decimal
    trial_days: int
    limits: dict


class SubscriptionPatch(BaseModel):
    plan_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,30}$")
    interval: Literal["monthly", "yearly"] | None = None
    price_override: Decimal | None = Field(default=None, ge=0)
    gmv_fee_rate_override: Decimal | None = Field(default=None, ge=0, lt=1)


class InvoiceLineOut(BaseModel):
    kind: str
    description: str
    quantity: Decimal
    unit_amount: Decimal
    amount: Decimal


class InvoiceOut(BaseModel):
    id: uuid.UUID
    number: str
    period_start: date
    period_end: date
    status: str
    subtotal: Decimal
    vat_amount: Decimal
    total: Decimal
    due_date: date
    payment_method: str | None
    payment_reference: str | None
    lines: list[InvoiceLineOut] = []


class MarkPaidIn(BaseModel):
    method: Literal["bank", "bkash", "sslcommerz", "manual"]
    reference: str = Field(min_length=3, max_length=120)


class CycleIn(BaseModel):
    today: date | None = None
    vat_rate: Decimal = Field(default=Decimal("0"), ge=0, lt=1)


async def _invoice(db, invoice_id, tenant_id: str | None = None) -> InvoiceOut | None:
    sql = "SELECT * FROM platform_invoices WHERE id = :i"
    params = {"i": invoice_id}
    if tenant_id is not None:
        sql += " AND tenant_id = :t"
        params["t"] = tenant_id
    row = (await db.execute(text(sql), params)).mappings().first()
    if row is None:
        return None
    lines = (
        (
            await db.execute(
                text(
                    "SELECT kind, description, quantity, unit_amount, amount "
                    "FROM platform_invoice_lines WHERE invoice_id = :i ORDER BY kind"
                ),
                {"i": row["id"]},
            )
        )
        .mappings()
        .all()
    )
    return InvoiceOut(
        **{k: row[k] for k in InvoiceOut.model_fields if k in row},
        lines=[InvoiceLineOut(**line) for line in lines],
    )


# ---- platform ----------------------------------------------------------------------------------------
@platform.get("/plans", response_model=list[PlanOut])
async def plans(_: PlatformAdmin, db: PlatformDB):
    rows = (await db.execute(text("SELECT * FROM plans ORDER BY monthly_price"))).mappings().all()
    return [PlanOut(**{k: r[k] for k in PlanOut.model_fields}) for r in rows]


@platform.patch("/tenants/{tenant_id}/subscription")
async def patch_subscription(
    tenant_id: str, body: SubscriptionPatch, request: Request, _: PlatformAdmin, db: PlatformDB
):
    data = body.model_dump(exclude_unset=True)
    if (
        "plan_code" in data
        and not (
            await db.execute(text("SELECT 1 FROM plans WHERE code=:c"), {"c": data["plan_code"]})
        ).first()
    ):
        raise NotFound("Plan not found")
    if data:
        # column names come from the fixed SubscriptionPatch fields, never from user input
        sets = ", ".join(f"{k} = :{k}" for k in data if k in SubscriptionPatch.model_fields)
        stmt = (
            "UPDATE tenant_subscriptions SET " + sets + ", updated_at = now() WHERE tenant_id = :t"  # noqa: S608
        )
        res = await db.execute(text(stmt), {**data, "t": tenant_id})
        if res.rowcount == 0:
            raise NotFound("Not found")
    ent = await service.entitlements(db, tenant_id)
    return {"plan_code": ent.plan_code, "status": ent.status, "limits": ent.limits}


@platform.get("/tenants/{tenant_id}/invoices", response_model=list[InvoiceOut])
async def tenant_invoices(tenant_id: str, _: PlatformAdmin, db: PlatformDB):
    ids = (
        (
            await db.execute(
                text(
                    "SELECT id FROM platform_invoices WHERE tenant_id = :t ORDER BY period_start DESC"
                ),
                {"t": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    return [await _invoice(db, i) for i in ids]


@platform.post("/invoices/{invoice_id}/mark-paid")
async def mark_paid(
    invoice_id: uuid.UUID, body: MarkPaidIn, request: Request, _: PlatformAdmin, db: PlatformDB
):
    out = await service.mark_paid(db, str(invoice_id), method=body.method, reference=body.reference)
    await _flush_hosts(request, db, invoice_id=invoice_id)
    return out


@platform.post("/billing/run-cycle")
async def run_cycle(body: CycleIn, request: Request, _: PlatformAdmin, db: PlatformDB):
    out = await service.run_billing_cycle(db, body.today, body.vat_rate)
    hosts = (await db.execute(text("SELECT host FROM domains"))).scalars().all()
    await invalidate_host(request.app.state.redis, *hosts)
    return out


async def _flush_hosts(request, db, invoice_id):
    hosts = (
        (
            await db.execute(
                text("""SELECT d.host FROM domains d JOIN platform_invoices i
                                      ON i.tenant_id = d.tenant_id WHERE i.id = :i"""),
                {"i": invoice_id},
            )
        )
        .scalars()
        .all()
    )
    await invalidate_host(request.app.state.redis, *hosts)


# ---- tenant owner -----------------------------------------------------------------------------------------
class BillingOverview(BaseModel):
    plan_code: str
    status: str
    limits: dict
    usage: dict
    open_invoices: int


@admin.get("", response_model=BillingOverview)
async def overview(_: Owner, tenant: Tenant, db: TenantDB):
    ent = await service.entitlements(db, tenant.id)
    usage = (
        (
            await db.execute(
                text(
                    """SELECT (SELECT count(*) FROM vendors WHERE tenant_id = :t AND NOT is_house
                    AND status NOT IN ('closed','rejected')) AS vendors,
                  (SELECT count(*) FROM staff_members WHERE tenant_id = :t AND status = 'active') AS staff,
                  (SELECT count(*) FROM domains WHERE tenant_id = :t AND kind = 'custom') AS custom_domains"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    open_n = (
        await db.execute(
            text("SELECT count(*) FROM platform_invoices WHERE tenant_id = :t AND status='open'"),
            {"t": tenant.id},
        )
    ).scalar()
    return BillingOverview(
        plan_code=ent.plan_code,
        status=ent.status,
        limits=ent.limits,
        usage=dict(usage),
        open_invoices=open_n,
    )


@admin.get("/invoices", response_model=list[InvoiceOut])
async def my_invoices(_: Owner, tenant: Tenant, db: TenantDB):
    ids = (
        (
            await db.execute(
                text(
                    "SELECT id FROM platform_invoices WHERE tenant_id = :t ORDER BY period_start DESC"
                ),
                {"t": tenant.id},
            )
        )
        .scalars()
        .all()
    )
    return [await _invoice(db, i, tenant.id) for i in ids]


@admin.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def my_invoice(invoice_id: str, _: Owner, tenant: Tenant, db: TenantDB):
    try:
        iid = uuid.UUID(invoice_id)
    except ValueError as exc:
        raise NotFound("Not found") from exc
    inv = await _invoice(db, iid, tenant.id)
    if inv is None:
        raise NotFound("Not found")
    return inv
