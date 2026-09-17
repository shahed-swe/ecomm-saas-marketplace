"""Return surfaces: buyer requests, vendor decisions and QC, staff overrides, refunds, store credit.

Refund destinations (a bKash number, a bank account) are written encrypted and read back masked;
staff see `••••1234`, and only the refund rail ever sees the real value.
"""

import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.cache_keys import object_key, tkey
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import AppError, Forbidden, NotFound
from app.core.ratelimit import hit
from app.core.security import Principal
from app.modules.returns import credit as store_credit
from app.modules.returns import service

buyer = APIRouter(prefix="/api/v1", tags=["returns"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:returns"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:returns"])

VendorSeller = Annotated[Principal, Depends(require_vendor_role("orders.write", approved=True))]
ReturnsManage = Annotated[Principal, Depends(require_tenant_staff("returns.manage"))]
RefundsApprove = Annotated[Principal, Depends(require_tenant_staff("refunds.approve"))]
CustomersRead = Annotated[Principal, Depends(require_tenant_staff("customers.read"))]

RETURN_SQL = """SELECT r.id, r.number, r.status, r.reason, r.reason_note, r.shipping_payer,
       r.return_shipping_fee, r.refund_method, r.refund_total, r.pickup_courier, r.pickup_consignment_id,
       r.pickup_tracking_url, r.qc_note, r.qc_due_at, r.escalated, r.created_at, r.updated_at,
       o.number AS order_number, s.number AS shipment_number, r.vendor_id, v.display_name AS vendor_name
FROM return_requests r
JOIN orders o ON o.id = r.order_id AND o.tenant_id = r.tenant_id
JOIN sub_orders s ON s.id = r.sub_order_id AND s.tenant_id = r.tenant_id
JOIN vendors v ON v.id = r.vendor_id AND v.tenant_id = r.tenant_id
WHERE r.tenant_id = :t"""
MAX_PHOTO = 6 * 1024 * 1024


def _buyer(p: Principal) -> Principal:
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    return p


def _overrides(app) -> dict:
    return {**getattr(app.state, "payment_gateways", {}), **getattr(app.state, "couriers", {})}


# --------------------------------------------------------------------------------------- buyer
class ReturnItemIn(BaseModel):
    order_item_id: uuid.UUID
    qty: int = Field(ge=1, le=100)


class ReturnIn(BaseModel):
    sub_order_id: uuid.UUID
    reason: Literal[
        "damaged",
        "wrong_item",
        "not_as_described",
        "missing_parts",
        "size_issue",
        "changed_mind",
        "other",
    ]
    items: list[ReturnItemIn] = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=500)
    refund_method: Literal["original", "store_credit", "bkash", "bank"] = "original"
    bkash_number: str | None = None
    bank_account: dict[str, str] | None = None


