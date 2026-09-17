import json
import time
import uuid

import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_keys import tkey
from app.core.config import Settings
from app.core.errors import AppError, NotFound
from app.modules.theme.contrast import check_tokens
from app.modules.theme.presets import PRESETS, preset_document
from app.modules.theme.schema import ThemeDocument

PUBLISHED_TTL = 3600


class ContrastError(AppError):
    status = 422
    code = "contrast_too_low"


def validate_document(doc: ThemeDocument, tenant_id: str) -> None:
    prefix = f"/media/t/{tenant_id}/"
    for url in (doc.brand.logo_url, doc.brand.logo_dark_url, doc.brand.favicon_url):
        if url and not url.startswith(prefix):
            raise AppError("Image does not belong to this store", status=422, code="invalid_asset")


async def ensure_theme(db: AsyncSession, tenant_id: str, actor: str = "system") -> dict:
    row = (
        (
            await db.execute(
                text("SELECT * FROM tenant_themes WHERE tenant_id = :t"), {"t": tenant_id}
            )
        )
        .mappings()
        .first()
    )
    if row:
        return dict(row)
    doc = preset_document("minimal").model_dump(mode="json", by_alias=True)
    await db.execute(
        text(
            "INSERT INTO tenant_themes (tenant_id, draft_document, draft_updated_by) "
            "VALUES (:t, CAST(:d AS jsonb), :a)"
        ),
        {"t": tenant_id, "d": json.dumps(doc), "a": actor},
    )
    version = await _insert_version(db, tenant_id, doc, actor, "initial preset")
    await db.execute(
        text("UPDATE tenant_themes SET published_version_id = :v WHERE tenant_id = :t"),
        {"v": version["id"], "t": tenant_id},
    )
    return dict(
        (
            await db.execute(
                text("SELECT * FROM tenant_themes WHERE tenant_id = :t"), {"t": tenant_id}
            )
        )
        .mappings()
        .one()
    )


async def _insert_version(db, tenant_id: str, doc: dict, actor: str, note: str | None) -> dict:
    number = (
        await db.execute(
            text("SELECT coalesce(max(number), 0) + 1 FROM theme_versions WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar()
    row = (
        (
            await db.execute(
                text("""INSERT INTO theme_versions (tenant_id, number, document, note, created_by)
                VALUES (:t, :n, CAST(:d AS jsonb), :note, :a) RETURNING id, number, created_at"""),
                {"t": tenant_id, "n": number, "d": json.dumps(doc), "note": note, "a": actor},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def save_draft(db, tenant_id: str, doc: ThemeDocument, actor: str) -> None:
    validate_document(doc, tenant_id)
    await ensure_theme(db, tenant_id, actor)
    await db.execute(
        text(
            "UPDATE tenant_themes SET draft_document = CAST(:d AS jsonb), draft_updated_at = now(), "
            "draft_updated_by = :a WHERE tenant_id = :t"
        ),
        {"d": json.dumps(doc.model_dump(mode="json", by_alias=True)), "a": actor, "t": tenant_id},
    )


async def apply_preset(db, tenant_id: str, key: str, actor: str) -> ThemeDocument:
    if key not in PRESETS:
        raise NotFound("Preset not found")
    current = await ensure_theme(db, tenant_id, actor)
    doc = preset_document(key)
    doc.brand = ThemeDocument.model_validate(current["draft_document"]).brand  # keep the logo
    await save_draft(db, tenant_id, doc, actor)
    return doc


async def publish(db, redis, tenant_id: str, actor: str, note: str | None) -> dict:
    theme = await ensure_theme(db, tenant_id, actor)
    doc = ThemeDocument.model_validate(theme["draft_document"])
    validate_document(doc, tenant_id)
    issues = check_tokens(doc.tokens)
    if issues:
        detail = "; ".join(
            f"{i.mode}: {i.foreground} on {i.background} is {i.ratio}:1, needs {i.required}:1"
            for i in issues
        )
        raise ContrastError(detail)
    version = await _insert_version(
        db, tenant_id, doc.model_dump(mode="json", by_alias=True), actor, note
    )
    await db.execute(
        text("UPDATE tenant_themes SET published_version_id = :v WHERE tenant_id = :t"),
        {"v": version["id"], "t": tenant_id},
    )
    await redis.delete(tkey(tenant_id, "theme", "published"))
    return version


async def restore(db, redis, tenant_id: str, version_id: str, actor: str) -> dict:
    try:
        vid = uuid.UUID(version_id)
    except ValueError as exc:
        raise NotFound("Not found") from exc
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, number, document FROM theme_versions WHERE id = :v AND tenant_id = :t"
                ),
                {"v": vid, "t": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    await ensure_theme(db, tenant_id, actor)
    # Rollback is a pointer flip; the draft follows so the next edit starts from what is live.
    await db.execute(
        text(
            "UPDATE tenant_themes SET published_version_id = :v, draft_document = CAST(:d AS jsonb), "
            "draft_updated_at = now(), draft_updated_by = :a WHERE tenant_id = :t"
        ),
        {"v": vid, "d": json.dumps(row["document"]), "a": actor, "t": tenant_id},
    )
    await redis.delete(tkey(tenant_id, "theme", "published"))
    return {"id": row["id"], "number": row["number"]}


async def published_document(db, redis, tenant_id: str) -> dict:
    key = tkey(tenant_id, "theme", "published")
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    theme = await ensure_theme(db, tenant_id)
    row = (
        (
            await db.execute(
                text(
                    "SELECT number, document FROM theme_versions WHERE id = :v AND tenant_id = :t"
                ),
                {"v": theme["published_version_id"], "t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    payload = {"version": row["number"], "document": row["document"]}
    await redis.set(key, json.dumps(payload), ex=PUBLISHED_TTL)
    return payload


def preview_token(settings: Settings, tenant_id: str, ttl: int = 900) -> str:
    now = int(time.time())
    return jwt.encode(
        {"typ": "theme_preview", "tid": tenant_id, "iat": now, "exp": now + ttl},
        settings.jwt_secret,
        algorithm="HS256",
    )


def preview_allowed(settings: Settings, token: str, tenant_id: str) -> bool:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return data.get("typ") == "theme_preview" and data.get("tid") == tenant_id
