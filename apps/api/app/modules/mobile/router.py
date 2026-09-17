"""The white-label apps' own API: configuration, update gate, and account deletion.

An app should be able to rebrand, re-theme and reshuffle its home screen **without a new release**,
because a store submission takes days and a campaign does not wait. So everything the app renders
comes from here: theme tokens, the same server-driven home sections the web storefront uses, the
enabled payment methods, and the copy for a maintenance window.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.deps import CurrentPrincipal, Tenant, TenantDB, require_tenant_staff
from app.core.errors import AppError, Forbidden, NotFound
from app.core.security import Principal
from app.modules.theme import storefront

app_router = APIRouter(prefix="/api/v1/app", tags=["mobile"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:mobile"])

SettingsWrite = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]
DELETION_GRACE_DAYS = 14


def _version_tuple(version: str) -> tuple[int, ...]:
    """'1.10.2' > '1.9.9'. Comparing versions as strings is how apps lock users out by mistake."""
    parts = []
    for chunk in str(version).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts + [0] * (3 - len(parts)))[:3]


def update_state(current: str | None, latest: str | None, minimum: str | None) -> str:
    if not current or not latest:
        return "ok"
    if minimum and _version_tuple(current) < _version_tuple(minimum):
        return "force"
    if _version_tuple(current) < _version_tuple(latest):
        return "optional"
    return "ok"


@app_router.get("/config")
async def app_config(
    request: Request,
    tenant: Tenant,
    db: TenantDB,
    app: Literal["buyer", "vendor"] = "buyer",
    platform: Literal["ios", "android"] | None = None,
    version: str | None = None,
):
    """Everything the app needs on launch, in one call, with the update gate answered."""
    from app.modules.theme import service as theme_service

    published = await theme_service.published_document(db, request.app.state.redis, tenant.id)
    document = published["document"]
    config = (
        (
            await db.execute(
                text("SELECT * FROM app_configs WHERE tenant_id = :t AND app = :a"),
                {"t": tenant.id, "a": app},
            )
        )
        .mappings()
        .first()
    )
    release = (
        (
            await db.execute(
                text(
                    """SELECT latest_version, min_supported_version, store_url, force_message
                       FROM app_releases WHERE tenant_id = :t AND app = :a AND platform = :p"""
                ),
                {"t": tenant.id, "a": app, "p": platform or "android"},
            )
        )
        .mappings()
        .first()
    )
    settings_row = (
        (
            await db.execute(
                text(
                    """SELECT ts.cod_enabled, ts.vat_pricing, ts.messaging_enabled,
                              ts.store_credit_enabled, t.default_locale, t.name, t.store_mode
                       FROM tenant_settings ts JOIN tenants t ON t.id = ts.tenant_id
                       WHERE ts.tenant_id = :t"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .one()
    )
    gateways = list(
        (
            await db.execute(
                text(
                    "SELECT provider FROM payment_accounts WHERE tenant_id = :t AND status <> 'disabled'"
                ),
                {"t": tenant.id},
            )
        )
        .scalars()
        .all()
    )
    state = update_state(
        version,
        (release or {}).get("latest_version"),
        (release or {}).get("min_supported_version"),
    )
    return {
        "store": {
            "name": settings_row["name"],
            "locale": settings_row["default_locale"],
            "store_mode": settings_row["store_mode"],
            "host": tenant.primary_host or tenant.host,
        },
        "branding": {
            "app_name": (config or {}).get("app_name") or settings_row["name"],
            "icon_url": (config or {}).get("icon_url"),
            "splash_url": (config or {}).get("splash_url"),
            "logo_url": (document.get("brand") or {}).get("logo_url"),
        },
        "theme": {
            "tokens": document.get("tokens") or {},
            "brand": document.get("brand") or {},
            "typography": (document.get("tokens") or {}).get("typography") or {},
        },
        "payments": {
            "cod": bool(settings_row["cod_enabled"]),
            "gateways": gateways,
            "store_credit": bool(settings_row["store_credit_enabled"]),
        },
        "features": {
            "messaging": bool(settings_row["messaging_enabled"]),
            **((config or {}).get("features") or {}),
        },
        "update": {
            "state": state,
            "latest_version": (release or {}).get("latest_version"),
            "min_supported_version": (release or {}).get("min_supported_version"),
            "store_url": (release or {}).get("store_url"),
            "message": (release or {}).get("force_message"),
        },
        "maintenance": {
            "on": bool((config or {}).get("maintenance")),
            "message": (config or {}).get("maintenance_message"),
        },
        "crash_dsn": (config or {}).get("crash_dsn"),
    }


