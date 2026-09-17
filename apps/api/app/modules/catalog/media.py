"""Product image pipeline (brainstorm §6, architecture §6).

upload -> size gate -> magic bytes -> bomb guard -> checksum dedupe -> EXIF/GPS strip + sRGB ->
Lanczos downscale to 160/400/800/1600 (never upscale) -> AVIF q55 + WebP q82 + one JPEG 800 ->
16px blur placeholder -> immutable random keys -> one DB row with the rendition map.

The request only stores the source bytes and enqueues; decoding happens in a worker (or a
threadpool in dev/test). Originals are never served.
"""

import base64
import hashlib
import io
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from PIL import Image, ImageCms, ImageOps
from pydantic import BaseModel
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core import audit
from app.core.cache_keys import object_key
from app.core.deps import Tenant, TenantDB, require_vendor_role
from app.core.errors import AppError, NotFound
from app.core.ratelimit import hit
from app.core.security import Principal

router = APIRouter(prefix="/api/v1/vendor/media", tags=["vendor:media"])
VendorCatalog = Annotated[Principal, Depends(require_vendor_role("catalog.write", approved=True))]

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
WIDTHS = (160, 400, 800, 1600)
CACHE = "public, max-age=31536000, immutable"


class InvalidImage(AppError):
    status = 422
    code = "invalid_image"


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypavif", b"ftypheic", b"ftypmif1"):
        return "avif"
    return None


def render(data: bytes) -> dict:
    """Pure CPU work. Returns renditions as bytes plus metadata."""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise InvalidImage("Image is too large")
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.load()
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise InvalidImage("File is not a supported image") from exc
    icc = img.info.get("icc_profile")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            img = ImageCms.profileToProfile(
                img, src, ImageCms.createProfile("sRGB"), outputMode="RGB"
            )
        except (ImageCms.PyCMSError, OSError):
            img = img.convert("RGB")
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert(
        "RGBA" if has_alpha else "RGB"
    )  # drops EXIF/XMP/GPS: new image, no metadata carried
    out: dict = {"width": img.width, "height": img.height, "files": {}}
    for w in WIDTHS:
        if w > img.width and w != WIDTHS[0]:
            continue  # never upscale (smallest is always produced)
        target = (
            img
            if w >= img.width
            else img.resize(
                (w, max(1, round(img.height * w / img.width))), Image.Resampling.LANCZOS
            )
        )
        for fmt, kw in (
            ("avif", {"quality": 55, "speed": 6}),
            ("webp", {"quality": 82, "method": 5}),
        ):
            buf = io.BytesIO()
            target.save(buf, fmt.upper(), **kw)
            out["files"][(fmt, target.width)] = buf.getvalue()
    fallback = (
        img
        if img.width <= 800
        else img.resize((800, round(img.height * 800 / img.width)), Image.Resampling.LANCZOS)
    )
    buf = io.BytesIO()
    fallback.convert("RGB").save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    out["files"][("jpeg", fallback.width)] = buf.getvalue()
    tiny = img.convert("RGB").resize(
        (16, max(1, round(16 * img.height / img.width))), Image.Resampling.BILINEAR
    )
    buf = io.BytesIO()
    tiny.save(buf, "WEBP", quality=40)
    out["blur"] = "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    return out


