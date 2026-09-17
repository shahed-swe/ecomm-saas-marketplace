"""Tenant-level taxonomy (fixes v1 per-vendor categories): categories with attributes, and brands."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.cache_keys import isr_tag
from app.core.deps import Tenant, TenantDB, require_tenant_staff
from app.core.errors import AppError, Conflict, NotFound
from app.core.security import Principal

admin = APIRouter(prefix="/api/v1/admin/catalog", tags=["admin:catalog"])
public = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])
Editor = Annotated[Principal, Depends(require_tenant_staff("catalog.write"))]
SLUG = r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$"


class CategoryIn(BaseModel):
    slug: str = Field(pattern=SLUG)
    name_en: str = Field(min_length=2, max_length=80)
    name_bn: str | None = Field(default=None, max_length=80)
    parent_id: uuid.UUID | None = None
    position: int = 0


class CategoryPatch(BaseModel):
    name_en: str | None = Field(default=None, min_length=2, max_length=80)
    name_bn: str | None = Field(default=None, max_length=80)
    position: int | None = None
    is_active: bool | None = None


class CategoryOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    slug: str
    name_en: str
    name_bn: str | None
    depth: int
    position: int
    is_active: bool
    children: list["CategoryOut"] = []


class AttributeIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")
    label_en: str = Field(min_length=1, max_length=60)
    label_bn: str | None = Field(default=None, max_length=60)
    type: Literal["text", "number", "select", "multiselect", "boolean"]
    options: list[str] = Field(default_factory=list, max_length=100)
    unit: str | None = Field(default=None, max_length=12)
    required: bool = False
    filterable: bool = False
    position: int = 0

    @model_validator(mode="after")
    def _options(self):
        if self.type in ("select", "multiselect") and not self.options:
            raise ValueError("select attributes need options")
        if self.type not in ("select", "multiselect") and self.options:
            raise ValueError("only select attributes take options")
        return self


class AttributeOut(AttributeIn):
    id: uuid.UUID
    category_id: uuid.UUID


class BrandIn(BaseModel):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=80)


class BrandOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    logo_url: str | None
    is_active: bool


def _tree(rows: list[dict]) -> list[CategoryOut]:
    nodes = {r["id"]: CategoryOut(**r) for r in rows}
    roots = []
    for n in sorted(nodes.values(), key=lambda x: (x.position, x.name_en)):
        if n.parent_id and n.parent_id in nodes:
            nodes[n.parent_id].children.append(n)
        else:
            roots.append(n)
    return roots


async def _revalidate(request: Request, tenant_id: str) -> None:
    await request.app.state.revalidator.revalidate(tenant_id, [isr_tag(tenant_id, "categories")])


@admin.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    body: CategoryIn, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    depth, path = 0, []
    if body.parent_id:
        parent = (
            await db.execute(
                text("SELECT depth, path FROM categories WHERE id = :p AND tenant_id = :t"),
                {"p": body.parent_id, "t": tenant.id},
            )
        ).first()
        if parent is None:
            raise NotFound("Parent category not found")
        depth, path = parent.depth + 1, [*parent.path, body.parent_id]
        if depth > 3:
            raise AppError("Categories can be at most 4 levels deep", status=422, code="too_deep")
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        text(
                            """INSERT INTO categories (tenant_id, parent_id, slug, name_en, name_bn, depth, path, position)
                   VALUES (:t, :p, :s, :en, :bn, :d, CAST(:path AS uuid[]), :pos)
                   RETURNING id, parent_id, slug, name_en, name_bn, depth, position, is_active"""
                        ),
                        {
                            "t": tenant.id,
                            "p": body.parent_id,
                            "s": body.slug,
                            "en": body.name_en,
                            "bn": body.name_bn,
                            "d": depth,
                            "path": [str(x) for x in path],
                            "pos": body.position,
                        },
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise Conflict("Slug already used") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="category.create",
        entity="category",
        entity_id=row["id"],
        data={"slug": body.slug},
        request=request,
    )
    await _revalidate(request, tenant.id)
    return CategoryOut(**row)


@admin.patch("/categories/{category_id}", response_model=CategoryOut)
async def patch_category(
    category_id: uuid.UUID,
    body: CategoryPatch,
    request: Request,
    actor: Editor,
    tenant: Tenant,
    db: TenantDB,
):
    data = body.model_dump(exclude_unset=True)
    row = (
        (
            await db.execute(
                text(
                    """UPDATE categories SET name_en = coalesce(:en, name_en), name_bn = CASE WHEN :has_bn THEN :bn ELSE name_bn END,
               position = coalesce(:pos, position), is_active = coalesce(:act, is_active)
           WHERE id = :c AND tenant_id = :t
           RETURNING id, parent_id, slug, name_en, name_bn, depth, position, is_active"""
                ),
                {
                    "en": data.get("name_en"),
                    "has_bn": "name_bn" in data,
                    "bn": data.get("name_bn"),
                    "pos": data.get("position"),
                    "act": data.get("is_active"),
                    "c": category_id,
                    "t": tenant.id,
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
        actor=actor,
        action="category.update",
        entity="category",
        entity_id=category_id,
        data=data,
        request=request,
    )
    await _revalidate(request, tenant.id)
    return CategoryOut(**row)


@admin.post("/categories/{category_id}/attributes", response_model=AttributeOut, status_code=201)
async def add_attribute(
    category_id: uuid.UUID,
    body: AttributeIn,
    request: Request,
    actor: Editor,
    tenant: Tenant,
    db: TenantDB,
):
    if not (
        await db.execute(
            text("SELECT 1 FROM categories WHERE id = :c AND tenant_id = :t"),
            {"c": category_id, "t": tenant.id},
        )
    ).first():
        raise NotFound("Not found")
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        text(
                            """INSERT INTO category_attributes (tenant_id, category_id, key, label_en, label_bn, type, options, unit,
                       required, filterable, position)
                   VALUES (:t, :c, :k, :en, :bn, :ty, :o, :u, :r, :f, :p) RETURNING *"""
                        ),
                        {
                            "t": tenant.id,
                            "c": category_id,
                            "k": body.key,
                            "en": body.label_en,
                            "bn": body.label_bn,
                            "ty": body.type,
                            "o": body.options,
                            "u": body.unit,
                            "r": body.required,
                            "f": body.filterable,
                            "p": body.position,
                        },
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise Conflict("Attribute key already exists in this category") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="category.attribute_add",
        entity="category",
        entity_id=category_id,
        data={"key": body.key},
        request=request,
    )
    return AttributeOut(**{k: row[k] for k in AttributeOut.model_fields})


@admin.delete("/category-attributes/{attribute_id}", status_code=204)
async def delete_attribute(
    attribute_id: uuid.UUID, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    row = (
        await db.execute(
            text("DELETE FROM category_attributes WHERE id = :a AND tenant_id = :t RETURNING key"),
            {"a": attribute_id, "t": tenant.id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="category.attribute_delete",
        entity="category_attribute",
        entity_id=attribute_id,
        data={"key": row.key},
        request=request,
    )


@admin.post("/brands", response_model=BrandOut, status_code=201)
async def create_brand(
    body: BrandIn, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        text(
                            "INSERT INTO brands (tenant_id, slug, name) VALUES (:t, :s, :n) RETURNING id, slug, name, logo_url, is_active"
                        ),
                        {"t": tenant.id, "s": body.slug, "n": body.name},
                    )
                )
                .mappings()
                .one()
            )
    except IntegrityError as exc:
        raise Conflict("Slug already used") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="brand.create",
        entity="brand",
        entity_id=row["id"],
        data={"slug": body.slug},
        request=request,
    )
    return BrandOut(**row)


@admin.patch("/brands/{brand_id}", response_model=BrandOut)
async def patch_brand(
    brand_id: uuid.UUID,
    body: BrandIn,
    request: Request,
    actor: Editor,
    tenant: Tenant,
    db: TenantDB,
):
    row = (
        (
            await db.execute(
                text(
                    "UPDATE brands SET slug = :s, name = :n WHERE id = :b AND tenant_id = :t RETURNING id, slug, name, logo_url, is_active"
                ),
                {"s": body.slug, "n": body.name, "b": brand_id, "t": tenant.id},
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
        actor=actor,
        action="brand.update",
        entity="brand",
        entity_id=brand_id,
        data=body.model_dump(),
        request=request,
    )
    return BrandOut(**row)


@public.get("/categories", response_model=list[CategoryOut])
async def category_tree(tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, parent_id, slug, name_en, name_bn, depth, position, is_active FROM categories
           WHERE tenant_id = :t AND is_active ORDER BY depth, position"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return _tree([dict(r) for r in rows])


@public.get("/categories/{slug}/attributes", response_model=list[AttributeOut])
async def category_attributes(slug: str, tenant: Tenant, db: TenantDB):
    """Attributes of a category including those inherited from its ancestors."""
    rows = (
        (
            await db.execute(
                text(
                    """SELECT a.* FROM categories c
           JOIN category_attributes a ON a.tenant_id = c.tenant_id AND (a.category_id = c.id OR a.category_id = ANY(c.path))
           WHERE c.tenant_id = :t AND c.slug = :s ORDER BY a.position, a.key"""
                ),
                {"t": tenant.id, "s": slug},
            )
        )
        .mappings()
        .all()
    )
    return [AttributeOut(**{k: r[k] for k in AttributeOut.model_fields}) for r in rows]


@public.get("/brands", response_model=list[BrandOut])
async def brands(tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, slug, name, logo_url, is_active FROM brands WHERE tenant_id = :t "
                    "AND is_active ORDER BY name"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [BrandOut(**r) for r in rows]


async def attributes_for_category(db, tenant_id: str, category_id) -> list[dict]:
    return [
        dict(r)
        for r in (
            await db.execute(
                text(
                    """SELECT a.key, a.type, a.options, a.required FROM categories c
           JOIN category_attributes a ON a.tenant_id = c.tenant_id AND (a.category_id = c.id OR a.category_id = ANY(c.path))
           WHERE c.tenant_id = :t AND c.id = :c"""
                ),
                {"t": tenant_id, "c": category_id},
            )
        )
        .mappings()
        .all()
    ]


def validate_attributes(defs: list[dict], values: dict, *, require_complete: bool) -> dict:
    """Unknown keys rejected, types enforced, required enforced on publish."""
    by_key = {d["key"]: d for d in defs}
    errors, clean = [], {}
    for k, v in values.items():
        d = by_key.get(k)
        if d is None:
            errors.append(f"{k}: not an attribute of this category")
            continue
        t = d["type"]
        ok = (
            (t == "text" and isinstance(v, str) and len(v) <= 200)
            or (t == "number" and isinstance(v, int | float) and not isinstance(v, bool))
            or (t == "boolean" and isinstance(v, bool))
            or (t == "select" and v in d["options"])
            or (
                t == "multiselect"
                and isinstance(v, list)
                and v
                and all(x in d["options"] for x in v)
            )
        )
        if not ok:
            errors.append(f"{k}: invalid value")
        else:
            clean[k] = v
    if require_complete:
        errors += [f"{d['key']}: required" for d in defs if d["required"] and d["key"] not in clean]
    if errors:
        raise AppError("; ".join(errors), status=422, code="invalid_attributes")
    return clean