@buyer.get("/me/orders/{number}/return-window")
async def return_window(number: str, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    """What can still be sent back, per shipment, and until when."""
    _buyer(p)
    subs = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.number, s.status FROM sub_orders s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND o.number = :n AND o.user_id = CAST(:u AS uuid)
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
    out = []
    for sub in subs:
        window = await service.window_for(db, tenant.id, sub["id"])
        out.append(
            {
                "sub_order_id": str(sub["id"]),
                "number": sub["number"],
                "status": sub["status"],
                "returnable": sub["status"] == "delivered" and window.open,
                "window_days": window.days,
                "window_expires_at": window.expires_at,
            }
        )
    return out


@buyer.post("/me/returns", status_code=201)
async def request_return(
    body: ReturnIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    target = None
    if body.refund_method == "bkash":
        from app.core.phone import normalize_bd_phone

        number = normalize_bd_phone(body.bkash_number or "")
        if number is None:
            raise AppError("Enter a valid bKash number", status=422, code="invalid_phone")
        target = {"bkash_number": number}
    elif body.refund_method == "bank":
        if not (body.bank_account or {}).get("account_number"):
            raise AppError("Bank account details are required", status=422, code="invalid_account")
        target = dict(body.bank_account)
    out = await service.create_request(
        db,
        request.app.state.settings,
        tenant.id,
        sub_order_id=body.sub_order_id,
        user_id=p.sub,
        reason=body.reason,
        items=[i.model_dump() for i in body.items],
        note=body.note,
        refund_method=body.refund_method,
        refund_target=target,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.request",
        entity="return",
        entity_id=out["id"],
        data={"reason": body.reason, "number": out["number"]},
        request=request,
    )
    return out


@buyer.get("/me/returns")
async def my_returns(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(RETURN_SQL + " AND r.user_id = CAST(:u AS uuid) ORDER BY r.created_at DESC"),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


class PhotoUrlIn(BaseModel):
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    byte_size: int = Field(gt=0, le=MAX_PHOTO)


@buyer.post("/me/returns/{return_id}/photo-url")
async def photo_upload_url(
    return_id: uuid.UUID,
    body: PhotoUrlIn,
    request: Request,
    p: CurrentPrincipal,
    tenant: Tenant,
    db: TenantDB,
):
    """Evidence goes straight to the private bucket with a short-lived signed PUT — the API never
    handles the bytes, and nothing about a return is ever served publicly."""
    _buyer(p)
    ret = await service.load(db, tenant.id, return_id, user_id=p.sub)
    if ret["status"] not in ("requested", "approved"):
        raise service.ReturnError("This return no longer accepts photos", code="bad_state")
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "return-photo", p.sub), 30, 3600)
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[body.content_type]
    key = object_key(tenant.id, "returns", str(return_id), f"{secrets.token_urlsafe(12)}.{ext}")
    up = request.app.state.private_storage.presign_put(
        key, content_type=body.content_type, max_bytes=body.byte_size
    )
    return {
        "url": up.url,
        "method": up.method,
        "headers": up.headers,
        "storage_key": key,
        "expires_in": up.expires_in,
    }


class PhotoIn(BaseModel):
    storage_key: str = Field(max_length=300)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]


@buyer.post("/me/returns/{return_id}/photos", status_code=201)
async def register_photo(
    return_id: uuid.UUID,
    body: PhotoIn,
    request: Request,
    p: CurrentPrincipal,
    tenant: Tenant,
    db: TenantDB,
):
    _buyer(p)
    await service.load(db, tenant.id, return_id, user_id=p.sub)
    prefix = object_key(tenant.id, "returns", str(return_id), "")
    if not body.storage_key.startswith(prefix):
        raise NotFound("Upload not found")  # a key from another return is simply unknown
    if await request.app.state.private_storage.head(body.storage_key) is None:
        raise NotFound("Upload not found")
    await db.execute(
        text(
            """INSERT INTO return_photos (tenant_id, return_id, object_key, content_type, uploaded_by)
               VALUES (:t, :r, :k, :c, :u)"""
        ),
        {"t": tenant.id, "r": return_id, "k": body.storage_key, "c": body.content_type, "u": p.sub},
    )
    return {"uploaded": True}


