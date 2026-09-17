"""Tenant resolution from Host (architecture §3.1, §4) and RLS session scoping."""

import json
import re
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound

HOST_CACHE_TTL = 60
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")


class TenantUnavailable(AppError):
    status = 503
    code = "store_unavailable"


@dataclass(frozen=True)
class TenantContext:
    id: str
    status: str
    host: str
    primary_host: str | None
    is_primary: bool


def normalise_host(raw: str | None) -> str:
    host = (raw or "").strip().lower().split(",")[0].strip()
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.rstrip(".")
    if not host or not _HOST_RE.match(host):
        raise NotFound("Store not found")
    return host


def request_host(request: Request) -> str:
    # Caddy sets X-Forwarded-Host; uvicorn runs with --proxy-headers behind trusted proxies only.
    return normalise_host(request.headers.get("x-forwarded-host") or request.headers.get("host"))


async def resolve_tenant(request: Request, host: str) -> TenantContext:
    redis = request.app.state.redis
    cache_key = f"host:{host}"  # global map host->tenant; contains no tenant data
    cached = await redis.get(cache_key)
    if cached is not None:
        data = json.loads(cached)
    else:
        async with request.app.state.db.engine.connect() as conn:
            row = (
                (await conn.execute(text("SELECT * FROM resolve_tenant_by_host(:h)"), {"h": host}))
                .mappings()
                .first()
            )
        data = (
            {
                "id": str(row["tenant_id"]),
                "status": row["tenant_status"],
                "domain_status": row["domain_status"],
                "is_primary": row["is_primary"],
                "primary_host": row["primary_host"],
            }
            if row
            else {}
        )
        await redis.set(cache_key, json.dumps(data), ex=HOST_CACHE_TTL)
    if not data or data["domain_status"] != "active" or data["status"] in ("cancelled", "purged"):
        raise NotFound("Store not found")
    return TenantContext(
        id=data["id"],
        status=data["status"],
        host=host,
        primary_host=data["primary_host"],
        is_primary=data["is_primary"],
    )


async def invalidate_host(redis, *hosts: str) -> None:
    if hosts:
        await redis.delete(*(f"host:{h}" for h in hosts))


async def scope_session(
    session: AsyncSession, tenant_id: str, vendor_id: str | None = None
) -> None:
    """Transaction-local RLS variables. Must run inside an open transaction."""
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, true), set_config('app.vendor_id', :v, true)"),
        {"t": str(tenant_id), "v": str(vendor_id) if vendor_id else ""},
    )
