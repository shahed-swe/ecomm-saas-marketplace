"""SEO feeds for the storefront: sitemap entries (visible products, categories, stores, custom pages)."""

from datetime import datetime

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.core.deps import Tenant, TenantDB
from app.modules.catalog.products import VISIBLE
from app.modules.theme import service as theme_service

router = APIRouter(prefix="/api/v1/seo", tags=["seo"])
MAX_URLS = 50_000  # sitemap protocol limit per file


class Entry(BaseModel):
    path: str
    lastmod: datetime | None = None


class SitemapOut(BaseModel):
    primary_host: str | None
    entries: list[Entry]


@router.get("/sitemap", response_model=SitemapOut)
async def sitemap(
    request: Request, tenant: Tenant, db: TenantDB, page: int = Query(1, ge=1, le=20)
):
    offset = (page - 1) * MAX_URLS
    rows = (
        (
            await db.execute(
                text(
                    f"""(SELECT '/p/' || p.slug AS path, p.updated_at AS lastmod FROM products p
             JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id WHERE {VISIBLE})
            UNION ALL
            (SELECT '/c/' || slug, NULL FROM categories WHERE tenant_id = :t AND is_active)
            UNION ALL
            (SELECT '/store/' || slug, updated_at FROM vendors WHERE tenant_id = :t AND status = 'approved' AND NOT is_house)
            ORDER BY path LIMIT :lim OFFSET :off"""
                ),  # noqa: S608
                {"t": tenant.id, "lim": MAX_URLS, "off": offset},
            )
        )
        .mappings()
        .all()
    )
    entries = [Entry(path="/")] if page == 1 else []
    if page == 1:
        doc = (await theme_service.published_document(db, request.app.state.redis, tenant.id))[
            "document"
        ]
        entries += [
            Entry(path=f"/pages/{p['slug']}")
            for p in doc.get("pages", [])
            if p.get("published", True)
        ]
    entries += [Entry(**r) for r in rows]
    return SitemapOut(primary_host=tenant.primary_host, entries=entries)
