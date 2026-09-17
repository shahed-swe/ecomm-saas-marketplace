from typing import Literal

import pyotp
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.core import audit
from app.core.cache_keys import tkey
from app.core.deps import CurrentPrincipal, PlatformDB, Tenant, TenantDB
from app.core.errors import AppError, Unauthorized, problem_response
from app.core.passwords import MIN_LENGTH, verify_password
from app.core.phone import normalize_bd_phone
from app.core.ratelimit import hit
from app.core.security import Principal, create_access_token
from app.modules.identity import service
from app.modules.identity.models import PlatformUser, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
me_router = APIRouter(prefix="/api/v1", tags=["auth"])
platform_auth = APIRouter(prefix="/platform/v1/auth", tags=["platform"])

COOKIE = "rt"
Surface = Literal["buyer", "staff", "vendor"]


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_LENGTH, max_length=200)
    full_name: str | None = Field(default=None, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    surface: Surface = "buyer"
    vendor_id: str | None = None


class OtpRequestIn(BaseModel):
    phone: str = Field(max_length=20)
    purpose: Literal["login", "guest_checkout"] = "login"


class OtpVerifyIn(BaseModel):
    phone: str = Field(max_length=20)
    code: str = Field(pattern=r"^\d{6}$")
    purpose: Literal["login", "guest_checkout"] = "login"


class RefreshIn(BaseModel):
    refresh_token: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    kind: str
    refresh_token: str | None = None  # only for app clients (X-Client: app)


class OtpAccepted(BaseModel):
    status: str = "sent"


class MeOut(BaseModel):
    id: str
    kind: str
    email: str | None
    phone: str | None
    full_name: str | None
    roles: list[str]
    vendor_id: str | None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _respond(request: Request, response: Response, pair: service.TokenPair) -> TokenOut:
    is_app = request.headers.get("x-client") == "app"
    if not is_app:
        response.set_cookie(
            COOKIE,
            pair.refresh_token,
            httponly=True,
            samesite="lax",
            secure=request.app.state.settings.cookie_secure,
            path="/api/v1/auth",
            max_age=60 * 60 * 24 * 30,
        )
    return TokenOut(
        access_token=pair.access_token,
        expires_in=pair.expires_in,
        kind=pair.kind,
        refresh_token=pair.refresh_token if is_app else None,
    )


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(
    body: RegisterIn, request: Request, response: Response, tenant: Tenant, db: TenantDB
):
    redis = request.app.state.redis
    await hit(redis, tkey(tenant.id, "rl", "register", _client_ip(request)), 10, 3600)
    user = await service.register_buyer(
        db, tenant.id, email=body.email, password=body.password, full_name=body.full_name
    )
    p = Principal(sub=str(user.id), kind="buyer", tid=tenant.id)
    pair, _ = await service.issue_tokens(db, request.app.state.settings, p)
    return _respond(request, response, pair)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, response: Response, tenant: Tenant, db: TenantDB):
    redis = request.app.state.redis
    await hit(redis, tkey(tenant.id, "rl", "login", "ip", _client_ip(request)), 50, 900)
    await hit(
        redis,
        tkey(tenant.id, "rl", "login", "acct", service._sha(body.email.lower())[:32]),
        10,
        900,
    )
    user = await service.authenticate_password(db, email=body.email, password=body.password)
    p = await service.principal_for(db, user, body.surface, body.vendor_id)
    pair, _ = await service.issue_tokens(db, request.app.state.settings, p)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="auth.login",
        entity="user",
        entity_id=user.id,
        data={"surface": body.surface},
        request=request,
    )
    return _respond(request, response, pair)


@router.post("/otp/request", response_model=OtpAccepted, status_code=202)
async def otp_request(body: OtpRequestIn, request: Request, tenant: Tenant, db: TenantDB):
    phone = normalize_bd_phone(body.phone)
    if phone is None:
        raise AppError("Enter a valid Bangladeshi mobile number", status=422, code="invalid_phone")
    redis = request.app.state.redis
    await hit(redis, tkey(tenant.id, "rl", "otp", "phone", phone[1:]), 5, 3600)
    await hit(redis, tkey(tenant.id, "rl", "otp", "ip", _client_ip(request)), 20, 3600)
    code = await service.create_otp(db, request.app.state.settings, tenant.id, phone, body.purpose)
    if code:
        await request.app.state.sms.send(
            tenant_id=tenant.id,
            phone=phone,
            message=f"{code} is your verification code. It expires in 5 minutes. Do not share it.",
        )
    return OtpAccepted()


@router.post("/otp/verify", response_model=TokenOut)
async def otp_verify(
    body: OtpVerifyIn, request: Request, response: Response, tenant: Tenant, db: TenantDB
):
    phone = normalize_bd_phone(body.phone)
    settings = request.app.state.settings
    if phone is None or not await service.verify_otp(
        db, settings, tenant.id, phone, body.purpose, body.code
    ):
        return problem_response(request, service.InvalidOtp("Code is invalid or expired"))
    user = await service.buyer_by_phone(db, tenant.id, phone)
    p = Principal(sub=str(user.id), kind="buyer", tid=tenant.id)
    pair, _ = await service.issue_tokens(db, settings, p)
    return _respond(request, response, pair)


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    request: Request,
    response: Response,
    tenant: Tenant,
    db: TenantDB,
    body: RefreshIn | None = None,
):
    raw = (body.refresh_token if body else None) or request.cookies.get(COOKIE)
    if not raw:
        raise Unauthorized("Invalid session")
    try:
        pair = await service.rotate_refresh(db, request.app.state.settings, raw)
    except service.SessionRevoked:
        response.delete_cookie(COOKIE, path="/api/v1/auth")
        return problem_response(request, Unauthorized("Session revoked"))
    return _respond(request, response, pair)


@router.post("/logout", status_code=204)
async def logout(request: Request, tenant: Tenant, db: TenantDB, body: RefreshIn | None = None):
    raw = (body.refresh_token if body else None) or request.cookies.get(COOKIE)
    if raw:
        await service.revoke_family(db, raw)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/api/v1/auth")
    return resp


@me_router.get("/me", response_model=MeOut)
async def me(p: CurrentPrincipal, db: TenantDB):
    user = await db.get(User, p.sub)
    if user is None:
        raise Unauthorized("Invalid token")
    return MeOut(
        id=str(user.id),
        kind=p.kind,
        email=user.email,
        phone=user.phone,
        full_name=user.full_name,
        roles=list(p.roles),
        vendor_id=p.vid,
    )


# ---- platform super-admin -----------------------------------------------------------------------------
class PlatformLoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    totp: str = Field(pattern=r"^\d{6}$")


@platform_auth.post("/login", response_model=TokenOut)
async def platform_login(body: PlatformLoginIn, request: Request, db: PlatformDB):
    await hit(request.app.state.redis, f"platform:rl:login:{_client_ip(request)}", 10, 900)
    u = (
        await db.execute(select(PlatformUser).where(PlatformUser.email == body.email.lower()))
    ).scalar_one_or_none()
    ok = verify_password(u.password_hash if u else None, body.password)
    if (
        not u
        or not ok
        or u.status != "active"
        or not pyotp.TOTP(u.totp_secret).verify(body.totp, valid_window=1)
    ):
        raise Unauthorized("Invalid credentials")
    settings = request.app.state.settings
    token = create_access_token(
        settings, Principal(sub=str(u.id), kind="platform", roles=("super",)), ttl=3600
    )
    return TokenOut(access_token=token, expires_in=3600, kind="platform")


__all__ = ["router", "me_router", "platform_auth", "Depends"]
