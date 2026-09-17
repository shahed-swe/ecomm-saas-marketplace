"""Vendor store pages: the limited builder (decision: owner full, vendor limited).

Vendors pick an accent colour (contrast-checked against the tenant's live palette), a banner, and
up to 10 `vendor_allowed` sections. Everything else about the storefront stays the tenant's."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from app.core import audit
from app.core.cache_keys import isr_tag
from app.core.deps import Tenant, TenantDB, require_vendor_role
from app.core.errors import AppError, NotFound
from app.core.security import Principal
from app.modules.catalog.products import CARD_COLUMNS_SQL
from app.modules.theme import service
from app.modules.theme.contrast import ratio
from app.modules.theme.schema import RGB_PATTERN, ThemeDocument
from app.modules.theme.sections import Section, asset_url, validate_sections
from app.modules.theme.storefront import ResolvedSection, resolve_sections

vendor = APIRouter(prefix="/api/v1/vendor/store-theme", tags=["vendor:store-theme"])
public = APIRouter(prefix="/api/v1/storefront/stores", tags=["storefront"])
StoreEditor = Annotated[Principal, Depends(require_vendor_role("storefront.write", approved=True))]


class StoreThemeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accent: str | None = Field(default=None, pattern=RGB_PATTERN)
    banner_url: str | None = None
    sections: list[Section] = Field(default_factory=list, max_length=10)
    _banner = field_validator("banner_url")(lambda cls, v: asset_url(v))


class StoreThemeOut(BaseModel):
    draft: StoreThemeIn
    published: StoreThemeIn | None
    published_at: datetime | None


async def _validate(db, request: Request, tenant_id: str, body: StoreThemeIn) -> None:
    try:
        validate_sections(body.sections, "store", vendor=True)
    except ValueError as exc:
        raise AppError(str(exc), status=422, code="invalid_sections") from exc
    raw = body.model_dump_json()
    import re

    for url in re.findall(r'"(/media/t/[^"]+)"', raw):
        if not url.startswith(f"/media/t/{tenant_id}/"):
            raise AppError("Image does not belong to this store", status=422, code="invalid_asset")
    if body.accent:
        payload = await service.published_document(db, request.app.state.redis, tenant_id)
        colors = ThemeDocument.model_validate(payload["document"]).tokens.colors
        if ratio(body.accent, colors.bg) < 3.0 or ratio("255 255 255", body.accent) < 4.5:
            raise AppError(
                "Pick a darker accent colour so text stays readable",
                status=422,
                code="contrast_too_low",
            )


@vendor.get("", response_model=StoreThemeOut)
async def get_store_theme(p: StoreEditor, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(
                    "SELECT draft, published, published_at FROM vendor_store_themes "
                    "WHERE tenant_id = :t AND vendor_id = :v"
                ),
                {"t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return StoreThemeOut(draft=StoreThemeIn(), published=None, published_at=None)
    return StoreThemeOut(
        draft=StoreThemeIn.model_validate(row["draft"]),
        published=StoreThemeIn.model_validate(row["published"]) if row["published"] else None,
        published_at=row["published_at"],
    )


@vendor.put("/draft", response_model=StoreThemeOut)
async def put_draft(
    body: StoreThemeIn, request: Request, p: StoreEditor, tenant: Tenant, db: TenantDB
):
    await _validate(db, request, tenant.id, body)
    await db.execute(
        text(
            """INSERT INTO vendor_store_themes (tenant_id, vendor_id, draft, updated_by) VALUES (:t, :v, CAST(:d AS jsonb), :a)
           ON CONFLICT (tenant_id, vendor_id) DO UPDATE SET draft = EXCLUDED.draft, updated_at = now(),
               updated_by = EXCLUDED.updated_by"""
        ),
        {"t": tenant.id, "v": p.vid, "d": body.model_dump_json(), "a": p.sub},
    )
    return await get_store_theme(p, tenant, db)


@vendor.post("/publish", response_model=StoreThemeOut)
async def publish(request: Request, p: StoreEditor, tenant: Tenant, db: TenantDB):
    row = (
        await db.execute(
            text(
                "SELECT draft FROM vendor_store_themes WHERE tenant_id = :t AND vendor_id = :v FOR UPDATE"
            ),
            {"t": tenant.id, "v": p.vid},
        )
    ).first()
    if row is None:
        raise NotFound("Nothing to publish")
    body = StoreThemeIn.model_validate(row.draft)
    await _validate(db, request, tenant.id, body)
    await db.execute(
        text(
            "UPDATE vendor_store_themes SET published = draft, published_at = now() "
            "WHERE tenant_id = :t AND vendor_id = :v"
        ),
        {"t": tenant.id, "v": p.vid},
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="store_theme.publish",
        entity="vendor",
        entity_id=p.vid,
        request=request,
    )
    await request.app.state.revalidator.revalidate(tenant.id, [isr_tag(tenant.id, "store", p.vid)])
    return await get_store_theme(p, tenant, db)


class StorePageOut(BaseModel):
    vendor: dict
    accent: str | None
    banner_url: str | None
    sections: list[ResolvedSection]
    products: list[dict]


@public.get("/{slug}", response_model=StorePageOut)
async def store_page(slug: str, tenant: Tenant, db: TenantDB, limit: int = Query(24, ge=1, le=60)):
    v = (
        (
            await db.execute(
                text(
                    """SELECT v.id, v.slug, v.display_name, v.district, v.is_house, sf.logo_url, sf.tagline, sf.bio, sf.return_policy,
                  st.published
           FROM vendors v
           LEFT JOIN vendor_storefronts sf ON sf.vendor_id = v.id AND sf.tenant_id = v.tenant_id
           LEFT JOIN vendor_store_themes st ON st.vendor_id = v.id AND st.tenant_id = v.tenant_id
           WHERE v.tenant_id = :t AND v.slug = :s AND v.status = 'approved' AND NOT v.is_house"""
                ),
                {"t": tenant.id, "s": slug},
            )
        )
        .mappings()
        .first()
    )
    if v is None:
        raise NotFound("Store not found")
    theme = StoreThemeIn.model_validate(v["published"]) if v["published"] else StoreThemeIn()
    sections = await resolve_sections(
        db, tenant.id, [s.model_dump(mode="json") for s in theme.sections]
    )
    rows = (
        (
            await db.execute(
                text(CARD_COLUMNS_SQL + " AND v.id = :vid ORDER BY p.id DESC LIMIT :l"),
                {"t": tenant.id, "vid": v["id"], "l": limit},
            )
        )
        .mappings()
        .all()
    )
    card = {k: (str(val) if k == "id" else val) for k, val in v.items() if k != "published"}
    return StorePageOut(
        vendor=card,
        accent=theme.accent,
        banner_url=theme.banner_url,
        sections=sections,
        products=[
            {k: (str(x) if k in ("id", "vendor_id") else x) for k, x in r.items()} for r in rows
        ],
    )