@buyer.get("/me/store-credit")
async def my_store_credit(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    return {
        "balance": await store_credit.balance(db, tenant.id, p.sub),
        "history": await store_credit.history(db, tenant.id, p.sub),
    }


# -------------------------------------------------------------------------------------- vendor
class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


class PickupIn(BaseModel):
    courier: Literal["pathao", "steadfast", "redx"] | None = None


class QcIn(BaseModel):
    passed: bool
    results: dict[str, Literal["restock", "write_off", "reject"]] | None = None
    note: str | None = Field(default=None, max_length=300)


@vendor.get("/returns")
async def vendor_returns(
    p: VendorSeller, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    RETURN_SQL
                    + " AND (CAST(:s AS text) IS NULL OR r.status = :s) ORDER BY r.created_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/returns/{return_id}")
async def vendor_return(return_id: uuid.UUID, p: VendorSeller, tenant: Tenant, db: TenantDB):
    return await _detail(db, tenant.id, return_id, vendor_id=p.vid)


@vendor.post("/returns/{return_id}/decision")
async def vendor_decision(
    return_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.decide(
        db,
        tenant.id,
        return_id,
        approve=body.approve,
        actor_id=p.sub,
        note=body.note,
        vendor_id=p.vid,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.decision",
        entity="return",
        entity_id=return_id,
        data={"approve": body.approve},
        request=request,
    )
    return out


@vendor.post("/returns/{return_id}/pickup")
async def vendor_pickup(
    return_id: uuid.UUID,
    body: PickupIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.book_reverse_pickup(
        db,
        request.app.state.settings,
        tenant.id,
        return_id,
        actor_id=p.sub,
        courier=body.courier,
        vendor_id=p.vid,
        overrides=_overrides(request.app),
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.pickup",
        entity="return",
        entity_id=return_id,
        data={"courier": out["courier"]},
        request=request,
    )
    return out


class MarkIn(BaseModel):
    status: Literal["picked_up", "received"]


@vendor.post("/returns/{return_id}/mark")
async def vendor_mark(
    return_id: uuid.UUID,
    body: MarkIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    return await service.mark(
        db, tenant.id, return_id, body.status, actor_id=p.sub, vendor_id=p.vid
    )


@vendor.post("/returns/{return_id}/qc")
async def vendor_qc(
    return_id: uuid.UUID,
    body: QcIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.qc(
        db,
        tenant.id,
        return_id,
        passed=body.passed,
        results=body.results,
        note=body.note,
        actor_id=p.sub,
        vendor_id=p.vid,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.qc",
        entity="return",
        entity_id=return_id,
        data={"passed": body.passed},
        request=request,
    )
    return out


# --------------------------------------------------------------------------------- staff admin
async def _detail(db, tenant_id: str, return_id, *, vendor_id=None, settings=None) -> dict:
    row = await service.load(db, tenant_id, return_id, vendor_id=vendor_id)
    items = (
        (
            await db.execute(
                text(
                    """SELECT ri.id, ri.qty, ri.refund_amount, ri.vat_amount, ri.qc_result,
                              i.title_snapshot, i.sku_snapshot
                       FROM return_items ri
                       JOIN order_items i ON i.id = ri.order_item_id AND i.tenant_id = ri.tenant_id
                       WHERE ri.tenant_id = :t AND ri.return_id = :r"""
                ),
                {"t": tenant_id, "r": return_id},
            )
        )
        .mappings()
        .all()
    )
    photos = (
        (
            await db.execute(
                text(
                    "SELECT object_key FROM return_photos WHERE tenant_id = :t AND return_id = :r "
                    "ORDER BY uploaded_at"
                ),
                {"t": tenant_id, "r": return_id},
            )
        )
        .scalars()
        .all()
    )
    out = {
        k: v
        for k, v in row.items()
        if k not in ("tenant_id", "refund_target_ciphertext", "user_id")
    }
    if settings is not None:
        out["refund_target"] = service.mask_target(
            service.decrypt_target(settings, tenant_id, return_id, row["refund_target_ciphertext"])
        )
    return {**out, "items": [dict(i) for i in items], "photos": list(photos)}


@admin.get("/returns")
async def admin_returns(
    p: ReturnsManage,
    tenant: Tenant,
    db: TenantDB,
    status: str | None = None,
    escalated: bool | None = None,
    limit: int = 50,
):
    rows = (
        (
            await db.execute(
                text(
                    RETURN_SQL
                    + """ AND (CAST(:s AS text) IS NULL OR r.status = :s)
                          AND (CAST(:e AS boolean) IS NULL OR r.escalated = :e)
                        ORDER BY r.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "e": escalated, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/returns/{return_id}")
async def admin_return(
    return_id: uuid.UUID, request: Request, p: ReturnsManage, tenant: Tenant, db: TenantDB
):
    return await _detail(db, tenant.id, return_id, settings=request.app.state.settings)


@admin.post("/returns/{return_id}/decision")
async def admin_decision(
    return_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    p: ReturnsManage,
    tenant: Tenant,
    db: TenantDB,
):
    """Tenant staff can overrule a vendor — that is what an override is for."""
    out = await service.decide(
        db, tenant.id, return_id, approve=body.approve, actor_id=p.sub, note=body.note
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.override",
        entity="return",
        entity_id=return_id,
        data={"approve": body.approve, "note": body.note},
        request=request,
    )
    return out


@admin.post("/returns/{return_id}/refund")
async def admin_refund(
    return_id: uuid.UUID, request: Request, p: RefundsApprove, tenant: Tenant, db: TenantDB
):
    out = await service.refund(
        db,
        request.app.state.settings,
        tenant.id,
        return_id,
        actor_id=p.sub,
        overrides=_overrides(request.app),
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="refund.issue",
        entity="return",
        entity_id=return_id,
        data={"method": out["method"], "amount": str(out["amount"])},
        request=request,
    )
    return out


class ManualRefundIn(BaseModel):
    reference: str = Field(min_length=3, max_length=80)


@admin.get("/refunds")
async def list_refunds(
    p: RefundsApprove, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT f.id, f.method, f.amount, f.status, f.provider_ref, f.failure_reason,
                              f.created_at, f.completed_at, o.number AS order_number, r.number AS return_number
                       FROM refunds f
                       JOIN orders o ON o.id = f.order_id AND o.tenant_id = f.tenant_id
                       LEFT JOIN return_requests r ON r.id = f.return_id AND r.tenant_id = f.tenant_id
                       WHERE f.tenant_id = :t AND (CAST(:s AS text) IS NULL OR f.status = :s)
                       ORDER BY f.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.post("/refunds/{refund_id}/complete")
async def complete_refund(
    refund_id: uuid.UUID,
    body: ManualRefundIn,
    request: Request,
    p: RefundsApprove,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.complete_manual_refund(
        db, tenant.id, refund_id, reference=body.reference, actor_id=p.sub
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="refund.complete",
        entity="refund",
        entity_id=refund_id,
        data={"reference": body.reference},
        request=request,
    )
    return out


@admin.get("/credit-notes")
async def credit_notes(p: ReturnsManage, tenant: Tenant, db: TenantDB, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT c.number, c.amount, c.vat_amount, c.reason, c.issued_at,
                              s.number AS shipment_number, v.display_name AS vendor_name
                       FROM credit_notes c
                       JOIN sub_orders s ON s.id = c.sub_order_id AND s.tenant_id = c.tenant_id
                       JOIN vendors v ON v.id = c.vendor_id AND v.tenant_id = c.tenant_id
                       WHERE c.tenant_id = :t ORDER BY c.issued_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


class CreditIn(BaseModel):
    user_id: uuid.UUID
    amount: float = Field(gt=0)
    note: str = Field(min_length=3, max_length=200)


@admin.post("/store-credit")
async def grant_credit(
    body: CreditIn, request: Request, p: RefundsApprove, tenant: Tenant, db: TenantDB
):
    """Goodwill credit: always deliberate, always attributed, never silent."""
    from decimal import Decimal

    balance = await store_credit.grant(
        db,
        tenant.id,
        str(body.user_id),
        Decimal(str(body.amount)),
        reason="goodwill",
        actor_id=p.sub,
        note=body.note,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="store_credit.grant",
        entity="user",
        entity_id=body.user_id,
        data={"amount": body.amount, "note": body.note},
        request=request,
    )
    return {"balance": balance}


@admin.get("/store-credit")
async def customer_credit(
    p: CustomersRead, tenant: Tenant, db: TenantDB, user_id: uuid.UUID | None = None
):
    if user_id is None:
        rows = (
            (
                await db.execute(
                    text(
                        """SELECT a.user_id, a.balance, u.email, a.updated_at
                           FROM store_credit_accounts a JOIN users u ON u.id = a.user_id
                           WHERE a.tenant_id = :t AND a.balance > 0 ORDER BY a.balance DESC LIMIT 100"""
                    ),
                    {"t": tenant.id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]
    return {
        "balance": await store_credit.balance(db, tenant.id, str(user_id)),
        "history": await store_credit.history(db, tenant.id, str(user_id)),
    }


@admin.post("/returns/{return_id}/cancel", status_code=204)
async def cancel_return(
    return_id: uuid.UUID, request: Request, p: ReturnsManage, tenant: Tenant, db: TenantDB
):
    await service.mark(db, tenant.id, return_id, "cancelled", actor_id=p.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="return.cancel",
        entity="return",
        entity_id=return_id,
        request=request,
    )
    return Response(status_code=204)
