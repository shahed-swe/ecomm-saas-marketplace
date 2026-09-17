"""Tenant settings and the go-live onboarding checklist (architecture §12)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

from app.core import audit
from app.core.deps import Tenant, TenantDB, require_tenant_staff
from app.core.errors import AppError
from app.core.phone import normalize_bd_phone
from app.core.security import Principal
from app.core.tenancy import invalidate_host

router = APIRouter(prefix="/api/v1/admin", tags=["admin:settings"])
Reader = Annotated[Principal, Depends(require_tenant_staff("settings.read"))]
Writer = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]


class SettingsOut(BaseModel):
    name: str
    store_mode: str
    default_locale: str
    support_email: str | None
    support_phone: str | None
    timezone: str
    currency: str


class SettingsPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    default_locale: Literal["bn", "en"] | None = None
    support_email: EmailStr | None = None
    support_phone: str | None = Field(default=None, max_length=20)


async def _load(db, tenant_id: str) -> SettingsOut:
    row = (
        (
            await db.execute(
                text(
                    """SELECT t.name, t.store_mode, t.default_locale, s.support_email, s.support_phone, s.timezone,
                  s.currency
           FROM tenants t JOIN tenant_settings s ON s.tenant_id = t.id WHERE t.id = :t"""
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    return SettingsOut(**row)


@router.get("/settings", response_model=SettingsOut)
async def get_settings(_: Reader, tenant: Tenant, db: TenantDB):
    return await _load(db, tenant.id)


@router.patch("/settings", response_model=SettingsOut)
async def patch_settings(
    body: SettingsPatch, request: Request, actor: Writer, tenant: Tenant, db: TenantDB
):
    data = body.model_dump(exclude_unset=True)
    if "support_phone" in data and data["support_phone"]:
        phone = normalize_bd_phone(data["support_phone"])
        if not phone:
            raise AppError(
                "Enter a valid Bangladeshi mobile number", status=422, code="invalid_phone"
            )
        data["support_phone"] = phone
    t_cols = {k: data[k] for k in ("name", "default_locale") if k in data}
    s_cols = {k: data[k] for k in ("support_email", "support_phone") if k in data}
    if t_cols:
        sets = ", ".join(f"{k} = :{k}" for k in t_cols)
        await db.execute(
            text(f"UPDATE tenants SET {sets}, updated_at = now() WHERE id = :t"),  # noqa: S608
            {**t_cols, "t": tenant.id},
        )
    if s_cols:
        sets = ", ".join(f"{k} = :{k}" for k in s_cols)
        await db.execute(
            text(f"UPDATE tenant_settings SET {sets}, updated_at = now() WHERE tenant_id = :t"),  # noqa: S608
            {**s_cols, "t": tenant.id},
        )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=actor,
        action="settings.update",
        entity="tenant",
        entity_id=tenant.id,
        data=data,
        request=request,
    )
    await invalidate_host(request.app.state.redis, tenant.host)
    return await _load(db, tenant.id)


class ChecklistItem(BaseModel):
    key: str
    done: bool
    phase: int


class OnboardingOut(BaseModel):
    items: list[ChecklistItem]
    ready_to_launch: bool


@router.get("/onboarding", response_model=OnboardingOut)
async def onboarding(_: Reader, tenant: Tenant, db: TenantDB):
    q = (
        (
            await db.execute(
                text(
                    """SELECT
             (SELECT support_phone IS NOT NULL AND support_email IS NOT NULL
                FROM tenant_settings WHERE tenant_id = :t) AS contact,
             (SELECT count(*) > 1 FROM theme_versions WHERE tenant_id = :t) AS theme_published,
             (SELECT draft_document->'brand'->>'logo_url' IS NOT NULL
                FROM tenant_themes WHERE tenant_id = :t) AS logo,
             (SELECT count(*) > 0 FROM domains WHERE tenant_id = :t AND kind = 'custom'
                AND status = 'active') AS custom_domain"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    items = [
        ChecklistItem(key="store_contact", done=bool(q["contact"]), phase=4),
        ChecklistItem(key="brand_logo", done=bool(q["logo"]), phase=4),
        ChecklistItem(key="theme_published", done=bool(q["theme_published"]), phase=4),
        ChecklistItem(key="custom_domain", done=bool(q["custom_domain"]), phase=2),
        ChecklistItem(key="payment_methods", done=False, phase=11),
        ChecklistItem(key="courier_account", done=False, phase=12),
        ChecklistItem(key="first_product", done=False, phase=7),
    ]
    required = {
        "store_contact",
        "brand_logo",
        "theme_published",
        "payment_methods",
        "courier_account",
        "first_product",
    }
    return OnboardingOut(
        items=items, ready_to_launch=all(i.done for i in items if i.key in required)
    )
