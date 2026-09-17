"""The build runner's endpoints. Platform-admin only, and a 404 to everyone else."""

import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.deps import PlatformAdmin, PlatformDB
from app.modules.mobile import builds

router = APIRouter(prefix="/platform/v1", tags=["platform:builds"])


class ClaimIn(BaseModel):
    runner: str = Field(min_length=3, max_length=120)
    limit: int = Field(default=1, ge=1, le=10)


class ReportIn(BaseModel):
    status: Literal["building", "succeeded", "failed", "uploaded", "rejected"]
    artifact_url: str | None = Field(default=None, max_length=500)
    store_status: str | None = Field(default=None, max_length=120)
    error: str | None = Field(default=None, max_length=1000)


@router.post("/app-builds/claim")
async def claim(body: ClaimIn, p: PlatformAdmin, db: PlatformDB):
    """One runner claims the next queued build(s); two runners never get the same one."""
    return await builds.claim_next(db, runner=body.runner, limit=body.limit)


@router.post("/app-builds/{build_id}/report")
async def report(build_id: uuid.UUID, body: ReportIn, p: PlatformAdmin, db: PlatformDB):
    return await builds.report(
        db,
        build_id,
        status=body.status,
        artifact_url=body.artifact_url,
        store_status=body.store_status,
        error=body.error,
    )


@router.get("/app-builds")
async def queue(p: PlatformAdmin, db: PlatformDB, status: str | None = None, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT b.id, b.tenant_id, t.slug, b.app, b.platform, b.version, b.build_number,
                              b.status, b.store_status, b.error, b.created_at, b.finished_at
                       FROM app_builds b JOIN tenants t ON t.id = b.tenant_id
                       WHERE (CAST(:s AS text) IS NULL OR b.status = :s)
                       ORDER BY b.created_at DESC LIMIT :l"""
                ),
                {"s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [{**dict(r), "tenant_id": str(r["tenant_id"])} for r in rows]


@router.get("/tenants/{tenant_id}/app-manifest")
async def manifest(
    tenant_id: uuid.UUID,
    p: PlatformAdmin,
    db: PlatformDB,
    app: Literal["buyer", "vendor"] = "buyer",
):
    return await builds.manifest(db, str(tenant_id), app=app)
