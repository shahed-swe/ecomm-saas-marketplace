import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.cache_keys import tkey
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.phone import normalize_bd_phone
from app.core.ratelimit import hit
from app.core.security import Principal
from app.modules.checkout import service

buyer = APIRouter(prefix="/api/v1", tags=["checkout"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:orders"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:orders"])

VendorSeller = Annotated[Principal, Depends(require_vendor_role("orders.write", approved=True))]
Marketing = Annotated[Principal, Depends(require_tenant_staff("marketing.write"))]
Settings_ = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]
OrdersRead = Annotated[Principal, Depends(require_tenant_staff("orders.read"))]


def _buyer(p: Principal) -> Principal:
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    return p


# ------------------------------------------------------------------------------------------ geography
@buyer.get("/geo/districts")
async def districts(db: TenantDB, _: Tenant):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT d.code, d.name_en, v.code AS division, v.name_en AS division_name, v.name_bn AS division_bn, d.zone
           FROM geo_districts d JOIN geo_divisions v ON v.code = d.division_code ORDER BY v.name_en, d.name_en"""
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------------------------------- cart
class CartItemIn(BaseModel):
    variant_id: uuid.UUID
    qty: int = Field(ge=1, le=20)


class QtyIn(BaseModel):
    qty: int = Field(ge=0, le=20)


async def _cart_id(db, tenant_id: str, user_id: str) -> uuid.UUID:
    cid = (
        await db.execute(
            text("SELECT id FROM carts WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant_id, "u": user_id},
        )
    ).scalar()
    if cid is None:
        cid = (
            await db.execute(
                text(
                    """INSERT INTO carts (tenant_id, user_id) VALUES (:t, :u)
               ON CONFLICT (tenant_id, user_id) WHERE user_id IS NOT NULL DO UPDATE SET updated_at = now() RETURNING id"""
                ),
                {"t": tenant_id, "u": user_id},
            )
        ).scalar()
    return cid


async def _items(db, tenant_id: str, cart_id) -> list[tuple[str, int]]:
    return [
        (str(r.variant_id), r.qty)
        for r in (
            await db.execute(
                text(
                    "SELECT variant_id, qty FROM cart_items WHERE tenant_id = :t AND cart_id = :c ORDER BY added_at"
                ),
                {"t": tenant_id, "c": cart_id},
            )
        )
    ]


class QuoteIn(BaseModel):
    district_code: str | None = Field(default=None, pattern=r"^[a-z-]{2,40}$")
    coupons: dict[str, str] = Field(default_factory=dict, max_length=20)


@buyer.get("/cart")
async def get_cart(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    cid = await _cart_id(db, tenant.id, p.sub)
    items = await _items(db, tenant.id, cid)
    if not items:
        return {"items": [], "quote": None}
    q = await service.build_quote(db, tenant.id, items, district=None, coupons={}, user_id=p.sub)
    return {"items": [{"variant_id": v, "qty": n} for v, n in items], "quote": q.as_dict()}


@buyer.post("/cart/items", status_code=201)
async def add_to_cart(
    body: CartItemIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "cart", p.sub), 120, 60)
    ok = (
        await db.execute(
            text(
                """SELECT 1 FROM product_variants pv JOIN products p ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id
           JOIN vendors v ON v.id = pv.vendor_id AND v.tenant_id = pv.tenant_id
           WHERE pv.id = :v AND pv.tenant_id = :t AND pv.is_active AND p.status = 'active'
             AND p.moderation_status = 'approved' AND v.status = 'approved'"""
            ),
            {"v": body.variant_id, "t": tenant.id},
        )
    ).first()
    if not ok:
        raise NotFound("Product not found")
    cid = await _cart_id(db, tenant.id, p.sub)
    count = (
        await db.execute(
            text("SELECT count(*) FROM cart_items WHERE tenant_id = :t AND cart_id = :c"),
            {"t": tenant.id, "c": cid},
        )
    ).scalar()
    if count >= 50:
        raise AppError("Your cart is full", status=422, code="cart_full")
    await db.execute(
        text(
            """INSERT INTO cart_items (tenant_id, cart_id, variant_id, qty) VALUES (:t, :c, :v, :q)
           ON CONFLICT (tenant_id, cart_id, variant_id) DO UPDATE SET qty = LEAST(20, cart_items.qty + EXCLUDED.qty)"""
        ),
        {"t": tenant.id, "c": cid, "v": body.variant_id, "q": body.qty},
    )
    return {"cart_id": str(cid)}


@buyer.patch("/cart/items/{variant_id}", status_code=204)
async def set_qty(
    variant_id: uuid.UUID, body: QtyIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    cid = await _cart_id(db, tenant.id, p.sub)
    if body.qty == 0:
        await db.execute(
            text(
                "DELETE FROM cart_items WHERE tenant_id = :t AND cart_id = :c AND variant_id = :v"
            ),
            {"t": tenant.id, "c": cid, "v": variant_id},
        )
    else:
        await db.execute(
            text(
                "UPDATE cart_items SET qty = :q WHERE tenant_id = :t AND cart_id = :c AND variant_id = :v"
            ),
            {"q": body.qty, "t": tenant.id, "c": cid, "v": variant_id},
        )


@buyer.post("/cart/quote")
async def quote(body: QuoteIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    cid = await _cart_id(db, tenant.id, p.sub)
    q = await service.build_quote(
        db,
        tenant.id,
        await _items(db, tenant.id, cid),
        district=body.district_code,
        coupons=body.coupons,
        user_id=p.sub,
    )
    return q.as_dict()


# ---------------------------------------------------------------------------------------- addresses
class AddressIn(BaseModel):
    label: str | None = Field(default=None, max_length=30)
    recipient_name: str = Field(min_length=2, max_length=80)
    phone: str = Field(max_length=20)
    district_code: str = Field(pattern=r"^[a-z-]{2,40}$")
    upazila: str = Field(min_length=2, max_length=60)
    area: str | None = Field(default=None, max_length=80)
    address_line: str = Field(min_length=5, max_length=200)
    landmark: str | None = Field(default=None, max_length=120)
    is_default: bool = False


def _address(body: AddressIn) -> dict:
    phone = normalize_bd_phone(body.phone)
    if phone is None:
        raise AppError("Enter a valid Bangladeshi mobile number", status=422, code="invalid_phone")
    return {**body.model_dump(), "phone": phone}


@buyer.get("/me/addresses")
async def list_addresses(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM addresses WHERE tenant_id = :t AND user_id = :u ORDER BY is_default DESC, created_at"
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            k: (str(v) if k in ("id", "tenant_id", "user_id") else v)
            for k, v in r.items()
            if k != "tenant_id"
        }
        for r in rows
    ]


@buyer.post("/me/addresses", status_code=201)
async def add_address(body: AddressIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    data = _address(body)
    if not (
        await db.execute(
            text("SELECT 1 FROM geo_districts WHERE code = :d"), {"d": data["district_code"]}
        )
    ).first():
        raise AppError("Unknown district", status=422, code="invalid_district")
    count = (
        await db.execute(
            text("SELECT count(*) FROM addresses WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant.id, "u": p.sub},
        )
    ).scalar()
    if count >= 10:
        raise AppError("You can save up to 10 addresses", status=422, code="too_many_addresses")
    if data["is_default"] or count == 0:
        await db.execute(
            text("UPDATE addresses SET is_default = false WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant.id, "u": p.sub},
        )
        data["is_default"] = True
    aid = (
        await db.execute(
            text(
                """INSERT INTO addresses (tenant_id, user_id, label, recipient_name, phone, district_code, upazila, area,
               address_line, landmark, is_default)
           VALUES (:t, :u, :label, :recipient_name, :phone, :district_code, :upazila, :area, :address_line, :landmark,
                   :is_default) RETURNING id"""
            ),
            {"t": tenant.id, "u": p.sub, **data},
        )
    ).scalar()
    return {"id": str(aid)}


@buyer.delete("/me/addresses/{address_id}", status_code=204)
async def delete_address(address_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    res = await db.execute(
        text("DELETE FROM addresses WHERE id = :a AND tenant_id = :t AND user_id = :u"),
        {"a": address_id, "t": tenant.id, "u": p.sub},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")


# ----------------------------------------------------------------------------------------- checkout
class PlaceIn(BaseModel):
    address_id: uuid.UUID | None = None
    address: AddressIn | None = None
    payment_method: Literal["cod", "bkash", "sslcommerz"]
    coupons: dict[str, str] = Field(default_factory=dict, max_length=20)
    expected_total: Decimal = Field(ge=0)
    contact_email: EmailStr | None = None


@buyer.post("/checkout/place", status_code=201)
async def place(
    body: PlaceIn,
    request: Request,
    p: CurrentPrincipal,
    tenant: Tenant,
    db: TenantDB,
    idempotency_key: str = Header(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
):
    _buyer(p)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "checkout", p.sub), 20, 600)
    if body.address_id:
        addr = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM addresses WHERE id = :a AND tenant_id = :t AND user_id = :u"
                    ),
                    {"a": body.address_id, "t": tenant.id, "u": p.sub},
                )
            )
            .mappings()
            .first()
        )
        if addr is None:
            raise NotFound("Address not found")
        address = {
            k: addr[k]
            for k in (
                "recipient_name",
                "phone",
                "district_code",
                "upazila",
                "area",
                "address_line",
                "landmark",
            )
        }
    elif body.address:
        address = {
            k: v for k, v in _address(body.address).items() if k not in ("label", "is_default")
        }
    else:
        raise AppError("Choose a delivery address", status=422, code="address_required")
    phone = (
        (
            await db.execute(
                text("SELECT phone, phone_verified_at FROM users WHERE id = :u AND tenant_id = :t"),
                {"u": p.sub, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    if body.payment_method == "cod" and not phone["phone_verified_at"]:
        raise CheckoutError_(
            "Verify your mobile number to use cash on delivery", "phone_unverified"
        )
    cid = await _cart_id(db, tenant.id, p.sub)
    items = await _items(db, tenant.id, cid)
    try:
        order, token = await service.place_order(
            db,
            tenant.id,
            user_id=p.sub,
            items=items,
            address=address,
            contact_phone=phone["phone"] or address["phone"],
            contact_email=body.contact_email,
            payment_method=body.payment_method,
            coupons=body.coupons,
            expected_total=body.expected_total,
            idempotency_key=idempotency_key,
        )
    except IntegrityError as exc:
        raise Conflict("Order could not be placed, please retry") from exc
    if not order["replayed"]:
        await audit.record(
            db,
            tenant_id=tenant.id,
            actor=p,
            action="order.place",
            entity="order",
            entity_id=order["id"],
            data={"number": order["number"], "method": body.payment_method},
            request=request,
        )
    return {**order, "tracking_token": token}


def CheckoutError_(msg: str, code: str) -> service.CheckoutError:  # noqa: N802
    return service.CheckoutError(msg, code=code)


# ------------------------------------------------------------------------------------------ orders
ORDER_SQL = """SELECT o.id, o.number, o.payment_method, o.items_subtotal, o.discount_total, o.shipping_total, o.vat_total,
       o.grand_total, o.placed_at, o.payment_due_at, o.shipping_address,
       derive_order_status(array_agg(s.status)) AS status,
       json_agg(json_build_object('id', s.id, 'number', s.number, 'vendor_id', s.vendor_id, 'vendor_name', v.display_name,
                                  'status', s.status, 'total', s.total) ORDER BY s.number) AS shipments
