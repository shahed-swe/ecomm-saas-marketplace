"""Payment surfaces: buyer (start/confirm/status), provider webhooks, staff credential admin.

The webhook route is the only one that cannot use `tenant_db`: a provider posts to the platform
host with no tenant Host header, so the tenant comes from its public id in the path and the
session is scoped by hand. It is still an ordinary app-role session — RLS applies.
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app.core import audit
from app.core.cache_keys import tkey
from app.core.crypto import last4
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import AppError, Forbidden, NotFound, problem_response
from app.core.ratelimit import hit
from app.core.security import Principal
from app.core.tenancy import scope_session
from app.modules.payments import service
from app.modules.payments.gateways import PROVIDERS, GatewayError, build_gateway

buyer = APIRouter(prefix="/api/v1", tags=["payments"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:payments"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:payments"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

SettingsWrite = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]
OrdersRead = Annotated[Principal, Depends(require_tenant_staff("orders.read"))]
VendorFinance = Annotated[Principal, Depends(require_vendor_role("finance.read"))]

CREDENTIAL_FIELDS = {
    "bkash": ("app_key", "app_secret", "username", "password"),
    "sslcommerz": ("store_id", "store_passwd"),
}
HINT_FIELD = {"bkash": "app_key", "sslcommerz": "store_id"}


def _overrides(app) -> dict:
    return getattr(app.state, "payment_gateways", {})


def _buyer(p: Principal) -> Principal:
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    return p


async def _urls(request: Request, db, tenant: Tenant, provider: str, order_number: str):
    """Buyer returns to the tenant's own host; the provider calls back on the platform host.

    The callback URL carries the tenant's opaque public id — never its uuid, never its host, so a
    custom domain moving does not break in-flight payments."""
    public_id = (
        await db.execute(text("SELECT public_id FROM tenants WHERE id = :t"), {"t": tenant.id})
    ).scalar()
    base = f"https://{tenant.primary_host or tenant.host}"
    platform = request.app.state.settings.public_api_url or base
    return (
        f"{base}/orders/{order_number}/payment",
        f"{platform}/webhooks/{provider}/{public_id}",
    )


# --------------------------------------------------------------------------------------- buyer
class StartIn(BaseModel):
    order_number: str = Field(min_length=3, max_length=40)


@buyer.post("/payments/start")
async def start(body: StartIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "pay", p.sub), 20, 600)
    order = (
        await db.execute(
            text("SELECT payment_method FROM orders WHERE tenant_id = :t AND number = :n"),
            {"t": tenant.id, "n": body.order_number},
        )
    ).first()
    if order is None:
        raise NotFound("Order not found")
    provider = order.payment_method
    if provider not in PROVIDERS:
        raise service.PaymentError("This order is cash on delivery", code="not_prepaid")
    account = await service.load_account(db, request.app.state.settings, tenant.id, provider)
    gateway = build_gateway(provider, account.mode, _overrides(request.app))
    return_url, callback_url = await _urls(request, db, tenant, provider, body.order_number)
    try:
        return await service.start_payment(
            db,
            tenant.id,
            order_number=body.order_number,
            user_id=p.sub,
            gateway=gateway,
            account=account,
            return_url=return_url,
            callback_url=callback_url,
        )
    except GatewayError as exc:
        raise AppError(
            "The payment provider is not responding. Please try again.",
            status=502,
            code="gateway_error",
        ) from exc


@buyer.post("/payments/{payment_id}/confirm")
async def confirm(
    payment_id: uuid.UUID, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    """Called by the return page. The provider's answer decides, not the query string it carried."""
    _buyer(p)
    row = (
        (
            await db.execute(
                text(
                    """SELECT p.provider, p.status, o.user_id FROM payments p
                       JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                       WHERE p.tenant_id = :t AND p.id = :p"""
                ),
                {"t": tenant.id, "p": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or str(row["user_id"]) != str(p.sub):
        raise NotFound("Payment not found")
    account = await service.load_account(db, request.app.state.settings, tenant.id, row["provider"])
    gateway = build_gateway(row["provider"], account.mode, _overrides(request.app))
    try:
        return await service.settle(
            db,
            tenant.id,
            payment_id=payment_id,
            gateway=gateway,
            account=account,
            app_state=request.app.state,
        )
    except GatewayError as exc:
        raise AppError(
            "We could not reach the payment provider. Your money is safe — refresh in a moment.",
            status=502,
            code="gateway_error",
        ) from exc


@buyer.get("/payments/{payment_id}")
async def payment_status(payment_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    row = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.provider, p.status, p.amount, p.verified_at, o.number AS order_number,
                              o.user_id
                       FROM payments p JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                       WHERE p.tenant_id = :t AND p.id = :p"""
                ),
                {"t": tenant.id, "p": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or str(row["user_id"]) != str(p.sub):
        raise NotFound("Payment not found")
    return {k: v for k, v in row.items() if k != "user_id"}


@buyer.get("/me/orders/{number}/payment")
async def latest_payment(number: str, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    """The return page comes back with nothing but the order number; this is how it finds its attempt."""
    _buyer(p)
    row = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.provider, p.status, p.amount, p.verified_at, o.number AS order_number
                       FROM orders o LEFT JOIN payments p ON p.order_id = o.id AND p.tenant_id = o.tenant_id
                       WHERE o.tenant_id = :t AND o.number = :n AND o.user_id = CAST(:u AS uuid)
                       ORDER BY p.created_at DESC NULLS LAST LIMIT 1"""
                ),
                {"t": tenant.id, "n": number, "u": p.sub},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Order not found")
    return {k: v for k, v in row.items()} if row["id"] else {"id": None, "status": "none"}


# ------------------------------------------------------------------------------------ webhooks
@webhooks.post("/{provider}/{tenant_public_id}")
async def provider_webhook(
    provider: str, tenant_public_id: str, request: Request, response: Response
):
    """Always 200 once the event is stored: a provider must not retry an event we already have."""
    if provider not in PROVIDERS:
        raise NotFound("Not found")
    async with request.app.state.db.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM resolve_tenant_by_public_id(:p)"), {"p": tenant_public_id}
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        raise NotFound("Not found")
    tenant_id = str(row["tenant_id"])
    body = await request.body()
    try:
        form = dict(await request.form())
    except Exception:  # pragma: no cover - malformed multipart
        form = {}
    if not form and body:
        import json as _json

        try:
            form = _json.loads(body)
        except ValueError:
            form = {}
    async with request.app.state.db.sessionmaker() as session:
        async with session.begin():
            await scope_session(session, tenant_id)
            try:
                account = await service.load_account(
                    session, request.app.state.settings, tenant_id, provider
                )
            except AppError as exc:
                return problem_response(request, exc)
            gateway = build_gateway(provider, account.mode, _overrides(request.app))
            try:
                event = gateway.parse_callback(
                    credentials=account.credentials,
                    body=body,
                    form=form,
                    headers=dict(request.headers),
                )
            except GatewayError as exc:
                return problem_response(
                    request, AppError(str(exc), status=400, code="bad_callback")
                )
            payment_id = await service.payment_for_callback(
                session,
                tenant_id,
                provider=provider,
                provider_ref=event.provider_ref,
                order_number=event.payload.get("tran_id")
                or event.payload.get("merchantInvoiceNumber"),
            )
            fresh = await service.record_event(
                session,
                tenant_id,
                provider=provider,
                event_id=event.event_id,
                kind=event.kind,
                payload=event.payload,
                payment_id=payment_id,
            )
            if not fresh:
                return {"status": "duplicate"}
            if payment_id is None:
                # Not ours (another tenant's account, or an attempt we never created); the event is
                # still stored, so reconciliation can show it as an unmatched callback.
                return {"status": "ignored"}
            try:
                result = await service.settle(
                    session,
                    tenant_id,
                    payment_id=payment_id,
                    gateway=gateway,
                    account=account,
                    app_state=request.app.state,
                )
            except GatewayError:
                return {"status": "retry_later"}
            return {"status": result["status"]}


# --------------------------------------------------------------------------------- staff admin
class AccountIn(BaseModel):
    provider: Literal["bkash", "sslcommerz"]
    mode: Literal["sandbox", "live"] = "sandbox"
    credentials: dict[str, str]

    @field_validator("credentials")
    @classmethod
    def _trim(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: s.strip() for k, s in v.items() if s and s.strip()}


class ProviderIn(BaseModel):
    provider: Literal["bkash", "sslcommerz"]


def _view(row, credentials: dict | None) -> dict:
    hint = (credentials or {}).get(HINT_FIELD[row["provider"]], "")
    return {
        "provider": row["provider"],
        "mode": row["mode"],
        "status": row["status"],
        "last_checked_at": row["last_checked_at"],
        "last_error": row["last_error"],
        "updated_at": row["updated_at"],
        "key_hint": f"••••{last4(hint)}" if hint else None,
    }


@admin.get("/payment-accounts")
async def list_accounts(request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text("SELECT * FROM payment_accounts WHERE tenant_id = :t ORDER BY provider"),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    out = []
    for row in rows:
        try:
            account = await service.load_account(
                db, request.app.state.settings, tenant.id, row["provider"]
            )
            out.append(_view(row, account.credentials))
        except (AppError, ValueError):
            out.append(_view(row, None))
    return out


@admin.put("/payment-accounts")
async def put_account(
    body: AccountIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    missing = [f for f in CREDENTIAL_FIELDS[body.provider] if f not in body.credentials]
    if missing:
        raise AppError(
            f"Missing credential fields: {', '.join(missing)}",
            status=422,
            code="credentials_incomplete",
        )
    settings = request.app.state.settings
    credentials = {k: body.credentials[k] for k in CREDENTIAL_FIELDS[body.provider]}
    ciphertext = service.encrypt_credentials(settings, tenant.id, body.provider, credentials)
    await db.execute(
        text(
            """INSERT INTO payment_accounts (tenant_id, provider, mode, credentials_ciphertext, status, updated_by)
               VALUES (:t, :p, :m, :c, 'unverified', :a)
               ON CONFLICT (tenant_id, provider) DO UPDATE
               SET mode = EXCLUDED.mode, credentials_ciphertext = EXCLUDED.credentials_ciphertext,
                   status = 'unverified', last_error = NULL, updated_by = EXCLUDED.updated_by,
                   updated_at = now()"""
        ),
        {"t": tenant.id, "p": body.provider, "m": body.mode, "c": ciphertext, "a": p.sub},
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payment_account.set",
        entity="payment_account",
        entity_id=body.provider,
        data={"provider": body.provider, "mode": body.mode},  # credentials never land in the log
        request=request,
    )
    return await _health(request, db, tenant, body.provider, actor=p)


@admin.post("/payment-accounts/health")
async def check_health(
    body: ProviderIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    return await _health(request, db, tenant, body.provider, actor=p)


@admin.post("/payment-accounts/disable")
async def disable_account(
    body: ProviderIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    res = await db.execute(
        text(
            "UPDATE payment_accounts SET status = 'disabled', updated_at = now(), updated_by = :a "
            "WHERE tenant_id = :t AND provider = :p"
        ),
        {"a": p.sub, "t": tenant.id, "p": body.provider},
    )
    if res.rowcount == 0:
        raise NotFound("Not configured")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="payment_account.disable",
        entity="payment_account",
        entity_id=body.provider,
        request=request,
    )
    return Response(status_code=204)


async def _health(request: Request, db, tenant: Tenant, provider: str, *, actor) -> dict:
    account = await service.load_account(db, request.app.state.settings, tenant.id, provider)
    gateway = build_gateway(provider, account.mode, _overrides(request.app))
    status, error = "healthy", None
    try:
        await gateway.health(credentials=account.credentials)
    except Exception as exc:  # gateway or transport: both mean "do not take money with this"
        status, error = "failing", str(exc)[:500]
    row = (
        (
            await db.execute(
                text(
                    """UPDATE payment_accounts SET status = :s, last_error = :e, last_checked_at = now()
                       WHERE tenant_id = :t AND provider = :p RETURNING *"""
                ),
                {"s": status, "e": error, "t": tenant.id, "p": provider},
            )
        )
        .mappings()
        .one()
    )
    return _view(row, account.credentials)


@admin.get("/payments")
async def list_payments(
    p: OrdersRead,
    tenant: Tenant,
    db: TenantDB,
    order_number: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.provider, p.status, p.amount, p.fee, p.provider_ref, p.payer_ref,
                              p.verified_at, p.failure_reason, p.created_at, o.number AS order_number
                       FROM payments p JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                       WHERE p.tenant_id = :t AND (CAST(:n AS text) IS NULL OR o.number = :n)
                         AND (CAST(:s AS text) IS NULL OR p.status = :s)
                       ORDER BY p.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "n": order_number, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------- cash to come
COD_SQL = """SELECT r.id, r.status, r.amount, r.courier, r.collected_at, r.settled_at, r.settlement_ref,
       o.number AS order_number, s.number AS shipment_number, r.created_at
FROM cod_receivables r
JOIN orders o ON o.id = r.order_id AND o.tenant_id = r.tenant_id
JOIN sub_orders s ON s.id = r.sub_order_id AND s.tenant_id = r.tenant_id
WHERE r.tenant_id = :t AND (CAST(:s AS text) IS NULL OR r.status = :s)"""


@admin.get("/payments/reconciliation")
async def reconciliation(p: OrdersRead, tenant: Tenant, db: TenantDB, days: int = 7):
    return await service.reconciliation_summary(db, tenant.id, days=min(max(days, 1), 90))


@vendor.get("/cod-receivables")
async def vendor_cod(
    p: VendorFinance, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(COD_SQL + " ORDER BY r.created_at DESC LIMIT :l"),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/cod-receivables")
async def admin_cod(
    p: OrdersRead,
    tenant: Tenant,
    db: TenantDB,
    status: str | None = None,
    vendor_id: uuid.UUID | None = None,
    limit: int = 50,
):
    rows = (
        (
            await db.execute(
                text(
                    COD_SQL
                    + " AND (CAST(:v AS uuid) IS NULL OR r.vendor_id = :v) ORDER BY r.created_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "s": status, "v": vendor_id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
