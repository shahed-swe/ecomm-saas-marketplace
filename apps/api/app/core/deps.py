"""Boundary dependencies (architecture §1). Each boundary has its own dependency; none is a flag."""

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unauthorized
from app.core.permissions import VENDOR_ROLE_PERMISSIONS, has_permission
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
    """Vendor boundary: membership and vendor state are re-checked on every request, so a removed
    staff member or suspended vendor loses access before the access token expires."""
    if p.kind != "vendor_staff" or not p.vid:
        raise Forbidden("Vendor access required")
    row = (
        await session.execute(
            text("""SELECT vu.role, v.status FROM vendor_users vu JOIN vendors v ON v.id = vu.vendor_id
                WHERE vu.user_id = CAST(:u AS uuid) AND vu.vendor_id = CAST(:v AS uuid)
                  AND vu.status = 'active'"""),
            {"u": p.sub, "v": p.vid},
        )
    ).first()
    if row is None or row.status in ("rejected", "closed"):
        raise Unauthorized("Invalid token")
    await scope_session(session, tenant.id, p.vid)
    return Principal(sub=p.sub, kind=p.kind, tid=p.tid, vid=p.vid, roles=(row.role,))


CurrentVendor = Annotated[Principal, Depends(current_vendor)]


def require_tenant_staff(*permissions: str):
    """Permissions come from the database on every request (never trusted from the token)."""

    async def dep(p: CurrentPrincipal, session: TenantDB) -> Principal:
        if p.kind != "tenant_staff":
            raise Forbidden("Staff access required")
        row = (
            await session.execute(
                text("""SELECT r.key, r.permissions FROM staff_members m
                    JOIN staff_roles r ON r.id = m.role_id
                    WHERE m.user_id = CAST(:u AS uuid) AND m.status = 'active'"""),
                {"u": p.sub},
            )
        ).first()
        if row is None:
            raise Unauthorized("Invalid token")
        granted = set(row.permissions)
        if not all(has_permission(granted, perm) for perm in permissions):
            raise Forbidden("Missing permission")
        return Principal(sub=p.sub, kind=p.kind, tid=p.tid, roles=(row.key,))

    return dep


def require_vendor_role(*perms: str):
    async def dep(p: CurrentVendor) -> Principal:
        granted = VENDOR_ROLE_PERMISSIONS.get(p.roles[0] if p.roles else "", set())
        if not all(has_permission(granted, perm) for perm in perms):
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