FROM orders o JOIN sub_orders s ON s.order_id = o.id AND s.tenant_id = o.tenant_id
JOIN vendors v ON v.id = s.vendor_id AND v.tenant_id = s.tenant_id
WHERE o.tenant_id = :t {where}
GROUP BY o.id"""


@buyer.get("/me/orders")
async def my_orders(
    p: CurrentPrincipal, tenant: Tenant, db: TenantDB, limit: int = Query(20, ge=1, le=50)
):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    ORDER_SQL.format(where="AND o.user_id = :u")
                    + " ORDER BY o.placed_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "u": p.sub, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@buyer.get("/me/orders/{number}")
async def my_order(number: str, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    row = (
        (
            await db.execute(
                text(ORDER_SQL.format(where="AND o.user_id = :u AND o.number = :n")),
                {"t": tenant.id, "u": p.sub, "n": number},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Order not found")
    items = (
        (
            await db.execute(
                text(
                    """SELECT i.sub_order_id, i.title_snapshot, i.sku_snapshot, i.options_snapshot, i.unit_price, i.qty,
                  i.discount_amount, i.line_total FROM order_items i JOIN sub_orders s ON s.id = i.sub_order_id
                  AND s.tenant_id = i.tenant_id WHERE s.order_id = :o AND i.tenant_id = :t ORDER BY i.title_snapshot"""
                ),
                {"o": row["id"], "t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(row), "items": [dict(i) for i in items]}


@buyer.get("/orders/track")
async def track(
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    number: str = Query(max_length=40),
    token: str = Query(min_length=16, max_length=64),
):
    """Guest-safe tracking link: order number + unguessable token; shows status only, no address or phone."""
    await hit(
        request.app.state.redis,
        tkey(tenant.id, "rl", "track", request.client.host if request.client else "x"),
        60,
        600,
    )
    row = (
        (
            await db.execute(
                text(ORDER_SQL.format(where="AND o.number = :n AND o.tracking_token_hash = :h")),
                {"t": tenant.id, "n": number, "h": service._hash(token)},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Order not found")
    return {
        "number": row["number"],
        "status": row["status"],
        "placed_at": row["placed_at"],
        "shipments": [
            {k: s[k] for k in ("number", "vendor_name", "status")} for s in row["shipments"]
        ],
    }


# ------------------------------------------------------------------------------------------- vendor
class CouponIn(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{3,30}$")
    kind: Literal["percent", "fixed"]
    value: Decimal = Field(gt=0)
    min_subtotal: Decimal = Field(default=Decimal("0"), ge=0)
    max_discount: Decimal | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    usage_limit: int | None = Field(default=None, gt=0)
    per_buyer_limit: int = Field(default=1, gt=0, le=100)


@vendor.post("/coupons", status_code=201)
async def create_coupon(
    body: CouponIn, request: Request, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    if body.kind == "percent" and body.value > 90:
        raise AppError("A percentage coupon can be at most 90%", status=422, code="invalid_coupon")
    try:
        async with db.begin_nested():
            cid = (
                await db.execute(
                    text(
                        """INSERT INTO coupons (tenant_id, vendor_id, code, kind, value, min_subtotal, max_discount, starts_at,
                       ends_at, usage_limit, per_buyer_limit)
                   VALUES (:t, :v, :code, :kind, :value, :min, :max, coalesce(:s, now()), :e, :ul, :pb) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "v": p.vid,
                        "code": body.code,
                        "kind": body.kind,
                        "value": body.value,
                        "min": body.min_subtotal,
                        "max": body.max_discount,
                        "s": body.starts_at,
                        "e": body.ends_at,
                        "ul": body.usage_limit,
                        "pb": body.per_buyer_limit,
                    },
                )
            ).scalar()
    except IntegrityError as exc:
        raise Conflict("Coupon code unavailable") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="coupon.create",
        entity="coupon",
        entity_id=cid,
        data={"code": body.code},
        request=request,
    )
    return {"id": str(cid)}


