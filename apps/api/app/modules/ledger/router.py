"""Money surfaces: the tenant's books, vendor balances and statements, payout batches, tax docs."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import Forbidden, NotFound
from app.core.security import Principal
from app.modules.ledger import documents, payouts
from app.modules.ledger import service as ledger

admin = APIRouter(prefix="/api/v1/admin", tags=["admin:finance"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:finance"])
buyer = APIRouter(prefix="/api/v1", tags=["finance"])

FinanceRead = Annotated[Principal, Depends(require_tenant_staff("finance.read"))]
PayoutsRead = Annotated[Principal, Depends(require_tenant_staff("payouts.read"))]
PayoutsPrepare = Annotated[Principal, Depends(require_tenant_staff("payouts.prepare"))]
PayoutsApprove = Annotated[Principal, Depends(require_tenant_staff("payouts.approve"))]
VendorFinance = Annotated[Principal, Depends(require_vendor_role("finance.read"))]

ENTRY_SQL = """SELECT e.id, e.group_id, e.account, e.vendor_id, e.direction, e.amount, e.entry_type,
       e.ref_type, e.ref_id, e.memo, e.created_at
FROM ledger_entries e WHERE e.tenant_id = :t"""


# ------------------------------------------------------------------------------------ the books
@admin.get("/ledger")
async def entries(
    p: FinanceRead,
    tenant: Tenant,
    db: TenantDB,
    account: str | None = None,
    entry_type: str | None = None,
    vendor_id: uuid.UUID | None = None,
    limit: int = 100,
):
    rows = (
        (
            await db.execute(
                text(
                    ENTRY_SQL
                    + """ AND (CAST(:a AS text) IS NULL OR e.account = :a)
                          AND (CAST(:e AS text) IS NULL OR e.entry_type = :e)
                          AND (CAST(:v AS uuid) IS NULL OR e.vendor_id = :v)
                        ORDER BY e.created_at DESC, e.id LIMIT :l"""
                ),
                {
                    "t": tenant.id,
                    "a": account,
                    "e": entry_type,
                    "v": vendor_id,
                    "l": min(limit, 500),
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/ledger/trial-balance")
async def trial_balance(p: FinanceRead, tenant: Tenant, db: TenantDB, since: date | None = None):
    """Debits must equal credits. If they ever do not, something is very wrong — say so loudly."""
    return await ledger.trial_balance(db, tenant.id, since=since)


@admin.get("/ledger/reconciliation")
async def reconciliation(p: FinanceRead, tenant: Tenant, db: TenantDB):
    """Ledger vs payments, couriers, refunds and wallets. Drift is shown, never silently fixed."""
    from app.modules.ledger import reconcile

    return await reconcile.reconcile(db, tenant.id)


@admin.get("/vendor-balances")
async def vendor_balances(p: PayoutsRead, tenant: Tenant, db: TenantDB):
    vendors = (
        (
            await db.execute(
                text(
                    "SELECT id, display_name FROM vendors WHERE tenant_id = :t AND status = 'approved' "
                    "ORDER BY display_name"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    out = []
    for v in vendors:
        statement = await ledger.vendor_statement(db, tenant.id, str(v["id"]))
        out.append({"vendor_id": str(v["id"]), "vendor_name": v["display_name"], **statement})
    return out


@vendor.get("/balance")
async def my_balance(p: VendorFinance, tenant: Tenant, db: TenantDB):
    return await ledger.vendor_statement(db, tenant.id, p.vid)


@vendor.get("/ledger")
async def my_ledger(p: VendorFinance, tenant: Tenant, db: TenantDB, limit: int = 100):
    """A vendor sees its own entries only — RLS says so, and so does this query."""
    rows = (
        (
            await db.execute(
                text(
                    ENTRY_SQL
                    + " AND e.vendor_id = CAST(:v AS uuid) ORDER BY e.created_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "v": p.vid, "l": min(limit, 500)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/payouts")
async def my_payouts(p: VendorFinance, tenant: Tenant, db: TenantDB, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT l.period_end, l.gross, l.tds, l.net, l.method, l.account_last4,
                              l.status, l.reference, l.paid_at, b.status AS batch_status
                       FROM payout_lines l
                       JOIN payout_batches b ON b.id = l.batch_id AND b.tenant_id = l.tenant_id
                       WHERE l.tenant_id = :t AND l.vendor_id = CAST(:v AS uuid)
                       ORDER BY l.period_end DESC LIMIT :l"""
                ),
                {"t": tenant.id, "v": p.vid, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# -------------------------------------------------------------------------------------- payouts
class BatchIn(BaseModel):
    period_end: date | None = None


@admin.post("/payout-batches", status_code=201)
async def build_batch(
    body: BatchIn, request: Request, p: PayoutsPrepare, tenant: Tenant, db: TenantDB
):
    schedule = (
        await db.execute(
            text("SELECT payout_schedule FROM tenant_settings WHERE tenant_id = :t"),
            {"t": tenant.id},
        )
    ).scalar()
    period_end = body.period_end or payouts.period_end_for(schedule, date.today())
    out = await payouts.build_batch(db, tenant.id, period_end=period_end, actor_id=p.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payout_batch.build",
        entity="payout_batch",
        entity_id=out["id"],
        data={"period_end": str(period_end), "lines": out["line_count"]},
        request=request,
    )
    return out


@admin.get("/payout-batches")
async def list_batches(p: PayoutsRead, tenant: Tenant, db: TenantDB, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM payout_batches WHERE tenant_id = :t ORDER BY period_end DESC LIMIT :l"
                ),
                {"t": tenant.id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [{k: v for k, v in r.items() if k != "tenant_id"} for r in rows]


@admin.get("/payout-batches/{batch_id}")
async def batch_detail(batch_id: uuid.UUID, p: PayoutsRead, tenant: Tenant, db: TenantDB):
    batch = (
        (
            await db.execute(
                text("SELECT * FROM payout_batches WHERE tenant_id = :t AND id = :i"),
                {"t": tenant.id, "i": batch_id},
            )
        )
        .mappings()
        .first()
    )
    if batch is None:
        raise NotFound("Not found")
    lines = (
        (
            await db.execute(
                text(
                    """SELECT l.id, l.gross, l.tds, l.net, l.method, l.account_last4, l.account_name,
                              l.status, l.reference, l.failure_reason, l.paid_at, v.display_name AS vendor_name
                       FROM payout_lines l JOIN vendors v ON v.id = l.vendor_id AND v.tenant_id = l.tenant_id
                       WHERE l.tenant_id = :t AND l.batch_id = :b ORDER BY v.display_name"""
                ),
                {"t": tenant.id, "b": batch_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        **{k: v for k, v in batch.items() if k != "tenant_id"},
        "lines": [dict(x) for x in lines],
    }


@admin.post("/payout-batches/{batch_id}/approve")
async def approve_batch(
    batch_id: uuid.UUID, request: Request, p: PayoutsApprove, tenant: Tenant, db: TenantDB
):
    out = await payouts.approve(db, tenant.id, batch_id, actor_id=p.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payout_batch.approve",
        entity="payout_batch",
        entity_id=batch_id,
        request=request,
    )
    return out


@admin.get("/payout-batches/{batch_id}/export")
async def export_batch(
    batch_id: uuid.UUID,
    request: Request,
    p: PayoutsApprove,
    tenant: Tenant,
    db: TenantDB,
    method: Literal["bank", "bkash"] = "bank",
):
    csv_text = await payouts.export(db, tenant.id, batch_id, method=method, actor_id=p.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payout_batch.export",
        entity="payout_batch",
        entity_id=batch_id,
        data={"method": method},
        request=request,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="payout-{batch_id}-{method}.csv"'},
    )


class PaidIn(BaseModel):
    reference: str = Field(min_length=3, max_length=80)


class FailedIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@admin.post("/payout-lines/{line_id}/paid")
async def mark_line_paid(
    line_id: uuid.UUID,
    body: PaidIn,
    request: Request,
    p: PayoutsApprove,
    tenant: Tenant,
    db: TenantDB,
):
    out = await payouts.mark_paid(db, tenant.id, line_id, reference=body.reference, actor_id=p.sub)
    if not out.get("already"):
        await audit.record(
            db,
            tenant_id=tenant.id,
            actor=p,
            action="payout_line.paid",
            entity="payout_line",
            entity_id=line_id,
            data={"reference": body.reference},
            request=request,
        )
    return out


@admin.post("/payout-lines/{line_id}/failed")
async def mark_line_failed(
    line_id: uuid.UUID,
    body: FailedIn,
    request: Request,
    p: PayoutsApprove,
    tenant: Tenant,
    db: TenantDB,
):
    out = await payouts.mark_failed(db, tenant.id, line_id, reason=body.reason, actor_id=p.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payout_line.failed",
        entity="payout_line",
        entity_id=line_id,
        data={"reason": body.reason},
        request=request,
    )
    return out


# --------------------------------------------------------------------------------- tax documents
class DocumentIn(BaseModel):
    sub_order_id: uuid.UUID
    kind: Literal["invoice", "credit_note"] = "invoice"


@admin.post("/tax-documents")
async def issue_document(
    body: DocumentIn, request: Request, p: FinanceRead, tenant: Tenant, db: TenantDB
):
    out = await documents.issue(
        db,
        request.app.state.private_storage,
        tenant.id,
        sub_order_id=body.sub_order_id,
        kind=body.kind,
    )
    return {
        **out,
        "url": request.app.state.private_storage.presign_get(out["document_key"]),
    }


@admin.get("/tax-documents")
async def list_documents(
    p: FinanceRead, tenant: Tenant, db: TenantDB, order_number: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT i.number, i.kind, i.taxable_amount, i.vat_amount, i.seller_bin, i.issued_at,
                              s.number AS shipment_number, o.number AS order_number,
                              v.display_name AS vendor_name
                       FROM tax_invoices i
                       JOIN sub_orders s ON s.id = i.sub_order_id AND s.tenant_id = i.tenant_id
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       JOIN vendors v ON v.id = i.vendor_id AND v.tenant_id = i.tenant_id
                       WHERE i.tenant_id = :t AND (CAST(:n AS text) IS NULL OR o.number = :n)
                       ORDER BY i.issued_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "n": order_number, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@buyer.get("/me/orders/{number}/invoices")
async def my_invoices(
    number: str, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    """A buyer's own tax invoices, generated on first ask and served by a short-lived signed URL."""
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    subs = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.number, s.status FROM sub_orders s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND o.number = :n AND o.user_id = CAST(:u AS uuid)
                         AND s.status IN ('delivered','shipped','ready_to_ship','processing','confirmed')
                       ORDER BY s.number"""
                ),
                {"t": tenant.id, "n": number, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    if not subs:
        raise NotFound("Order not found")
    storage = request.app.state.private_storage
    out = []
    for sub in subs:
        doc = await documents.issue(db, storage, tenant.id, sub_order_id=sub["id"])
        out.append(
            {
                "shipment_number": sub["number"],
                "number": doc["number"],
                "vat_amount": doc["vat_amount"],
                "url": storage.presign_get(doc["document_key"]),
            }
        )
    return out