async def process_asset(db, storage, private_storage, *, tenant_id: str, asset_id: str) -> dict:
    """Worker body (ARQ `process_media`). Idempotent: a ready asset is left alone."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, vendor_id, status, source_key FROM media_assets WHERE id = :a AND tenant_id = :t FOR UPDATE"
                ),
                {"a": asset_id, "t": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["status"] == "ready":
        return {"status": row["status"] if row else "missing"}
    data = (
        private_storage.path(row["source_key"]).read_bytes()
        if hasattr(private_storage, "path")
        else await private_storage.read(row["source_key"])
    )
    checksum = hashlib.sha256(data).hexdigest()
    dup = (
        (
            await db.execute(
                text(
                    """SELECT id, width, height, renditions, blur_data FROM media_assets
           WHERE tenant_id = :t AND coalesce(vendor_id, tenant_id) = coalesce(CAST(:v AS uuid), tenant_id)
             AND checksum = :c AND status = 'ready' AND id <> :a"""
                ),
                {"t": tenant_id, "v": row["vendor_id"], "c": checksum, "a": asset_id},
            )
        )
        .mappings()
        .first()
    )
    if dup:
        await db.execute(
            text(
                """UPDATE media_assets SET status = 'ready', width = :w, height = :h, renditions = :r, blur_data = :b,
                   checksum = NULL, error = 'duplicate of ' || :d WHERE id = :a AND tenant_id = :t"""
            ),
            {
                "w": dup["width"],
                "h": dup["height"],
                "r": _json(dup["renditions"]),
                "b": dup["blur_data"],
                "d": str(dup["id"]),
                "a": asset_id,
                "t": tenant_id,
            },
        )
        return {"status": "ready", "deduplicated_from": str(dup["id"])}
    try:
        result = await run_in_threadpool(render, data)
    except InvalidImage as exc:
        await db.execute(
            text(
                "UPDATE media_assets SET status = 'failed', error = :e WHERE id = :a AND tenant_id = :t"
            ),
            {"e": exc.detail, "a": asset_id, "t": tenant_id},
        )
        return {"status": "failed"}
    stem = secrets.token_urlsafe(12)
    renditions: dict = {"avif": {}, "webp": {}, "jpeg": {}}
    for (fmt, width), blob in result["files"].items():
        key = object_key(tenant_id, "media", f"{stem}-{width}.{'jpg' if fmt == 'jpeg' else fmt}")
        await storage.put(key, blob, content_type=f"image/{fmt}", cache_control=CACHE)
        renditions[fmt][str(width)] = storage.public_path(key)
    await db.execute(
        text(
            """UPDATE media_assets SET status = 'ready', checksum = :c, width = :w, height = :h,
               renditions = CAST(:r AS jsonb), blur_data = :b, byte_size = :sz WHERE id = :a AND tenant_id = :t"""
        ),
        {
            "c": checksum,
            "w": result["width"],
            "h": result["height"],
            "r": _json(renditions),
            "b": result["blur"],
            "sz": len(data),
            "a": asset_id,
            "t": tenant_id,
        },
    )
    return {"status": "ready"}


def _json(v) -> str:
    import json

    return json.dumps(v)


class AssetOut(BaseModel):
    id: uuid.UUID
    status: str
    width: int | None
    height: int | None
    renditions: dict
    blur_data: str | None
    error: str | None


@router.post("", response_model=AssetOut, status_code=202)
async def upload(
    request: Request, p: VendorCatalog, tenant: Tenant, db: TenantDB, file: UploadFile = File(...)
):
    await hit(request.app.state.redis, f"t:{tenant.id}:rl:media:{p.vid}", 300, 3600)
    data = await file.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise InvalidImage("Images must be 8 MB or smaller")
    if sniff(data) is None:
        raise InvalidImage("Use a JPEG, PNG, WebP or AVIF image")
    source_key = object_key(tenant.id, "media-src", p.vid, f"{uuid.uuid4().hex}")
    storage = request.app.state.private_storage
    path = storage.path(source_key) if hasattr(storage, "path") else None
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        await run_in_threadpool(path.write_bytes, data)
    else:
        await storage.write(source_key, data)
    asset_id = (
        await db.execute(
            text(
                """INSERT INTO media_assets (tenant_id, vendor_id, status, source_key, created_by)
           VALUES (:t, :v, 'processing', :k, :a) RETURNING id"""
            ),
            {"t": tenant.id, "v": p.vid, "k": source_key, "a": p.sub},
        )
    ).scalar()
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="media.upload",
        entity="media_asset",
        entity_id=asset_id,
        request=request,
    )
    await request.app.state.media_queue.enqueue(db, tenant_id=tenant.id, asset_id=str(asset_id))
    row = (
        (
            await db.execute(
                text("SELECT * FROM media_assets WHERE id = :a AND tenant_id = :t"),
                {"a": asset_id, "t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    return AssetOut(**{k: row[k] for k in AssetOut.model_fields})


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(asset_id: uuid.UUID, p: VendorCatalog, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM media_assets WHERE id = :a AND tenant_id = :t AND vendor_id = :v"
                ),
                {"a": asset_id, "t": tenant.id, "v": p.vid},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return AssetOut(**{k: row[k] for k in AssetOut.model_fields})


class ArqMediaQueue:
    """Production: enqueue after commit to the tenant-aware worker."""

    def __init__(self, pool):
        self.pool = pool

    async def enqueue(self, db, *, tenant_id: str, asset_id: str) -> None:
        from app.workers.settings import enqueue_tenant_job

        await enqueue_tenant_job(self.pool, "process_media", tenant_id=tenant_id, asset_id=asset_id)


class InlineMediaQueue:
    """Dev/test: process in the same transaction (still off the event loop via threadpool)."""

    def __init__(self, app):
        self.app = app

    async def enqueue(self, db, *, tenant_id: str, asset_id: str) -> None:
        await process_asset(
            db,
            self.app.state.storage,
            self.app.state.private_storage,
            tenant_id=tenant_id,
            asset_id=asset_id,
        )