@vendor.get("/coupons")
async def my_coupons(p: VendorSeller, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM coupons WHERE tenant_id = :t AND vendor_id = :v ORDER BY created_at DESC"
                ),
                {"t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .all()
    )
    return [
        {k: (str(x) if k in ("id", "tenant_id", "vendor_id") else x) for k, x in r.items()}
        for r in rows
    ]


@vendor.post("/coupons/{coupon_id}/deactivate", status_code=204)
async def deactivate_coupon(
    coupon_id: uuid.UUID, request: Request, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    res = await db.execute(
        text(
            "UPDATE coupons SET is_active = false WHERE id = :c AND tenant_id = :t AND vendor_id = :v"
        ),
        {"c": coupon_id, "t": tenant.id, "v": p.vid},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="coupon.deactivate",
        entity="coupon",
        entity_id=coupon_id,
        request=request,
    )


class JoinCampaignIn(BaseModel):
    variant_id: uuid.UUID
    campaign_price: Decimal = Field(gt=0)
    stock_cap: int = Field(gt=0, le=100_000)


@vendor.get("/campaigns")
async def open_campaigns(p: VendorSeller, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, slug, name, starts_at, ends_at, max_discount_percent FROM campaigns WHERE tenant_id = :t "
                    "AND status = 'scheduled' AND ends_at > now() ORDER BY starts_at"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [{**dict(r), "id": str(r["id"])} for r in rows]


@vendor.post("/campaigns/{campaign_id}/products", status_code=201)
async def join_campaign(
    campaign_id: uuid.UUID,
    body: JoinCampaignIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    c = (
        await db.execute(
            text(
                "SELECT max_discount_percent, ends_at FROM campaigns WHERE id = :c AND tenant_id = :t "
                "AND status = 'scheduled'"
            ),
            {"c": campaign_id, "t": tenant.id},
        )
    ).first()
    if c is None:
        raise NotFound("Campaign not found")
    v = (
        await db.execute(
            text(
                "SELECT price, stock_on_hand FROM product_variants WHERE id = :v AND tenant_id = :t AND vendor_id = :vid"
            ),
            {"v": body.variant_id, "t": tenant.id, "vid": p.vid},
        )
    ).first()
    if v is None:
        raise NotFound("Product not found")
    if body.campaign_price >= v.price:
        raise AppError(
            "Campaign price must be lower than the regular price", status=422, code="invalid_price"
        )
    if (v.price - body.campaign_price) / v.price * 100 > c.max_discount_percent:
        raise AppError(
            f"Discount can be at most {c.max_discount_percent}%", status=422, code="invalid_price"
        )
    try:
        async with db.begin_nested():
            cp = (
                await db.execute(
                    text(
                        """INSERT INTO campaign_products (tenant_id, campaign_id, vendor_id, variant_id, campaign_price, stock_cap)
                   VALUES (:t, :c, :v, :var, :price, :cap) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "c": campaign_id,
                        "v": p.vid,
                        "var": body.variant_id,
                        "price": body.campaign_price,
                        "cap": body.stock_cap,
                    },
                )
            ).scalar()
    except IntegrityError as exc:
        raise Conflict("Already in this campaign") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="campaign.join",
        entity="campaign_product",
        entity_id=cp,
        data={"price": str(body.campaign_price)},
        request=request,
    )
    return {"id": str(cp)}


@vendor.get("/orders")
async def vendor_orders(
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=100),
):
    sql = """SELECT s.id, s.number, s.status, s.items_subtotal, s.discount_total, s.shipping_fee, s.total, s.created_at,
                    o.payment_method, o.shipping_address->>'district_code' AS district
             FROM sub_orders s JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
             WHERE s.tenant_id = :t AND s.vendor_id = :v"""
    params: dict = {"t": tenant.id, "v": p.vid, "l": limit}
    if status:
        sql += " AND s.status = :s"
        params["s"] = status
    rows = (
        (await db.execute(text(sql + " ORDER BY s.created_at DESC LIMIT :l"), params))
        .mappings()
        .all()
    )
    return [{**dict(r), "id": str(r["id"])} for r in rows]


@vendor.get("/orders/{sub_order_id}")
async def vendor_order(sub_order_id: uuid.UUID, p: VendorSeller, tenant: Tenant, db: TenantDB):
    s = (
        (
            await db.execute(
                text(
                    """SELECT s.*, o.payment_method, o.shipping_address, o.contact_phone FROM sub_orders s
           JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
           WHERE s.id = :s AND s.tenant_id = :t AND s.vendor_id = :v"""
                ),
                {"s": sub_order_id, "t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .first()
    )
    if s is None:
        raise NotFound("Not found")
    items = (
        (
            await db.execute(
                text(
                    "SELECT title_snapshot, sku_snapshot, options_snapshot, unit_price, qty, discount_amount, "
                    "line_total FROM order_items WHERE sub_order_id = :s AND tenant_id = :t"
                ),
                {"s": sub_order_id, "t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    out = {
        k: (str(v) if isinstance(v, uuid.UUID) else v)
        for k, v in s.items()
        if k not in ("tenant_id",)
    }
    # other vendors in the same order are never exposed: only this sub-order
    return {**out, "items": [dict(i) for i in items]}


# -------------------------------------------------------------------------------------------- admin
class CampaignIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{2,60}$")
    name: str = Field(min_length=2, max_length=80)
    starts_at: datetime
    ends_at: datetime
    max_discount_percent: Decimal = Field(default=Decimal("90"), gt=0, le=90)


@admin.post("/campaigns", status_code=201)
async def create_campaign(
    body: CampaignIn, request: Request, actor: Marketing, tenant: Tenant, db: TenantDB
):
    if body.ends_at <= body.starts_at:
        raise AppError("End must be after start", status=422, code="invalid_dates")
    try:
        async with db.begin_nested():
            cid = (
                await db.execute(
                    text(
                        """INSERT INTO campaigns (tenant_id, slug, name, starts_at, ends_at, max_discount_percent)
                   VALUES (:t, :s, :n, :st, :e, :m) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "s": body.slug,
                        "n": body.name,
                        "st": body.starts_at,
                        "e": body.ends_at,
                        "m": body.max_discount_percent,
                    },
                )
            ).scalar()
    except IntegrityError as exc:
        raise Conflict("Slug already used") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="campaign.create",
        entity="campaign",
        entity_id=cid,
        data={"slug": body.slug},
        request=request,
    )
    return {"id": str(cid)}


@admin.post("/campaigns/{campaign_id}/cancel", status_code=204)
async def cancel_campaign(
    campaign_id: uuid.UUID, request: Request, actor: Marketing, tenant: Tenant, db: TenantDB
):
    res = await db.execute(
        text("UPDATE campaigns SET status = 'cancelled' WHERE id = :c AND tenant_id = :t"),
        {"c": campaign_id, "t": tenant.id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="campaign.cancel",
        entity="campaign",
        entity_id=campaign_id,
        request=request,
    )


class ShippingRateIn(BaseModel):
    vendor_id: uuid.UUID | None = None
    zone: Literal["inside_dhaka", "dhaka_suburb", "outside_dhaka"]
    base_fee: Decimal = Field(ge=0)
    base_weight_grams: int = Field(default=1000, gt=0)
    per_extra_kg: Decimal = Field(default=Decimal("0"), ge=0)
    free_over: Decimal | None = Field(default=None, gt=0)
    free_funded_by: Literal["vendor", "tenant"] = "vendor"


@admin.put("/shipping-rates", status_code=204)
async def upsert_rate(
    body: ShippingRateIn, request: Request, actor: Settings_, tenant: Tenant, db: TenantDB
):
    if (
        body.vendor_id
        and not (
            await db.execute(
                text("SELECT 1 FROM vendors WHERE id = :v AND tenant_id = :t"),
                {"v": body.vendor_id, "t": tenant.id},
            )
        ).first()
    ):
        raise NotFound("Vendor not found")
    await db.execute(
        text(
            """DELETE FROM shipping_rates WHERE tenant_id = :t AND zone = :z
             AND coalesce(vendor_id, tenant_id) = coalesce(CAST(:v AS uuid), tenant_id)"""
        ),
        {"t": tenant.id, "z": body.zone, "v": body.vendor_id},
    )
    await db.execute(
        text(
            """INSERT INTO shipping_rates (tenant_id, vendor_id, zone, base_fee, base_weight_grams, per_extra_kg, free_over,
               free_funded_by) VALUES (:t, :v, :z, :b, :bw, :pk, :fo, :ff)"""
        ),
        {
            "t": tenant.id,
            "v": body.vendor_id,
            "z": body.zone,
            "b": body.base_fee,
            "bw": body.base_weight_grams,
            "pk": body.per_extra_kg,
            "fo": body.free_over,
            "ff": body.free_funded_by,
        },
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="shipping_rate.set",
        entity="shipping_rate",
        data=body.model_dump(mode="json"),
        request=request,
    )


class CheckoutSettingsIn(BaseModel):
    cod_enabled: bool | None = None
    cod_max_order: Decimal | None = Field(default=None, ge=0)
    cod_blocked_districts: list[str] | None = Field(default=None, max_length=64)
    vat_registered: bool | None = None
    bin: str | None = Field(default=None, pattern=r"^\d{9,13}$")
    vat_pricing: Literal["inclusive", "exclusive"] | None = None
    default_vat_rate: Decimal | None = Field(default=None, ge=0, lt=1)
    reservation_minutes: int | None = Field(default=None, ge=5, le=120)


@admin.patch("/checkout-settings", status_code=204)
async def checkout_settings(
    body: CheckoutSettingsIn, request: Request, actor: Settings_, tenant: Tenant, db: TenantDB
):
    data = body.model_dump(exclude_unset=True)
    statements = {
        k: f"UPDATE tenant_settings SET {k} = :val WHERE tenant_id = :t"
        for k in CheckoutSettingsIn.model_fields
    }
    for k, v in data.items():
        await db.execute(text(statements[k]), {"val": v, "t": tenant.id})
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="checkout_settings.update",
        entity="tenant",
        entity_id=tenant.id,
        data={k: str(v) for k, v in data.items()},
        request=request,
    )


class TaxRateIn(BaseModel):
    category_id: uuid.UUID
    rate: Decimal = Field(ge=0, lt=1)


@admin.put("/tax-rates", status_code=204)
async def set_tax_rate(
    body: TaxRateIn, request: Request, actor: Settings_, tenant: Tenant, db: TenantDB
):
    if not (
        await db.execute(
            text("SELECT 1 FROM categories WHERE id = :c AND tenant_id = :t"),
            {"c": body.category_id, "t": tenant.id},
        )
    ).first():
        raise NotFound("Category not found")
    await db.execute(
        text("""INSERT INTO tax_rates (tenant_id, category_id, rate) VALUES (:t, :c, :r)
                             ON CONFLICT (tenant_id, category_id) DO UPDATE SET rate = EXCLUDED.rate"""),
        {"t": tenant.id, "c": body.category_id, "r": body.rate},
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="tax_rate.set",
        entity="category",
        entity_id=body.category_id,
        data={"rate": str(body.rate)},
        request=request,
    )


@admin.get("/orders")
async def admin_orders(
    _: OrdersRead, tenant: Tenant, db: TenantDB, limit: int = Query(50, ge=1, le=200)
):
    rows = (
        (
            await db.execute(
                text(ORDER_SQL.format(where="") + " ORDER BY o.placed_at DESC LIMIT :l"),
                {"t": tenant.id, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
