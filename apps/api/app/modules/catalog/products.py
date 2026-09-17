"""Products, variants, stock, media, moderation, public catalog, Q&A.

Stock correctness: every change takes a row lock on the variant, the DB CHECK keeps stock >= 0,
and an append-only inventory_movements row records the delta and resulting balance.
"""

import base64
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.security import Principal
from app.modules.billing.service import require_quota
from app.modules.catalog.revalidate import product_tags
from app.modules.catalog.taxonomy import attributes_for_category, validate_attributes

vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:catalog"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:moderation"])
public = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

VendorCatalog = Annotated[Principal, Depends(require_vendor_role("catalog.write", approved=True))]
Moderator = Annotated[Principal, Depends(require_tenant_staff("catalog.moderate"))]
SLUG = r"^[a-z0-9](?:[a-z0-9-]{0,118}[a-z0-9])?$"
_TAGS = re.compile(r"<[^>]+>")


def clean_text(value: str) -> str:
    """Descriptions are plain text/markdown. HTML tags are stripped on write (rendered escaped)."""
    return _TAGS.sub("", value).replace("\x00", "").strip()


def signature(options: dict[str, str]) -> str:
    return (
        "|".join(
            f"{k.strip().lower()}={str(v).strip().lower()}" for k, v in sorted(options.items())
        )
        or "default"
    )


# ------------------------------------------------------------------------------------------------ schemas
class VariantIn(BaseModel):
    sku: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    options: dict[str, str] = Field(default_factory=dict, max_length=3)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    compare_at_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    stock: int = Field(default=0, ge=0, le=1_000_000)
    weight_grams: int | None = Field(default=None, gt=0, le=100_000)
    barcode: str | None = Field(default=None, max_length=40)


class ProductIn(BaseModel):
    slug: str = Field(pattern=SLUG)
    title_en: str = Field(min_length=3, max_length=200)
    title_bn: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=20000)
    category_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    attributes: dict = Field(default_factory=dict)
    weight_grams: int | None = Field(default=None, gt=0, le=100_000)
    variants: list[VariantIn] = Field(min_length=1, max_length=100)

    @field_validator("description")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v)


class ProductPatch(BaseModel):
    title_en: str | None = Field(default=None, min_length=3, max_length=200)
    title_bn: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    brand_id: uuid.UUID | None = None
    attributes: dict | None = None
    weight_grams: int | None = Field(default=None, gt=0, le=100_000)
    status: Literal["draft", "active", "archived"] | None = None

    @field_validator("description")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return clean_text(v) if v is not None else v


class VariantPatch(BaseModel):
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    compare_at_price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    is_active: bool | None = None
    weight_grams: int | None = Field(default=None, gt=0, le=100_000)


class StockIn(BaseModel):
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    reason: Literal["manual", "adjustment", "return"] = "manual"
    note: str | None = Field(default=None, max_length=120)

    @field_validator("delta")
    @classmethod
    def _nonzero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("delta cannot be zero")
        return v


class VariantOut(BaseModel):
    id: uuid.UUID
    sku: str
    options: dict
    price: Decimal
    compare_at_price: Decimal | None
    stock_on_hand: int
    available: int
    is_active: bool


class MediaOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    position: int
    renditions: dict
    blur_data: str | None
    width: int | None
    height: int | None
    alt_text: str | None


