"""Storefront page resolution: published (or previewed) sections + the data they reference, in a fixed
query budget (architecture §5.3: home page ≤ 6 data queries)."""

import hashlib
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.core.deps import Tenant, TenantDB
from app.core.errors import NotFound
from app.modules.catalog.products import VISIBLE
from app.modules.theme import service
from app.modules.theme.css import sanitize_css
from app.modules.theme.schema import ThemeDocument

router = APIRouter(prefix="/api/v1/storefront", tags=["storefront"])

CARD_SQL = f"""
SELECT p.id, p.slug, p.title_en, p.title_bn, p.min_price, p.max_price, p.in_stock, p.vendor_id,
       v.display_name AS vendor_name, v.slug AS vendor_slug, p.category_id, p.brand_id,
       (SELECT ma.renditions FROM product_media pm JOIN media_assets ma ON ma.id = pm.asset_id AND ma.tenant_id = pm.tenant_id
         WHERE pm.tenant_id = p.tenant_id AND pm.product_id = p.id AND ma.status = 'ready' ORDER BY pm.position LIMIT 1) AS image
FROM products p JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
WHERE {VISIBLE}"""  # noqa: S608 - constant


class ResolvedSection(BaseModel):
    id: str
    type: str
    settings: dict
    data: dict | None = None


class PageOut(BaseModel):
    template: str
    version: int
    preview: bool
    title: dict | None = None
    seo: dict | None = None
    sections: list[ResolvedSection]
    layouts: dict


async def resolve_sections(db, tenant_id: str, sections: list[dict]) -> list[ResolvedSection]:
    visible = [s for s in sections if not s.get("hidden")]
    queries = {s["id"]: s["settings"].get("query") for s in visible if s["settings"].get("query")}
    category_slugs = {
        slug
        for s in visible
        if s["type"] == "category_grid"
        for slug in s["settings"]["category_slugs"]
    }
    brand_slugs = {
        slug for s in visible if s["type"] == "brand_strip" for slug in s["settings"]["brand_slugs"]
    }
    needs_vendors = any(s["type"] in ("top_vendors", "featured_vendor") for s in visible)
    data: dict[str, dict] = {}

    # 1 query: all product sources at once, ranked per section with a window function
    if queries:
        manual = {
            str(pid)
            for q in queries.values()
            if q["source"] == "manual"
            for pid in q["product_ids"]
        }
        rows = (
            (
                await db.execute(
                    text(
                        f"""WITH cards AS ({CARD_SQL}),
                    cats AS (SELECT c.id, c.slug, c.path FROM categories c WHERE c.tenant_id = :t),
                    brands_ AS (SELECT id, slug FROM brands WHERE tenant_id = :t)
                SELECT s.section_id, x.* FROM unnest(CAST(:sids AS text[]), CAST(:sources AS text[]), CAST(:vals AS text[]),
                                                   CAST(:limits AS int[]))
                       AS s(section_id, source, val, lim)
                CROSS JOIN LATERAL (
                    SELECT cards.* FROM cards
                    WHERE (s.source = 'newest')
                       OR (s.source = 'category' AND cards.category_id IN
                            (SELECT id FROM cats WHERE slug = s.val OR (SELECT id FROM cats WHERE slug = s.val) = ANY(path)))
                       OR (s.source = 'brand' AND cards.brand_id = (SELECT id FROM brands_ WHERE slug = s.val))
                       OR (s.source = 'store' AND cards.vendor_slug = s.val)
                       OR (s.source = 'manual' AND cards.id::text = ANY(CAST(:manual AS text[])))
                    ORDER BY cards.id DESC LIMIT s.lim) x"""
                    ),  # noqa: S608
                    {
                        "t": tenant_id,
                        "manual": sorted(manual),
                        "sids": list(queries),
                        "sources": [q["source"] for q in queries.values()],
                        "vals": [
                            q.get("category_slug")
                            or q.get("brand_slug")
                            or q.get("store_slug")
                            or ""
                            for q in queries.values()
                        ],
                        "limits": [q["limit"] for q in queries.values()],
                    },
                )
            )
            .mappings()
            .all()
        )
        for sid, q in queries.items():
            items = [dict(r) for r in rows if r["section_id"] == sid]
            if q["source"] == "manual":
                order = {str(p): i for i, p in enumerate(q["product_ids"])}
                items = sorted(
                    (i for i in items if str(i["id"]) in order), key=lambda i: order[str(i["id"])]
                )
            data[sid] = {
                "products": [
                    {
                        k: (
                            str(v)
                            if k in ("id", "vendor_id", "category_id", "brand_id") and v
                            else v
                        )
                        for k, v in i.items()
                        if k != "section_id"
                    }
                    for i in items
                ]
            }
    # 1 query: categories referenced by grids
    if category_slugs:
        cats = (
            (
                await db.execute(
                    text(
                        "SELECT slug, name_en, name_bn FROM categories WHERE tenant_id = :t AND is_active AND slug = ANY(:s)"
                    ),
                    {"t": tenant_id, "s": sorted(category_slugs)},
                )
            )
            .mappings()
            .all()
        )
        by = {c["slug"]: dict(c) for c in cats}
        for s in visible:
            if s["type"] == "category_grid":
                data[s["id"]] = {
                    "categories": [by[x] for x in s["settings"]["category_slugs"] if x in by]
                }
    # 1 query: brands
    if brand_slugs:
        brands = (
            (
                await db.execute(
                    text(
                        "SELECT slug, name, logo_url FROM brands WHERE tenant_id = :t AND is_active AND slug = ANY(:s)"
                    ),
                    {"t": tenant_id, "s": sorted(brand_slugs)},
                )
            )
            .mappings()
            .all()
        )
        by = {b["slug"]: dict(b) for b in brands}
        for s in visible:
            if s["type"] == "brand_strip":
                data[s["id"]] = {"brands": [by[x] for x in s["settings"]["brand_slugs"] if x in by]}
    # 1 query: vendors (top by approved active products; featured by slug)
    if needs_vendors:
        vendors = (
            (
                await db.execute(
                    text(
                        """SELECT v.slug, v.display_name, sf.logo_url, v.district,
                      (SELECT count(*) FROM products p WHERE p.tenant_id = v.tenant_id AND p.vendor_id = v.id
                        AND p.status = 'active' AND p.moderation_status = 'approved') AS product_count
               FROM vendors v LEFT JOIN vendor_storefronts sf ON sf.vendor_id = v.id AND sf.tenant_id = v.tenant_id
               WHERE v.tenant_id = :t AND v.status = 'approved' AND NOT v.is_house
               ORDER BY product_count DESC, v.display_name LIMIT 50"""
                    ),
                    {"t": tenant_id},
                )
            )
            .mappings()
            .all()
        )
        for s in visible:
            if s["type"] == "top_vendors":
                data[s["id"]] = {"vendors": [dict(v) for v in vendors[: s["settings"]["limit"]]]}
            elif s["type"] == "featured_vendor":
                match = next(
                    (dict(v) for v in vendors if v["slug"] == s["settings"]["store_slug"]), None
                )
                data[s["id"]] = {"vendor": match}
    return [
        ResolvedSection(id=s["id"], type=s["type"], settings=s["settings"], data=data.get(s["id"]))
        for s in visible
    ]


