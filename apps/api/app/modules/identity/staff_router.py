"""Tenant staff management, vendor staff management, audit viewer."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core import audit
from app.core.deps import Tenant, TenantDB, require_tenant_staff, require_vendor_role
from app.core.errors import Conflict, NotFound
from app.core.phone import normalize_bd_phone
from app.core.security import Principal
from app.modules.billing.service import require_quota
from app.modules.identity.models import StaffMember, StaffRole, User, VendorUser

admin = APIRouter(prefix="/api/v1/admin", tags=["admin:staff"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:staff"])

StaffManager = Annotated[Principal, Depends(require_tenant_staff("staff.manage"))]
AuditReader = Annotated[Principal, Depends(require_tenant_staff("audit.read"))]
VendorStaffManager = Annotated[Principal, Depends(require_vendor_role("staff.manage"))]


class RoleOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    permissions: list[str]
    is_system: bool


class StaffInviteIn(BaseModel):
    email: EmailStr | None = None
    phone: str | None = None
    full_name: str | None = Field(default=None, max_length=120)
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")


class StaffOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str | None
    phone: str | None
    role: str
    status: str


class StaffPatch(BaseModel):
    role: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,40}$")
    status: Literal["active", "disabled"] | None = None


async def _find_or_create_user(db, tenant_id: str, email, phone, full_name) -> User:
    if not email and not phone:
        raise Conflict("Email or phone required")
    norm_phone = normalize_bd_phone(phone) if phone else None
    if phone and not norm_phone:
        raise Conflict("Invalid phone")
    cond = User.email == email.lower() if email else User.phone == norm_phone
    user = (await db.execute(select(User).where(cond))).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=uuid.UUID(tenant_id),
            email=email.lower() if email else None,
            phone=norm_phone,
            full_name=full_name,
        )
        db.add(user)
        await db.flush()
    return user


async def _staff_out(db, member_id) -> StaffOut:
    row = (
        await db.execute(
            select(
                StaffMember.id,
                StaffMember.user_id,
                User.email,
                User.phone,
                StaffRole.key,
                StaffMember.status,
            )
            .join(User, User.id == StaffMember.user_id)
            .join(StaffRole, StaffRole.id == StaffMember.role_id)
            .where(StaffMember.id == member_id)
        )
    ).one()
    return StaffOut(
        id=row[0], user_id=row[1], email=row[2], phone=row[3], role=row[4], status=row[5]
    )


@admin.get("/roles", response_model=list[RoleOut])
async def list_roles(_: StaffManager, db: TenantDB):
    rows = (await db.execute(select(StaffRole).order_by(StaffRole.key))).scalars()
    return [
        RoleOut(
            id=r.id, key=r.key, name=r.name, permissions=list(r.permissions), is_system=r.is_system
        )
        for r in rows
    ]


@admin.get("/staff", response_model=list[StaffOut])
async def list_staff(_: StaffManager, db: TenantDB):
    ids = (await db.execute(select(StaffMember.id).order_by(StaffMember.id))).scalars().all()
    return [await _staff_out(db, i) for i in ids]


@admin.post("/staff", response_model=StaffOut, status_code=201)
async def invite_staff(
    body: StaffInviteIn, request: Request, actor: StaffManager, tenant: Tenant, db: TenantDB
):
    role = (
        await db.execute(select(StaffRole).where(StaffRole.key == body.role))
    ).scalar_one_or_none()
    if role is None:
        raise NotFound("Role not found")
    if role.key == "owner":
        raise Conflict("Ownership is transferred, not invited")
    used = (
        await db.execute(
            text("SELECT count(*) FROM staff_members WHERE tenant_id = :t AND status = 'active'"),
            {"t": tenant.id},
        )
    ).scalar()
    await require_quota(db, tenant.id, "staff", used)
    user = await _find_or_create_user(db, tenant.id, body.email, body.phone, body.full_name)
    m = StaffMember(tenant_id=uuid.UUID(tenant.id), user_id=user.id, role_id=role.id)
    db.add(m)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("Already a staff member") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="staff.invite",
        entity="staff",
        entity_id=m.id,
        data={"role": role.key, "user_id": str(user.id)},
        request=request,
    )
    return await _staff_out(db, m.id)


@admin.patch("/staff/{member_id}", response_model=StaffOut)
async def update_staff(
    member_id: str,
    body: StaffPatch,
    request: Request,
    actor: StaffManager,
    tenant: Tenant,
    db: TenantDB,
):
    try:
        mid = uuid.UUID(member_id)
    except ValueError as exc:
        raise NotFound("Not found") from exc
    m = (await db.execute(select(StaffMember).where(StaffMember.id == mid))).scalar_one_or_none()
    if m is None:
        raise NotFound("Not found")
    current = (
        await db.execute(select(StaffRole.key).where(StaffRole.id == m.role_id))
    ).scalar_one()
    if current == "owner":
        raise Conflict("The owner cannot be changed here")
    if str(m.user_id) == actor.sub:
        raise Conflict("You cannot change your own access")
    changes = {}
    if body.role:
        role = (
            await db.execute(select(StaffRole).where(StaffRole.key == body.role))
        ).scalar_one_or_none()
        if role is None or role.key == "owner":
            raise NotFound("Role not found")
        m.role_id = role.id
        changes["role"] = role.key
    if body.status:
        m.status = body.status
        changes["status"] = body.status
    await db.flush()
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="staff.update",
        entity="staff",
        entity_id=m.id,
        data=changes,
        request=request,
    )
    return await _staff_out(db, m.id)


class AuditOut(BaseModel):
    id: uuid.UUID
    actor_kind: str
    actor_id: str
    vendor_id: uuid.UUID | None
    action: str
    entity: str
    entity_id: str | None
    data: dict
    created_at: datetime


@admin.get("/audit", response_model=list[AuditOut])
async def audit_log(
    _: AuditReader,
    db: TenantDB,
    entity: str | None = Query(None, max_length=60),
    actor_id: str | None = Query(None, max_length=64),
    before: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    sql = "SELECT id, actor_kind, actor_id, vendor_id, action, entity, entity_id, data, created_at FROM audit_log WHERE true"
    params: dict = {"limit": limit}
    if entity:
        sql += " AND entity = :entity"
        params["entity"] = entity
    if actor_id:
        sql += " AND actor_id = :actor"
        params["actor"] = actor_id
    if before:
        sql += " AND id < :before"
        params["before"] = before
    sql += " ORDER BY id DESC LIMIT :limit"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [AuditOut(**r) for r in rows]


# ---- vendor staff ------------------------------------------------------------------------------------------
class VendorStaffIn(BaseModel):
    email: EmailStr | None = None
    phone: str | None = None
    full_name: str | None = Field(default=None, max_length=120)
    role: Literal["manager", "staff"]


class VendorStaffOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str


class VendorStaffPatch(BaseModel):
    role: Literal["manager", "staff"] | None = None
    status: Literal["active", "disabled"] | None = None


@vendor.get("/staff", response_model=list[VendorStaffOut])
async def vendor_staff(p: VendorStaffManager, db: TenantDB):
    rows = (
        await db.execute(
            select(VendorUser)
            .where(VendorUser.vendor_id == uuid.UUID(p.vid))
            .order_by(VendorUser.id)
        )
    ).scalars()
    return [VendorStaffOut(id=r.id, user_id=r.user_id, role=r.role, status=r.status) for r in rows]


@vendor.post("/staff", response_model=VendorStaffOut, status_code=201)
async def add_vendor_staff(
    body: VendorStaffIn, request: Request, p: VendorStaffManager, tenant: Tenant, db: TenantDB
):
    if p.roles[0] == "manager" and body.role == "manager":
        raise Conflict("Only the owner can add managers")
    user = await _find_or_create_user(db, tenant.id, body.email, body.phone, body.full_name)
    vu = VendorUser(
        tenant_id=uuid.UUID(tenant.id), vendor_id=uuid.UUID(p.vid), user_id=user.id, role=body.role
    )
    db.add(vu)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("Already a member") from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor_staff.add",
        entity="vendor_user",
        entity_id=vu.id,
        data={"role": body.role},
        request=request,
    )
    return VendorStaffOut(id=vu.id, user_id=vu.user_id, role=vu.role, status=vu.status)


@vendor.patch("/staff/{member_id}", response_model=VendorStaffOut)
async def update_vendor_staff(
    member_id: str,
    body: VendorStaffPatch,
    request: Request,
    p: VendorStaffManager,
    tenant: Tenant,
    db: TenantDB,
):
    try:
        mid = uuid.UUID(member_id)
    except ValueError as exc:
        raise NotFound("Not found") from exc
    vu = (
        await db.execute(
            select(VendorUser).where(VendorUser.id == mid, VendorUser.vendor_id == uuid.UUID(p.vid))
        )
    ).scalar_one_or_none()
    if vu is None:
        raise NotFound("Not found")
    if vu.role == "owner" or str(vu.user_id) == p.sub:
        raise Conflict("This member cannot be changed here")
    if p.roles[0] == "manager" and (vu.role == "manager" or body.role == "manager"):
        raise Conflict("Only the owner can manage managers")
    if body.role:
        vu.role = body.role
    if body.status:
        vu.status = body.status
    await db.flush()
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="vendor_staff.update",
        entity="vendor_user",
        entity_id=vu.id,
        data=body.model_dump(exclude_unset=True),
        request=request,
    )
    return VendorStaffOut(id=vu.id, user_id=vu.user_id, role=vu.role, status=vu.status)
