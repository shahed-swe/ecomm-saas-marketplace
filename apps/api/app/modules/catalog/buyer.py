"""Buyer conveniences: wishlist (DB) and recently viewed (Redis, capped)."""

import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.core.cache_keys import tkey
from app.core.deps import CurrentPrincipal, Tenant, TenantDB
from app.core.errors import Forbidden, NotFound
from app.modules.catalog.products import VISIBLE

router = APIRouter(prefix="/api/v1/me", tags=["buyer"])
RECENT_MAX = 30


def _buyer(p):
    if p.kind != "buyer":
        raise Forbidden("Customer account required")
    return p


class ProductRef(BaseModel):
    product_id: uuid.UUID


CARDS = f"""SELECT p.id, p.slug, p.title_en, p.title_bn, p.min_price, p.max_price, p.in_stock, v.display_name AS vendor_name,
       (SELECT ma.renditions FROM product_media pm JOIN media_assets ma ON ma.id = pm.asset_id AND ma.tenant_id = pm.tenant_id
         WHERE pm.tenant_id = p.tenant_id AND pm.product_id = p.id AND ma.status = 'ready' ORDER BY pm.position LIMIT 1) AS image
FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id WHERE {VISIBLE}"""  # noqa: S608


async def _visible(db, tenant_id: str, product_id) -> bool:
    return (
        await db.execute(text(CARDS + " AND p.id = :p"), {"t": tenant_id, "p": product_id})
    ).first() is not None


@router.get("/wishlist")
async def wishlist(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    CARDS
                    + " AND p.id IN (SELECT product_id FROM wishlist_items WHERE tenant_id = :t AND user_id = :u) ORDER BY p.title_en"
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [{k: (str(v) if k == "id" else v) for k, v in r.items()} for r in rows]


@router.post("/wishlist", status_code=204)
async def add_wishlist(body: ProductRef, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    if not await _visible(db, tenant.id, body.product_id):
        raise NotFound("Product not found")
    await db.execute(
        text("""INSERT INTO wishlist_items (tenant_id, user_id, product_id) VALUES (:t, :u, :p)
                             ON CONFLICT DO NOTHING"""),
        {"t": tenant.id, "u": p.sub, "p": body.product_id},
    )


@router.delete("/wishlist/{product_id}", status_code=204)
async def remove_wishlist(product_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    await db.execute(
        text(
            "DELETE FROM wishlist_items WHERE tenant_id = :t AND user_id = :u AND product_id = :p"
        ),
        {"t": tenant.id, "u": p.sub, "p": product_id},
    )


@router.post("/recently-viewed", status_code=204)
async def viewed(
    body: ProductRef, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    if not await _visible(db, tenant.id, body.product_id):
        raise NotFound("Product not found")
    key = tkey(tenant.id, "rv", p.sub)
    redis = request.app.state.redis
    async with redis.pipeline(transaction=True) as pipe:
        await (
            pipe.lrem(key, 0, str(body.product_id))
            .lpush(key, str(body.product_id))
            .ltrim(key, 0, RECENT_MAX - 1)
            .expire(key, 60 * 60 * 24 * 60)
            .execute()
        )


@router.get("/recently-viewed")
async def recently_viewed(request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    ids = await request.app.state.redis.lrange(tkey(tenant.id, "rv", p.sub), 0, RECENT_MAX - 1)
    if not ids:
        return []
    rows = (
        (
            await db.execute(
                text(CARDS + " AND p.id = ANY(CAST(:ids AS uuid[]))"), {"t": tenant.id, "ids": ids}
            )
        )
        .mappings()
        .all()
    )
    by = {str(r["id"]): {k: (str(v) if k == "id" else v) for k, v in r.items()} for r in rows}
    return [by[i] for i in ids if i in by]