async def _document(
    request: Request, db, tenant_id: str, preview: str | None
) -> tuple[dict, int, bool]:
    if preview and service.preview_allowed(request.app.state.settings, preview, tenant_id):
        theme = await service.ensure_theme(db, tenant_id)
        return (
            ThemeDocument.model_validate(theme["draft_document"]).model_dump(
                mode="json", by_alias=True
            ),
            0,
            True,
        )
    payload = await service.published_document(db, request.app.state.redis, tenant_id)
    doc = ThemeDocument.model_validate(payload["document"]).model_dump(mode="json", by_alias=True)
    return doc, payload["version"], False


@router.get("/page", response_model=PageOut)
async def page(
    request: Request,
    response: Response,
    tenant: Tenant,
    db: TenantDB,
    template: Literal["home", "category_top", "product_bottom", "custom"] = "home",
    slug: str | None = Query(None, pattern=r"^[a-z0-9-]{1,60}$"),
    preview: str | None = Query(None, max_length=1024),
):
    doc, version, is_preview = await _document(request, db, tenant.id, preview)
    title = seo = None
    if template == "custom":
        page_doc = next(
            (p for p in doc["pages"] if p["slug"] == slug and (p["published"] or is_preview)), None
        )
        if page_doc is None:
            raise NotFound("Page not found")
        sections, title, seo = page_doc["sections"], page_doc["title"], page_doc["seo"]
    else:
        sections = doc["templates"][template]
    resolved = await resolve_sections(db, tenant.id, sections)
    response.headers["cache-control"] = (
        "no-store" if is_preview else "public, max-age=30, stale-while-revalidate=300"
    )
    return PageOut(
        template=template,
        version=version,
        preview=is_preview,
        title=title,
        seo=seo,
        sections=resolved,
        layouts={
            **doc["layouts"],
            "product_layout": doc["templates"]["product_layout"],
            "collection_layout": doc["templates"]["collection_layout"],
        },
    )


@router.get("/custom.css")
async def custom_css(
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    preview: str | None = Query(None, max_length=1024),
):
    doc, version, is_preview = await _document(request, db, tenant.id, preview)
    css = sanitize_css(doc.get("custom_css") or "", tenant.id) if doc.get("custom_css") else ""
    etag = '"' + hashlib.sha256(css.encode()).hexdigest()[:24] + '"'
    if not is_preview and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"etag": etag})
    return Response(
        css,
        media_type="text/css",
        headers={
            "etag": etag,
            "x-content-type-options": "nosniff",
            "cache-control": "no-store"
            if is_preview
            else "public, max-age=60, stale-while-revalidate=600",
        },
    )