@app_router.get("/home")
async def app_home(request: Request, response: Response, tenant: Tenant, db: TenantDB):
    """The same server-driven sections the web home renders — one source, two clients."""
    return await storefront.page(
        request=request,
        response=response,
        tenant=tenant,
        db=db,
        template="home",
        slug=None,
        preview=None,
    )


# ------------------------------------------------------------------------- account deletion
class DeletionIn(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


@app_router.post("/account/delete", status_code=202)
async def request_deletion(
    body: DeletionIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    """Required by both stores. Open orders and unspent credit hold it up — with a reason given."""
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    blocking = (
        (
            await db.execute(
                text(
                    """SELECT
                         (SELECT count(*) FROM sub_orders s
                            JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                           WHERE s.tenant_id = :t AND o.user_id = CAST(:u AS uuid)
                             AND s.status NOT IN ('delivered','cancelled','returned')) AS open_orders,
                         (SELECT coalesce(balance, 0) FROM store_credit_accounts
                           WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)) AS credit"""
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .one()
    )
    if blocking["open_orders"]:
        raise AppError(
            "You have orders on the way. We can delete your account once they are complete.",
            status=409,
            code="open_orders",
        )
    scheduled = datetime.now(UTC) + timedelta(days=DELETION_GRACE_DAYS)
    row = (
        await db.execute(
            text(
                """INSERT INTO account_deletion_requests (tenant_id, user_id, reason, scheduled_for)
                   VALUES (:t, :u, :r, :s)
                   ON CONFLICT (tenant_id, user_id) WHERE status = 'pending' DO NOTHING
                   RETURNING id, scheduled_for"""
            ),
            {"t": tenant.id, "u": p.sub, "r": body.reason, "s": scheduled},
        )
    ).first()
    if row is None:
        existing = (
            await db.execute(
                text(
                    "SELECT scheduled_for FROM account_deletion_requests WHERE tenant_id = :t "
                    "AND user_id = CAST(:u AS uuid) AND status = 'pending'"
                ),
                {"t": tenant.id, "u": p.sub},
            )
        ).scalar()
        return {"status": "pending", "scheduled_for": existing, "store_credit": blocking["credit"]}
    return {
        "status": "pending",
        "scheduled_for": row.scheduled_for,
        "store_credit": blocking["credit"],
        "grace_days": DELETION_GRACE_DAYS,
    }


@app_router.post("/account/delete/cancel")
async def cancel_deletion(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    res = await db.execute(
        text(
            "UPDATE account_deletion_requests SET status = 'cancelled' WHERE tenant_id = :t "
            "AND user_id = CAST(:u AS uuid) AND status = 'pending'"
        ),
        {"t": tenant.id, "u": p.sub},
    )
    if res.rowcount == 0:
        raise NotFound("No deletion request")
    return {"status": "cancelled"}


@app_router.get("/account/delete")
async def deletion_status(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(
                    """SELECT status, scheduled_for, created_at FROM account_deletion_requests
                       WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)
                       ORDER BY created_at DESC LIMIT 1"""
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {"status": "none"}


# ------------------------------------------------------------------------------- staff admin
class ReleaseIn(BaseModel):
    app: Literal["buyer", "vendor"] = "buyer"
    platform: Literal["ios", "android"]
    latest_version: str = Field(pattern=r"^\d+(\.\d+){0,3}$")
    min_supported_version: str = Field(pattern=r"^\d+(\.\d+){0,3}$")
    store_url: str | None = Field(default=None, max_length=300)
    force_message: str | None = Field(default=None, max_length=300)


class AppConfigIn(BaseModel):
    app: Literal["buyer", "vendor"] = "buyer"
    app_name: str | None = Field(default=None, max_length=60)
    bundle_id: str | None = Field(default=None, max_length=120)
    icon_url: str | None = None
    splash_url: str | None = None
    crash_dsn: str | None = Field(default=None, max_length=300)
    maintenance: bool = False
    maintenance_message: str | None = Field(default=None, max_length=300)
    features: dict = Field(default_factory=dict)


@admin.put("/app-releases")
async def put_release(
    body: ReleaseIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    if _version_tuple(body.min_supported_version) > _version_tuple(body.latest_version):
        raise AppError(
            "The minimum supported version cannot be newer than the latest one",
            status=422,
            code="bad_versions",
        )
    await db.execute(
        text(
            """INSERT INTO app_releases (tenant_id, app, platform, latest_version,
                   min_supported_version, store_url, force_message, updated_by)
               VALUES (:t, :a, :p, :lv, :mv, :url, :msg, :by)
               ON CONFLICT (tenant_id, app, platform) DO UPDATE
               SET latest_version = EXCLUDED.latest_version,
                   min_supported_version = EXCLUDED.min_supported_version,
                   store_url = EXCLUDED.store_url, force_message = EXCLUDED.force_message,
                   released_at = now(), updated_by = EXCLUDED.updated_by"""
        ),
        {
            "t": tenant.id,
            "a": body.app,
            "p": body.platform,
            "lv": body.latest_version,
            "mv": body.min_supported_version,
            "url": body.store_url,
            "msg": body.force_message,
            "by": p.sub,
        },
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="app_release.set",
        entity="app_release",
        entity_id=f"{body.app}:{body.platform}",
        data=body.model_dump(),
        request=request,
    )
    return body.model_dump()


@admin.put("/app-config")
async def put_app_config(
    body: AppConfigIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    await db.execute(
        text(
            """INSERT INTO app_configs (tenant_id, app, bundle_id, app_name, icon_url, splash_url,
                   crash_dsn, maintenance, maintenance_message, features, updated_by)
               VALUES (:t, :a, :b, :n, :i, :s, :d, :m, :mm, CAST(:f AS jsonb), :by)
               ON CONFLICT (tenant_id, app) DO UPDATE
               SET bundle_id = EXCLUDED.bundle_id, app_name = EXCLUDED.app_name,
                   icon_url = EXCLUDED.icon_url, splash_url = EXCLUDED.splash_url,
                   crash_dsn = EXCLUDED.crash_dsn, maintenance = EXCLUDED.maintenance,
                   maintenance_message = EXCLUDED.maintenance_message, features = EXCLUDED.features,
                   updated_by = EXCLUDED.updated_by, updated_at = now()"""
        ),
        {
            "t": tenant.id,
            "a": body.app,
            "b": body.bundle_id,
            "n": body.app_name,
            "i": body.icon_url,
            "s": body.splash_url,
            "d": body.crash_dsn,
            "m": body.maintenance,
            "mm": body.maintenance_message,
            "f": __import__("json").dumps(body.features),
            "by": p.sub,
        },
    )
    return body.model_dump()


@admin.get("/app-config")
async def get_app_config(p: SettingsWrite, tenant: Tenant, db: TenantDB):
    configs = (
        (await db.execute(text("SELECT * FROM app_configs WHERE tenant_id = :t"), {"t": tenant.id}))
        .mappings()
        .all()
    )
    releases = (
        (
            await db.execute(
                text("SELECT * FROM app_releases WHERE tenant_id = :t ORDER BY app, platform"),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return {
        "configs": [{k: v for k, v in c.items() if k != "tenant_id"} for c in configs],
        "releases": [{k: v for k, v in r.items() if k != "tenant_id"} for r in releases],
    }


@admin.get("/account-deletions")
async def deletion_queue(p: SettingsWrite, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT d.id, d.status, d.reason, d.scheduled_for, d.created_at, u.email
                       FROM account_deletion_requests d
                       JOIN users u ON u.id = d.user_id AND u.tenant_id = d.tenant_id
                       WHERE d.tenant_id = :t ORDER BY d.created_at DESC LIMIT 100"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.post("/account-deletions/{request_id}/complete", status_code=204)
async def complete_deletion(
    request_id: uuid.UUID, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    from app.modules.mobile import service as mobile

    done = await mobile.anonymise_user(db, tenant.id, request_id=request_id, actor_id=p.sub)
    if not done:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="account.delete",
        entity="deletion_request",
        entity_id=request_id,
        request=request,
    )
    return Response(status_code=204)
