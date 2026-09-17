import hashlib
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.deps import Tenant, TenantDB, require_tenant_staff
from app.core.security import Principal
from app.modules.theme import service
from app.modules.theme.contrast import check_tokens
from app.modules.theme.images import MAX_BYTES, InvalidImage, store_brand_image
from app.modules.theme.presets import PRESETS
from app.modules.theme.schema import ThemeDocument

public = APIRouter(prefix="/api/v1/theme", tags=["theme"])
admin = APIRouter(prefix="/api/v1/admin/theme", tags=["admin:theme"])

Editor = Annotated[Principal, Depends(require_tenant_staff("theme.edit"))]
Publisher = Annotated[Principal, Depends(require_tenant_staff("theme.publish"))]


class PublishedTheme(BaseModel):
    version: int
    document: ThemeDocument
    preview: bool = False


class AdminTheme(BaseModel):
    draft: ThemeDocument
    draft_updated_at: datetime
    published_version: int | None
    unpublished_changes: bool
    contrast_issues: list[dict]


class PresetOut(BaseModel):
    key: str
    name: str
    document: ThemeDocument


class VersionOut(BaseModel):
    id: str
    number: int
    note: str | None
    created_by: str
    created_at: datetime
    is_published: bool


class PublishIn(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class PresetIn(BaseModel):
    preset: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")


@public.get("", response_model=PublishedTheme)
async def get_theme(
    request: Request,
    response: Response,
    tenant: Tenant,
    db: TenantDB,
    preview: str | None = Query(default=None, max_length=1024),
):
    settings = request.app.state.settings
    if preview and service.preview_allowed(settings, preview, tenant.id):
        theme = await service.ensure_theme(db, tenant.id)
        response.headers["cache-control"] = "no-store"
        return PublishedTheme(version=0, document=theme["draft_document"], preview=True)
    payload = await service.published_document(db, request.app.state.redis, tenant.id)
    etag = '"' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"etag": etag})
    response.headers["etag"] = etag
    response.headers["cache-control"] = "public, max-age=60, stale-while-revalidate=600"
    return PublishedTheme(**payload)


@public.get("/presets", response_model=list[PresetOut])
async def presets():
    from app.modules.theme.presets import preset_document

    return [
        PresetOut(key=k, name=v["name"], document=preset_document(k)) for k, v in PRESETS.items()
    ]


@admin.get("", response_model=AdminTheme)
async def admin_theme(_: Editor, tenant: Tenant, db: TenantDB):
    theme = await service.ensure_theme(db, tenant.id)
    draft = ThemeDocument.model_validate(theme["draft_document"])
    published = (
        (
            await db.execute(
                text(
                    "SELECT number, document FROM theme_versions WHERE id = :v AND tenant_id = :t"
                ),
                {"v": theme["published_version_id"], "t": tenant.id},
            )
        )
        .mappings()
        .first()
    )
    return AdminTheme(
        draft=draft,
        draft_updated_at=theme["draft_updated_at"],
        published_version=published["number"] if published else None,
        unpublished_changes=not published or published["document"] != theme["draft_document"],
        contrast_issues=[i.__dict__ for i in check_tokens(draft.tokens)],
    )


@admin.put("/draft", response_model=ThemeDocument)
async def put_draft(
    body: ThemeDocument, request: Request, actor: Editor, tenant: Tenant, db: TenantDB
):
    await service.save_draft(db, tenant.id, body, actor.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="theme.draft",
        entity="theme",
        data={"preset": body.preset},
        request=request,
    )
    return body


@admin.post("/preset", response_model=ThemeDocument)
async def use_preset(body: PresetIn, request: Request, actor: Editor, tenant: Tenant, db: TenantDB):
    doc = await service.apply_preset(db, tenant.id, body.preset, actor.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="theme.preset",
        entity="theme",
        data={"preset": body.preset},
        request=request,
    )
    return doc


@admin.post("/publish", response_model=VersionOut)
async def publish(
    body: PublishIn, request: Request, actor: Publisher, tenant: Tenant, db: TenantDB
):
    v = await service.publish(db, request.app.state.redis, tenant.id, actor.sub, body.note)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="theme.publish",
        entity="theme_version",
        entity_id=v["id"],
        data={"number": v["number"]},
        request=request,
    )
    return VersionOut(
        id=str(v["id"]),
        number=v["number"],
        note=body.note,
        created_by=actor.sub,
        created_at=v["created_at"],
        is_published=True,
    )


@admin.get("/versions", response_model=list[VersionOut])
async def versions(_: Editor, tenant: Tenant, db: TenantDB, limit: int = Query(30, ge=1, le=100)):
    theme = await service.ensure_theme(db, tenant.id)
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, number, note, created_by, created_at FROM theme_versions "
                    "WHERE tenant_id = :t ORDER BY number DESC LIMIT :l"
                ),
                {"l": limit, "t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [
        VersionOut(
            id=str(r["id"]),
            number=r["number"],
            note=r["note"],
            created_by=r["created_by"],
            created_at=r["created_at"],
            is_published=r["id"] == theme["published_version_id"],
        )
        for r in rows
    ]


@admin.post("/versions/{version_id}/restore", response_model=VersionOut)
async def restore(
    version_id: str, request: Request, actor: Publisher, tenant: Tenant, db: TenantDB
):
    v = await service.restore(db, request.app.state.redis, tenant.id, version_id, actor.sub)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="theme.rollback",
        entity="theme_version",
        entity_id=v["id"],
        data={"number": v["number"]},
        request=request,
    )
    row = (
        (
            await db.execute(
                text(
                    "SELECT note, created_by, created_at FROM theme_versions WHERE id=:v AND tenant_id=:t"
                ),
                {"v": v["id"], "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return VersionOut(
        id=str(v["id"]),
        number=v["number"],
        note=row["note"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        is_published=True,
    )


class PreviewOut(BaseModel):
    token: str
    expires_in: int


@admin.post("/preview-token", response_model=PreviewOut)
async def preview_token(request: Request, _: Editor, tenant: Tenant):
    return PreviewOut(
        token=service.preview_token(request.app.state.settings, tenant.id), expires_in=900
    )


class UploadOut(BaseModel):
    url: str
    fallback_url: str
    width: int
    height: int


@admin.post("/images", response_model=UploadOut, status_code=201)
async def upload_brand_image(
    request: Request, actor: Editor, tenant: Tenant, db: TenantDB, file: UploadFile = File(...)
):
    data = await file.read(MAX_BYTES + 1)
    if not data:
        raise InvalidImage("Empty file")
    out = await store_brand_image(request.app.state.storage, tenant.id, data)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="theme.image_upload",
        entity="asset",
        data={"url": out["url"]},
        request=request,
    )
    return UploadOut(**out)