class ProductOut(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    slug: str
    title_en: str
    title_bn: str | None
    description: str
    category_id: uuid.UUID
    brand_id: uuid.UUID | None
    attributes: dict
    status: str
    moderation_status: str
    moderation_reason: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    in_stock: bool
    updated_at: datetime
    variants: list[VariantOut] = []
    media: list[MediaOut] = []


# -------------------------------------------------------------------------------------------- helpers
async def _refresh_rollups(db, tenant_id: str, product_id) -> None:
    await db.execute(
        text(
            """UPDATE products p SET min_price = r.mn, max_price = r.mx, in_stock = r.stock, updated_at = now(),
               search_vector = setweight(to_tsvector('simple', coalesce(p.title_en,'') || ' ' || coalesce(p.title_bn,'')), 'A')
                            || setweight(to_tsvector('simple', coalesce(b.name,'')), 'B')
                            || setweight(to_tsvector('simple', left(coalesce(p.description,''), 2000)), 'C')
           FROM (SELECT min(price) mn, max(price) mx, bool_or(stock_on_hand - stock_reserved > 0) stock
                 FROM product_variants WHERE tenant_id = :t AND product_id = :p AND is_active) r
           LEFT JOIN brands b ON b.tenant_id = :t AND b.id = (SELECT brand_id FROM products WHERE id = :p AND tenant_id = :t)
           WHERE p.id = :p AND p.tenant_id = :t"""
        ),
        {"t": tenant_id, "p": product_id},
    )


async def load_product(
    db, tenant_id: str, product_id, vendor_id: str | None = None
) -> ProductOut | None:
    sql = "SELECT * FROM products WHERE id = :p AND tenant_id = :t"
    params = {"p": product_id, "t": tenant_id}
    if vendor_id:
        sql += " AND vendor_id = :v"
        params["v"] = vendor_id
    row = (await db.execute(text(sql), params)).mappings().first()
    if row is None:
        return None
    variants = (
        (
            await db.execute(
                text(
                    """SELECT id, sku, options, price, compare_at_price, stock_on_hand, stock_on_hand - stock_reserved AS available,
                  is_active FROM product_variants WHERE tenant_id = :t AND product_id = :p ORDER BY created_at"""
                ),
                {"t": tenant_id, "p": row["id"]},
            )
        )
        .mappings()
        .all()
    )
    media = (
        (
            await db.execute(
                text(
                    """SELECT pm.id, pm.asset_id, pm.position, a.renditions, a.blur_data, a.width, a.height, a.alt_text
           FROM product_media pm JOIN media_assets a ON a.id = pm.asset_id AND a.tenant_id = pm.tenant_id
           WHERE pm.tenant_id = :t AND pm.product_id = :p AND a.status = 'ready' ORDER BY pm.position"""
                ),
                {"t": tenant_id, "p": row["id"]},
            )
        )
        .mappings()
        .all()
    )
    return ProductOut(
        **{k: row[k] for k in ProductOut.model_fields if k in row},
        variants=[VariantOut(**v) for v in variants],
        media=[MediaOut(**m) for m in media],
    )


async def _vendor_product(db, tenant_id: str, vendor_id: str, product_id) -> dict:
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM products WHERE id = :p AND tenant_id = :t AND vendor_id = :v FOR UPDATE"
                ),
                {"p": product_id, "t": tenant_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return dict(row)


async def _needs_moderation(db, tenant_id: str, vendor_id: str) -> bool:
    mode = (
        await db.execute(
            text("SELECT moderation_mode FROM tenant_settings WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar()
    if mode == "none":
        return False
    if mode == "all":
        return True
    house = (
        await db.execute(
            text("SELECT is_house FROM vendors WHERE id = :v AND tenant_id = :t"),
            {"v": vendor_id, "t": tenant_id},
        )
    ).scalar()
    if house:
        return False
    approved = (
        await db.execute(
            text(
                "SELECT 1 FROM products WHERE tenant_id = :t AND vendor_id = :v AND moderation_status = 'approved' LIMIT 1"
            ),
            {"t": tenant_id, "v": vendor_id},
        )
    ).first()
    return approved is None


async def _insert_variant(
    db, tenant_id: str, vendor_id: str, product_id, v: VariantIn, actor: str
) -> uuid.UUID:
    vid = (
        await db.execute(
            text(
                """INSERT INTO product_variants (tenant_id, vendor_id, product_id, sku, options, option_signature, price,
               compare_at_price, stock_on_hand, weight_grams, barcode)
           VALUES (:t, :v, :p, :sku, CAST(:o AS jsonb), :sig, :price, :cmp, :stock, :w, :bc) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": vendor_id,
                "p": product_id,
                "sku": v.sku,
                "o": json.dumps(v.options),
                "sig": signature(v.options),
                "price": v.price,
                "cmp": v.compare_at_price,
                "stock": v.stock,
                "w": v.weight_grams,
                "bc": v.barcode,
            },
        )
    ).scalar()
    if v.stock:
        await db.execute(
            text(
                """INSERT INTO inventory_movements (tenant_id, vendor_id, variant_id, delta, balance_after, reason, actor_id)
               VALUES (:t, :v, :var, :d, :d, 'manual', :a)"""
            ),
            {"t": tenant_id, "v": vendor_id, "var": vid, "d": v.stock, "a": actor},
        )
    return vid


# ---------------------------------------------------------------------------------------------- vendor
@vendor.post("/products", response_model=ProductOut, status_code=201)
async def create_product(
    body: ProductIn, request: Request, p: VendorCatalog, tenant: Tenant, db: TenantDB
):
    for v in body.variants:
        if v.compare_at_price is not None and v.compare_at_price <= v.price:
            raise AppError(
                "Compare-at price must be higher than the price", status=422, code="invalid_price"
            )
    count = (
        await db.execute(
            text("SELECT count(*) FROM products WHERE tenant_id = :t AND status <> 'archived'"),
            {"t": tenant.id},
        )
    ).scalar()
    await require_quota(db, tenant.id, "products", count)
    cat = (
        await db.execute(
            text("SELECT id FROM categories WHERE id = :c AND tenant_id = :t AND is_active"),
            {"c": body.category_id, "t": tenant.id},
        )
    ).first()
    if cat is None:
        raise NotFound("Category not found")
    if (
        body.brand_id
        and not (
            await db.execute(
                text("SELECT 1 FROM brands WHERE id = :b AND tenant_id = :t"),
                {"b": body.brand_id, "t": tenant.id},
            )
        ).first()
    ):
        raise NotFound("Brand not found")
    attrs = validate_attributes(
        await attributes_for_category(db, tenant.id, body.category_id),
        body.attributes,
        require_complete=False,
    )
    sigs = [signature(v.options) for v in body.variants]
    if len(set(sigs)) != len(sigs):
        raise AppError("Two variants have the same options", status=422, code="duplicate_variant")
    try:
        async with db.begin_nested():
            pid = (
                await db.execute(
                    text(
                        """INSERT INTO products (tenant_id, vendor_id, category_id, brand_id, slug, title_en, title_bn,
                       description, attributes, weight_grams)
                   VALUES (:t, :v, :c, :b, :s, :en, :bn, :d, CAST(:a AS jsonb), :w) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "v": p.vid,
                        "c": body.category_id,
                        "b": body.brand_id,
                        "s": body.slug,
                        "en": body.title_en,
                        "bn": body.title_bn,
                        "d": body.description,
                        "a": json.dumps(attrs),
                        "w": body.weight_grams,
                    },
                )
            ).scalar()
            for v in body.variants:
                await _insert_variant(db, tenant.id, p.vid, pid, v, p.sub)
    except IntegrityError as exc:
        # generic on purpose: never reveal that another vendor already uses a slug or SKU
        raise Conflict("Product link or SKU unavailable") from exc
    await _refresh_rollups(db, tenant.id, pid)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="product.create",
        entity="product",
        entity_id=pid,
        data={"slug": body.slug, "variants": len(body.variants)},
        request=request,
    )
    return await load_product(db, tenant.id, pid, p.vid)


@vendor.get("/products", response_model=list[ProductOut])
async def my_products(
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=100),
):
    sql = "SELECT id FROM products WHERE tenant_id = :t AND vendor_id = :v"
    params: dict = {"t": tenant.id, "v": p.vid, "l": limit}
    if status:
        sql += " AND status = :s"
        params["s"] = status
    ids = (
        (await db.execute(text(sql + " ORDER BY updated_at DESC LIMIT :l"), params)).scalars().all()
    )
    return [await load_product(db, tenant.id, i, p.vid) for i in ids]


@vendor.get("/products/{product_id}", response_model=ProductOut)
async def my_product(product_id: uuid.UUID, p: VendorCatalog, tenant: Tenant, db: TenantDB):
    out = await load_product(db, tenant.id, product_id, p.vid)
    if out is None:
        raise NotFound("Not found")
    return out


CONTENT_FIELDS = {"title_en", "title_bn", "description", "attributes", "brand_id"}


@vendor.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductPatch,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    current = await _vendor_product(db, tenant.id, p.vid, product_id)
    data = body.model_dump(exclude_unset=True)
    if "attributes" in data:
        data["attributes"] = validate_attributes(
            await attributes_for_category(db, tenant.id, current["category_id"]),
            data["attributes"],
            require_complete=False,
        )
    if (
        "brand_id" in data
        and data["brand_id"]
        and not (
            await db.execute(
                text("SELECT 1 FROM brands WHERE id = :b AND tenant_id = :t"),
                {"b": data["brand_id"], "t": tenant.id},
            )
        ).first()
    ):
        raise NotFound("Brand not found")
    new_status = data.get("status", current["status"])
    moderation = current["moderation_status"]
    if new_status == "active":
        attrs = data.get("attributes", current["attributes"])
        validate_attributes(
            await attributes_for_category(db, tenant.id, current["category_id"]),
            attrs,
            require_complete=True,
        )
        media = (
            await db.execute(
                text("SELECT count(*) FROM product_media WHERE tenant_id = :t AND product_id = :p"),
                {"t": tenant.id, "p": product_id},
            )
        ).scalar()
        if media == 0:
            raise AppError(
                "Add at least one photo before publishing", status=422, code="photo_required"
            )
        content_changed = bool(CONTENT_FIELDS & data.keys())
        mode = (
            await db.execute(
                text("SELECT moderation_mode FROM tenant_settings WHERE tenant_id = :t"),
                {"t": tenant.id},
            )
        ).scalar()
        if moderation != "approved" or (content_changed and mode == "all"):
            moderation = (
                "pending"
                if await _needs_moderation(db, tenant.id, p.vid) or mode == "all"
                else "approved"
            )
    sets = {
        "title_en": data.get("title_en", current["title_en"]),
        "title_bn": data.get("title_bn", current["title_bn"]),
        "description": data.get("description", current["description"]),
        "brand_id": data.get("brand_id", current["brand_id"]),
        "attributes": json.dumps(data.get("attributes", current["attributes"])),
        "weight_grams": data.get("weight_grams", current["weight_grams"]),
        "status": new_status,
        "moderation_status": moderation,
    }
    await db.execute(
        text(
            """UPDATE products SET title_en = :title_en, title_bn = :title_bn, description = :description,
               brand_id = :brand_id, attributes = CAST(:attributes AS jsonb), weight_grams = :weight_grams,
               status = :status, moderation_status = :moderation_status,
               moderation_reason = CASE WHEN :moderation_status = 'pending' THEN NULL ELSE moderation_reason END,
               published_at = CASE WHEN :status = 'active' AND :moderation_status = 'approved'
                                   THEN coalesce(published_at, now()) ELSE published_at END,
               updated_at = now()
           WHERE id = :p AND tenant_id = :t AND vendor_id = :v"""
        ),
        {**sets, "p": product_id, "t": tenant.id, "v": p.vid},
    )
    await _refresh_rollups(db, tenant.id, product_id)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="product.update",
        entity="product",
        entity_id=product_id,
        data={k: str(v) for k, v in data.items()},
        request=request,
    )
    await request.app.state.revalidator.revalidate(
        tenant.id,
        product_tags(
            tenant.id,
            product_slug=current["slug"],
            category_ids=[current["category_id"]],
            vendor_id=p.vid,
        ),
    )
    return await load_product(db, tenant.id, product_id, p.vid)


@vendor.post("/products/{product_id}/variants", response_model=VariantOut, status_code=201)
async def add_variant(
    product_id: uuid.UUID,
    body: VariantIn,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    current = await _vendor_product(db, tenant.id, p.vid, product_id)
    try:
        async with db.begin_nested():
            vid = await _insert_variant(db, tenant.id, p.vid, product_id, body, p.sub)
    except IntegrityError as exc:
        raise Conflict("SKU or option combination unavailable") from exc
    await _refresh_rollups(db, tenant.id, product_id)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="variant.create",
        entity="variant",
        entity_id=vid,
        data={"sku": body.sku},
        request=request,
    )
    await request.app.state.revalidator.revalidate(
        tenant.id, product_tags(tenant.id, product_slug=current["slug"])
    )
    row = (
        (
            await db.execute(
                text(
                    """SELECT id, sku, options, price, compare_at_price, stock_on_hand, stock_on_hand - stock_reserved AS available,
                  is_active FROM product_variants WHERE id = :i AND tenant_id = :t"""
                ),
                {"i": vid, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return VariantOut(**row)


async def _vendor_variant(db, tenant_id: str, vendor_id: str, variant_id) -> dict:
    row = (
        (
            await db.execute(
                text(
                    """SELECT v.*, p.slug AS product_slug, p.category_id FROM product_variants v
           JOIN products p ON p.id = v.product_id AND p.tenant_id = v.tenant_id
           WHERE v.id = :i AND v.tenant_id = :t AND v.vendor_id = :v FOR UPDATE OF v"""
                ),
                {"i": variant_id, "t": tenant_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return dict(row)


@vendor.patch("/variants/{variant_id}", response_model=VariantOut)
async def update_variant(
    variant_id: uuid.UUID,
    body: VariantPatch,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    v = await _vendor_variant(db, tenant.id, p.vid, variant_id)
    data = body.model_dump(exclude_unset=True)
    price = data.get("price", v["price"])
    cmp = data.get("compare_at_price", v["compare_at_price"])
    if cmp is not None and cmp <= price:
        raise AppError(
            "Compare-at price must be higher than the price", status=422, code="invalid_price"
        )
    await db.execute(
        text(
            """UPDATE product_variants SET price = :price, compare_at_price = :cmp, is_active = :act, weight_grams = :w,
               updated_at = now() WHERE id = :i AND tenant_id = :t AND vendor_id = :v"""
        ),
        {
            "price": price,
            "cmp": cmp,
            "act": data.get("is_active", v["is_active"]),
            "w": data.get("weight_grams", v["weight_grams"]),
            "i": variant_id,
            "t": tenant.id,
            "v": p.vid,
        },
    )
    await _refresh_rollups(db, tenant.id, v["product_id"])
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="variant.update",
        entity="variant",
        entity_id=variant_id,
        data={k: str(x) for k, x in data.items()},
        request=request,
    )
    # a price change is exactly the stale value buyers notice: always invalidate
    await request.app.state.revalidator.revalidate(
        tenant.id,
        product_tags(
            tenant.id,
            product_slug=v["product_slug"],
            category_ids=[v["category_id"]],
            vendor_id=p.vid,
        ),
    )
    row = (
        (
            await db.execute(
                text(
                    """SELECT id, sku, options, price, compare_at_price, stock_on_hand, stock_on_hand - stock_reserved AS available,
                  is_active FROM product_variants WHERE id = :i AND tenant_id = :t"""
                ),
                {"i": variant_id, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return VariantOut(**row)


async def adjust_stock(
    db,
    *,
    tenant_id: str,
    vendor_id: str,
    variant_id,
    delta: int,
    reason: str,
    actor: str,
    ref: str | None = None,
) -> int:
    row = (
        await db.execute(
            text(
                "SELECT stock_on_hand, stock_reserved FROM product_variants WHERE id = :i AND tenant_id = :t AND vendor_id = :v FOR UPDATE"
            ),
            {"i": variant_id, "t": tenant_id, "v": vendor_id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    new = row.stock_on_hand + delta
    if new < row.stock_reserved:
        raise Conflict("Stock cannot go below reserved quantity")
    await db.execute(
        text(
            "UPDATE product_variants SET stock_on_hand = :n, updated_at = now() WHERE id = :i AND tenant_id = :t"
        ),
        {"n": new, "i": variant_id, "t": tenant_id},
    )
    await db.execute(
        text(
            """INSERT INTO inventory_movements (tenant_id, vendor_id, variant_id, delta, balance_after, reason, ref, actor_id)
           VALUES (:t, :v, :i, :d, :n, :r, :ref, :a)"""
        ),
        {
            "t": tenant_id,
            "v": vendor_id,
            "i": variant_id,
            "d": delta,
            "n": new,
            "r": reason,
            "ref": ref,
            "a": actor,
        },
    )
    return new


@vendor.post("/variants/{variant_id}/stock", response_model=VariantOut)
async def change_stock(
    variant_id: uuid.UUID,
    body: StockIn,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    v = await _vendor_variant(db, tenant.id, p.vid, variant_id)
    await adjust_stock(
        db,
        tenant_id=tenant.id,
        vendor_id=p.vid,
        variant_id=variant_id,
        delta=body.delta,
        reason=body.reason,
        actor=p.sub,
        ref=body.note,
    )
    await _refresh_rollups(db, tenant.id, v["product_id"])
    await request.app.state.revalidator.revalidate(
        tenant.id, product_tags(tenant.id, product_slug=v["product_slug"])
    )
    row = (
        (
            await db.execute(
                text(
                    """SELECT id, sku, options, price, compare_at_price, stock_on_hand, stock_on_hand - stock_reserved AS available,
                  is_active FROM product_variants WHERE id = :i AND tenant_id = :t"""
                ),
                {"i": variant_id, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return VariantOut(**row)


class AttachMediaIn(BaseModel):
    asset_id: uuid.UUID
    position: int = Field(default=0, ge=0, le=50)
    alt_text: str | None = Field(default=None, max_length=160)


@vendor.post("/products/{product_id}/media", response_model=MediaOut, status_code=201)
async def attach_media(
    product_id: uuid.UUID,
    body: AttachMediaIn,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    current = await _vendor_product(db, tenant.id, p.vid, product_id)
    asset = (
        await db.execute(
            text(
                "SELECT status FROM media_assets WHERE id = :a AND tenant_id = :t AND vendor_id = :v"
            ),
            {"a": body.asset_id, "t": tenant.id, "v": p.vid},
        )
    ).first()
    if asset is None:
        raise NotFound("Image not found")
    if asset.status != "ready":
        raise Conflict(
            "Image is still processing"
            if asset.status == "processing"
            else "Image failed to process"
        )
    count = (
        await db.execute(
            text("SELECT count(*) FROM product_media WHERE tenant_id = :t AND product_id = :p"),
            {"t": tenant.id, "p": product_id},
        )
    ).scalar()
    if count >= 12:
        raise AppError("A product can have at most 12 photos", status=422, code="too_many_photos")
    try:
        async with db.begin_nested():
            mid = (
                await db.execute(
                    text(
                        """INSERT INTO product_media (tenant_id, vendor_id, product_id, asset_id, position)
                   VALUES (:t, :v, :p, :a, :pos) RETURNING id"""
                    ),
                    {
                        "t": tenant.id,
                        "v": p.vid,
                        "p": product_id,
                        "a": body.asset_id,
                        "pos": body.position,
                    },
                )
            ).scalar()
    except IntegrityError as exc:
        raise Conflict("Image already attached") from exc
    if body.alt_text:
        await db.execute(
            text("UPDATE media_assets SET alt_text = :alt WHERE id = :a AND tenant_id = :t"),
            {"alt": body.alt_text, "a": body.asset_id, "t": tenant.id},
        )
    await request.app.state.revalidator.revalidate(
        tenant.id, product_tags(tenant.id, product_slug=current["slug"])
    )
    row = (
        (
            await db.execute(
                text(
                    """SELECT pm.id, pm.asset_id, pm.position, a.renditions, a.blur_data, a.width, a.height, a.alt_text
           FROM product_media pm JOIN media_assets a ON a.id = pm.asset_id AND a.tenant_id = pm.tenant_id
           WHERE pm.id = :m AND pm.tenant_id = :t"""
                ),
                {"m": mid, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return MediaOut(**row)


@vendor.delete("/product-media/{media_id}", status_code=204)
async def detach_media(
    media_id: uuid.UUID, request: Request, p: VendorCatalog, tenant: Tenant, db: TenantDB
):
    row = (
        await db.execute(
            text(
                """DELETE FROM product_media pm USING products pr
           WHERE pm.id = :m AND pm.tenant_id = :t AND pm.vendor_id = :v AND pr.id = pm.product_id AND pr.tenant_id = pm.tenant_id
           RETURNING pr.slug"""
            ),
            {"m": media_id, "t": tenant.id, "v": p.vid},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await request.app.state.revalidator.revalidate(
        tenant.id, product_tags(tenant.id, product_slug=row.slug)
    )


# -------------------------------------------------------------------------------------------- moderation
class ModerationItem(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    vendor_name: str
    slug: str
    title_en: str
    updated_at: datetime


class ModerateIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=300)


@admin.get("/moderation/products", response_model=list[ModerationItem])
async def moderation_queue(
    _: Moderator, tenant: Tenant, db: TenantDB, limit: int = Query(50, ge=1, le=200)
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.vendor_id, v.display_name AS vendor_name, p.slug, p.title_en, p.updated_at
           FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
           WHERE p.tenant_id = :t AND p.moderation_status = 'pending' AND p.status = 'active'
           ORDER BY p.updated_at LIMIT :l"""
                ),
                {"t": tenant.id, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [ModerationItem(**r) for r in rows]


@admin.post("/products/{product_id}/moderate", response_model=ProductOut)
async def moderate(
    product_id: uuid.UUID,
    body: ModerateIn,
    request: Request,
    actor: Moderator,
    tenant: Tenant,
    db: TenantDB,
):
    if body.decision == "reject" and not body.reason:
        raise AppError("A reason is required", status=422, code="reason_required")
    row = (
        await db.execute(
            text(
                """UPDATE products SET moderation_status = :s, moderation_reason = :r,
               published_at = CASE WHEN :s = 'approved' AND status = 'active' THEN coalesce(published_at, now())
                                   ELSE published_at END, updated_at = now()
           WHERE id = :p AND tenant_id = :t RETURNING slug, category_id, vendor_id"""
            ),
            {
                "s": "approved" if body.decision == "approve" else "rejected",
                "r": body.reason,
                "p": product_id,
                "t": tenant.id,
            },
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action=f"product.{body.decision}",
        entity="product",
        entity_id=product_id,
        data={"reason": body.reason},
        request=request,
    )
    await request.app.state.revalidator.revalidate(
        tenant.id,
        product_tags(
            tenant.id,
            product_slug=row.slug,
            category_ids=[row.category_id],
            vendor_id=row.vendor_id,
        ),
    )
    return await load_product(db, tenant.id, product_id)


# ------------------------------------------------------------------------------------------------ public
VISIBLE = """p.tenant_id = :t AND p.status = 'active' AND p.moderation_status = 'approved'
             AND v.status = 'approved'"""


class CardOut(BaseModel):
    id: uuid.UUID
    slug: str
    title_en: str
    title_bn: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    in_stock: bool
    vendor_id: uuid.UUID
    vendor_name: str
    image: dict | None
    blur_data: str | None


class Page(BaseModel):
    items: list[CardOut]
    next_cursor: str | None


def _cursor(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(base64.urlsafe_b64decode(value.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        return None


@public.get("/products", response_model=Page)
async def list_products(
    tenant: Tenant,
    db: TenantDB,
    category: str | None = Query(None, max_length=80),
    brand: str | None = Query(None, max_length=80),
    store: str | None = Query(None, max_length=80),
    cursor: str | None = Query(None, max_length=64),
    limit: int = Query(24, ge=1, le=60),
):
    """Keyset pagination on id (uuid v7 = newest first). One query."""
    sql = f"""SELECT p.id, p.slug, p.title_en, p.title_bn, p.min_price, p.max_price, p.in_stock, p.vendor_id,
                     v.display_name AS vendor_name, a.renditions AS image, a.blur_data
              FROM products p
              JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
              LEFT JOIN LATERAL (SELECT ma.renditions, ma.blur_data FROM product_media pm
                                 JOIN media_assets ma ON ma.id = pm.asset_id AND ma.tenant_id = pm.tenant_id
                                 WHERE pm.tenant_id = p.tenant_id AND pm.product_id = p.id AND ma.status = 'ready'
                                 ORDER BY pm.position LIMIT 1) a ON true
              WHERE {VISIBLE}"""  # noqa: S608 - VISIBLE is a constant
    params: dict = {"t": tenant.id, "l": limit + 1}
    if category:
        sql += """ AND p.category_id IN (SELECT c.id FROM categories c WHERE c.tenant_id = :t AND
                   (c.slug = :cat OR (SELECT id FROM categories WHERE tenant_id = :t AND slug = :cat) = ANY(c.path)))"""
        params["cat"] = category
    if brand:
        sql += " AND p.brand_id = (SELECT id FROM brands WHERE tenant_id = :t AND slug = :brand)"
        params["brand"] = brand
    if store:
        sql += " AND v.slug = :store"
        params["store"] = store
    if cursor and (c := _cursor(cursor)):
        sql += " AND p.id < :cur"
        params["cur"] = c
    rows = (await db.execute(text(sql + " ORDER BY p.id DESC LIMIT :l"), params)).mappings().all()
    items = [CardOut(**r) for r in rows[:limit]]
    nxt = (
        base64.urlsafe_b64encode(str(items[-1].id).encode()).decode() if len(rows) > limit else None
    )
    return Page(items=items, next_cursor=nxt)


class VendorCard(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    logo_url: str | None
    district: str | None
    return_policy: str | None
    is_house: bool


class PdpOut(ProductOut):
    vendor: VendorCard
    category: dict
    brand: dict | None


@public.get("/products/{slug}", response_model=PdpOut)
async def product_detail(slug: str, tenant: Tenant, db: TenantDB):
    """PDP budget: 3 queries (product+vendor+category+brand, variants, media)."""
    row = (
        (
            await db.execute(
                text(
                    f"""SELECT p.*, v.slug AS v_slug, v.display_name AS v_name, v.district AS v_district, v.is_house AS v_house,
                   sf.logo_url AS v_logo, sf.return_policy AS v_return,
                   c.slug AS c_slug, c.name_en AS c_name_en, c.name_bn AS c_name_bn, b.slug AS b_slug, b.name AS b_name
            FROM products p
            JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
            LEFT JOIN vendor_storefronts sf ON sf.vendor_id = v.id AND sf.tenant_id = v.tenant_id
            JOIN categories c ON c.id = p.category_id AND c.tenant_id = p.tenant_id
            LEFT JOIN brands b ON b.id = p.brand_id AND b.tenant_id = p.tenant_id
            WHERE {VISIBLE} AND p.slug = :s"""
                ),  # noqa: S608
                {"t": tenant.id, "s": slug},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Product not found")
    variants = (
        (
            await db.execute(
                text(
                    """SELECT id, sku, options, price, compare_at_price, stock_on_hand,
                          stock_on_hand - stock_reserved AS available, is_active
                   FROM product_variants WHERE tenant_id = :t AND product_id = :p AND is_active
                   ORDER BY created_at"""
                ),
                {"t": tenant.id, "p": row["id"]},
            )
        )
        .mappings()
        .all()
    )
    media = (
        (
            await db.execute(
                text(
                    """SELECT pm.id, pm.asset_id, pm.position, a.renditions, a.blur_data, a.width, a.height, a.alt_text
                   FROM product_media pm JOIN media_assets a ON a.id = pm.asset_id AND a.tenant_id = pm.tenant_id
                   WHERE pm.tenant_id = :t AND pm.product_id = :p AND a.status = 'ready' ORDER BY pm.position"""
                ),
                {"t": tenant.id, "p": row["id"]},
            )
        )
        .mappings()
        .all()
    )
    base = ProductOut(
        **{k: row[k] for k in ProductOut.model_fields if k in row},
        variants=[VariantOut(**v) for v in variants],
        media=[MediaOut(**m) for m in media],
    )
    return PdpOut(
        **base.model_dump(),
        vendor=VendorCard(
            id=row["vendor_id"],
            slug=row["v_slug"],
            display_name=row["v_name"],
            logo_url=row["v_logo"],
            district=row["v_district"],
            return_policy=row["v_return"],
            is_house=row["v_house"],
        ),
        category={"slug": row["c_slug"], "name_en": row["c_name_en"], "name_bn": row["c_name_bn"]},
        brand={"slug": row["b_slug"], "name": row["b_name"]} if row["b_slug"] else None,
    )


# ------------------------------------------------------------------------------------------------- Q&A
class QuestionIn(BaseModel):
    question: str = Field(min_length=5, max_length=500)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class QuestionOut(BaseModel):
    id: uuid.UUID
    question: str
    answer: str | None
    answered_at: datetime | None
    created_at: datetime


@public.post("/products/{slug}/questions", response_model=QuestionOut, status_code=201)
async def ask(
    slug: str,
    body: QuestionIn,
    request: Request,
    principal: CurrentPrincipal,
    tenant: Tenant,
    db: TenantDB,
):
    if principal.kind != "buyer":
        raise Forbidden("Sign in as a customer to ask")
    from app.core.cache_keys import tkey
    from app.core.ratelimit import hit

    await hit(request.app.state.redis, tkey(tenant.id, "rl", "qa", principal.sub), 10, 3600)
    prod = (
        await db.execute(
            text(
                f"SELECT p.id, p.vendor_id FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id "
                f"WHERE {VISIBLE} AND p.slug = :s"
            ),
            {"t": tenant.id, "s": slug},
        )
    ).first()  # noqa: S608
    if prod is None:
        raise NotFound("Product not found")
    row = (
        (
            await db.execute(
                text(
                    """INSERT INTO product_questions (tenant_id, vendor_id, product_id, user_id, question)
           VALUES (:t, :v, :p, :u, :q) RETURNING id, question, answer, answered_at, created_at"""
                ),
                {
                    "t": tenant.id,
                    "v": prod.vendor_id,
                    "p": prod.id,
                    "u": principal.sub,
                    "q": clean_text(body.question),
                },
            )
        )
        .mappings()
        .one()
    )
    return QuestionOut(**row)


@public.get("/products/{slug}/questions", response_model=list[QuestionOut])
async def questions(slug: str, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT q.id, q.question, q.answer, q.answered_at, q.created_at FROM product_questions q
           JOIN products p ON p.id = q.product_id AND p.tenant_id = q.tenant_id
           WHERE q.tenant_id = :t AND p.slug = :s AND q.status = 'published' ORDER BY q.answered_at DESC LIMIT 50"""
                ),
                {"t": tenant.id, "s": slug},
            )
        )
        .mappings()
        .all()
    )
    return [QuestionOut(**r) for r in rows]


@vendor.get("/questions", response_model=list[QuestionOut])
async def my_questions(p: VendorCatalog, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, question, answer, answered_at, created_at FROM product_questions
           WHERE tenant_id = :t AND vendor_id = :v AND status = 'pending' ORDER BY created_at LIMIT 100"""
                ),
                {"t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .all()
    )
    return [QuestionOut(**r) for r in rows]


@vendor.post("/questions/{question_id}/answer", response_model=QuestionOut)
async def answer(
    question_id: uuid.UUID,
    body: AnswerIn,
    request: Request,
    p: VendorCatalog,
    tenant: Tenant,
    db: TenantDB,
):
    row = (
        (
            await db.execute(
                text(
                    """UPDATE product_questions SET answer = :a, answered_by = :by, answered_at = now(), status = 'published'
           WHERE id = :q AND tenant_id = :t AND vendor_id = :v AND status <> 'hidden'
           RETURNING id, question, answer, answered_at, created_at"""
                ),
                {
                    "a": clean_text(body.answer),
                    "by": p.sub,
                    "q": question_id,
                    "t": tenant.id,
                    "v": p.vid,
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="question.answer",
        entity="product_question",
        entity_id=question_id,
        request=request,
    )
    return QuestionOut(**row)


@admin.post("/questions/{question_id}/hide", status_code=204)
async def hide_question(
    question_id: uuid.UUID, request: Request, actor: Moderator, tenant: Tenant, db: TenantDB
):
    row = (
        await db.execute(
            text(
                "UPDATE product_questions SET status = 'hidden' WHERE id = :q AND tenant_id = :t RETURNING id"
            ),
            {"q": question_id, "t": tenant.id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="question.hide",
        entity="product_question",
        entity_id=question_id,
        request=request,
    )
