"""Boundary dependencies (architecture §1). Each boundary has its own dependency; none is a flag."""

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unauthorized
from app.core.security import Principal, decode_access_token
from app.core.tenancy import TenantContext, request_host, resolve_tenant, scope_session


async def current_tenant(request: Request) -> TenantContext:
    if getattr(request.state, "tenant", None) is None:
        request.state.tenant = await resolve_tenant(request, request_host(request))
    return request.state.tenant


Tenant = Annotated[TenantContext, Depends(current_tenant)]


async def tenant_db(request: Request, tenant: Tenant) -> AsyncIterator[AsyncSession]:
    """Session with app.tenant_id set for the whole request transaction."""
    async with request.app.state.db.sessionmaker() as session:
        async with session.begin():
            await scope_session(session, tenant.id)
            yield session


TenantDB = Annotated[AsyncSession, Depends(tenant_db)]


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Authentication required")
    return token


async def current_principal(request: Request, tenant: Tenant) -> Principal:
    try:
        p = decode_access_token(request.app.state.settings, _bearer(request))
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token") from exc
    # A token minted for store A is dead on store B's host (architecture §3.1 rule 1).
    if p.kind == "platform" or p.tid != tenant.id:
        raise Unauthorized("Invalid token")
    return p


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


async def current_vendor(p: CurrentPrincipal, tenant: Tenant, session: TenantDB) -> Principal:
    if p.kind != "vendor_staff" or not p.vid:
        raise Forbidden("Vendor access required")
    await scope_session(session, tenant.id, p.vid)
    return p


CurrentVendor = Annotated[Principal, Depends(current_vendor)]


def require_tenant_staff(*permissions: str):
    async def dep(p: CurrentPrincipal) -> Principal:
        if p.kind != "tenant_staff":
            raise Forbidden("Staff access required")
        # Phase 3 replaces role check with permission strings; owner holds all.
        if "owner" not in p.roles and not set(permissions) <= set(p.roles):
            raise Forbidden("Missing permission")
        return p

    return dep


async def require_platform_admin(request: Request) -> Principal:
    try:
        p = decode_access_token(request.app.state.settings, _bearer(request))
    except jwt.PyJWTError as exc:
        raise Unauthorized("Invalid token") from exc
    if p.kind != "platform":
        raise NotFound("Not found")  # the platform surface does not exist for others
    return p


PlatformAdmin = Annotated[Principal, Depends(require_platform_admin)]


async def platform_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.platform_db.sessionmaker() as session:
        async with session.begin():
            yield session


PlatformDB = Annotated[AsyncSession, Depends(platform_db)]
