"""Authentication flows (ADR 0014). All queries run inside a tenant-scoped session (RLS)."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, Conflict, Unauthorized
from app.core.passwords import hash_password, verify_password
from app.core.permissions import SYSTEM_ROLES
from app.core.security import Principal, create_access_token
from app.modules.identity.models import (
    OtpChallenge,
    RefreshToken,
    StaffMember,
    StaffRole,
    User,
    VendorUser,
)
from app.modules.vendors.models import Vendor


class InvalidOtp(AppError):
    status = 400
    code = "invalid_otp"


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    kind: str
    expires_in: int


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


# ---- tenant bootstrap ------------------------------------------------------------------------
async def seed_system_roles(db: AsyncSession, tenant_id) -> dict[str, uuid.UUID]:
    ids = {}
    for key, (name, perms) in SYSTEM_ROLES.items():
        role = StaffRole(tenant_id=tenant_id, key=key, name=name, permissions=perms, is_system=True)
        db.add(role)
        await db.flush()
        ids[key] = role.id
    return ids


# ---- users -------------------------------------------------------------------------------------
async def register_buyer(
    db: AsyncSession, tenant_id, *, email: str, password: str, full_name: str | None
) -> User:
    user = User(
        tenant_id=tenant_id,
        email=email.lower(),
        password_hash=hash_password(password),
        full_name=full_name,
    )
    db.add(user)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise Conflict("Could not create account") from exc  # no account enumeration detail
    return user


async def authenticate_password(db: AsyncSession, *, email: str, password: str) -> User:
    user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
    ok = verify_password(user.password_hash if user else None, password)
    if not user or not ok or user.status != "active":
        raise Unauthorized("Invalid credentials")
    user.last_login_at = _now()
    return user


# ---- surfaces: which token kind a user may receive ----------------------------------------------
async def principal_for(
    db: AsyncSession, user: User, surface: str, vendor_id: str | None = None
) -> Principal:
    tid = str(user.tenant_id)
    if surface == "buyer":
        return Principal(sub=str(user.id), kind="buyer", tid=tid)
    if surface == "staff":
        row = (
            await db.execute(
                select(StaffRole.key)
                .join(StaffMember, StaffMember.role_id == StaffRole.id)
                .where(StaffMember.user_id == user.id, StaffMember.status == "active")
            )
        ).first()
        if not row:
            raise Unauthorized("Invalid credentials")
        return Principal(sub=str(user.id), kind="tenant_staff", tid=tid, roles=(row[0],))
    if surface == "vendor":
        stmt = (
            select(VendorUser.vendor_id, VendorUser.role)
            .join(Vendor, Vendor.id == VendorUser.vendor_id)
            .where(
                VendorUser.user_id == user.id,
                VendorUser.status == "active",
                Vendor.status.notin_(("rejected", "closed")),
            )
        )
        if vendor_id:
            try:
                stmt = stmt.where(VendorUser.vendor_id == uuid.UUID(vendor_id))
            except ValueError as exc:
                raise Unauthorized("Invalid credentials") from exc
        rows = (await db.execute(stmt)).all()
        if len(rows) != 1:
            raise Unauthorized("Invalid credentials" if not rows else "Choose a vendor")
        return Principal(
            sub=str(user.id), kind="vendor_staff", tid=tid, vid=str(rows[0][0]), roles=(rows[0][1],)
        )
    raise Unauthorized("Invalid credentials")


# ---- tokens --------------------------------------------------------------------------------------
async def issue_tokens(
    db: AsyncSession, settings: Settings, p: Principal, family_id: uuid.UUID | None = None
) -> tuple[TokenPair, RefreshToken]:
    raw = secrets.token_urlsafe(32)
    ttl = (
        timedelta(days=settings.refresh_ttl_buyer_days)
        if p.kind == "buyer"
        else timedelta(hours=settings.refresh_ttl_staff_hours)
    )
    rt = RefreshToken(
        tenant_id=uuid.UUID(p.tid),
        user_id=uuid.UUID(p.sub),
        family_id=family_id or uuid.uuid4(),
        token_hash=_sha(raw),
        kind=p.kind,
        vendor_id=uuid.UUID(p.vid) if p.vid else None,
        expires_at=_now() + ttl,
    )
    db.add(rt)
    await db.flush()
    access = create_access_token(settings, p)
    return TokenPair(access, raw, p.kind, settings.jwt_access_ttl_seconds), rt


class SessionRevoked(Exception):
    """Refresh reuse detected. Raised only AFTER the family revocation was written, so callers
    must commit before responding (see router)."""


async def rotate_refresh(db: AsyncSession, settings: Settings, raw: str) -> TokenPair:
    rt = (
        await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == _sha(raw)).with_for_update()
        )
    ).scalar_one_or_none()
    if rt is None:
        raise Unauthorized("Invalid session")
    if rt.revoked_at is not None or rt.replaced_by is not None:
        # Reuse of a rotated token: assume theft, kill the whole family.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        await db.flush()
        raise SessionRevoked()
    if rt.expires_at <= _now():
        raise Unauthorized("Session expired")
    user = await db.get(User, rt.user_id)
    if user is None or user.status != "active":
        raise Unauthorized("Invalid session")
    surface = {"buyer": "buyer", "tenant_staff": "staff", "vendor_staff": "vendor"}[rt.kind]
    p = await principal_for(db, user, surface, str(rt.vendor_id) if rt.vendor_id else None)
    pair, new = await issue_tokens(db, settings, p, family_id=rt.family_id)
    rt.replaced_by = new.id
    rt.revoked_at = _now()
    return pair


async def revoke_family(db: AsyncSession, raw: str) -> None:
    rt = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == _sha(raw)))
    ).scalar_one_or_none()
    if rt:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )


# ---- OTP ---------------------------------------------------------------------------------------------
def _otp_hash(settings: Settings, tenant_id: str, phone: str, purpose: str, code: str) -> str:
    msg = f"{tenant_id}|{phone}|{purpose}|{code}".encode()
    return hmac.new(settings.otp_secret.encode(), msg, hashlib.sha256).hexdigest()


async def create_otp(
    db: AsyncSession, settings: Settings, tenant_id: str, phone: str, purpose: str
) -> str | None:
    """Returns the code to send, or None when a resend is too soon (caller still answers 202)."""
    last = (
        await db.execute(
            select(OtpChallenge.created_at)
            .where(OtpChallenge.phone == phone, OtpChallenge.purpose == purpose)
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last and (_now() - last).total_seconds() < settings.otp_resend_seconds:
        return None
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        OtpChallenge(
            tenant_id=uuid.UUID(tenant_id),
            phone=phone,
            purpose=purpose,
            code_hash=_otp_hash(settings, tenant_id, phone, purpose, code),
            expires_at=_now() + timedelta(seconds=settings.otp_ttl_seconds),
        )
    )
    await db.flush()
    return code


async def verify_otp(
    db: AsyncSession, settings: Settings, tenant_id: str, phone: str, purpose: str, code: str
) -> bool:
    """False on any failure. Failed attempts are flushed; the caller must commit, not raise."""
    ch = (
        await db.execute(
            select(OtpChallenge)
            .where(
                OtpChallenge.phone == phone,
                OtpChallenge.purpose == purpose,
                OtpChallenge.consumed_at.is_(None),
            )
            .order_by(OtpChallenge.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if ch is None or ch.expires_at <= _now() or ch.attempts >= settings.otp_max_attempts:
        return False
    expected = _otp_hash(settings, tenant_id, phone, purpose, code)
    if not hmac.compare_digest(expected, ch.code_hash):
        ch.attempts += 1
        await db.flush()
        return False
    ch.consumed_at = _now()
    return True


async def buyer_by_phone(db: AsyncSession, tenant_id: str, phone: str) -> User:
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if user is None:
        user = User(tenant_id=uuid.UUID(tenant_id), phone=phone, phone_verified_at=_now())
        db.add(user)
        await db.flush()
    elif user.status != "active":
        raise Unauthorized("Account unavailable")
    else:
        user.phone_verified_at = user.phone_verified_at or _now()
    user.last_login_at = _now()
    return user
