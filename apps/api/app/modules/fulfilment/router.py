"""Fulfilment surfaces: vendor packing and booking, staff couriers/rules/ops/settlements,
courier webhooks, buyer tracking.

Courier rules are edited as one ordered list (PUT replaces it) — an ordered policy is much easier
to reason about, and to audit, as a whole than as a pile of individually patched rows.
"""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.crypto import last4
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import AppError, Forbidden, NotFound, problem_response
from app.core.security import Principal
from app.core.tenancy import scope_session
from app.modules.fulfilment import service
from app.modules.fulfilment.couriers import COURIERS, CourierError, build_courier

vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:fulfilment"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:fulfilment"])
buyer = APIRouter(prefix="/api/v1", tags=["fulfilment"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

VendorSeller = Annotated[Principal, Depends(require_vendor_role("orders.write", approved=True))]
SettingsWrite = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]
OrdersRead = Annotated[Principal, Depends(require_tenant_staff("orders.read"))]
OrdersWrite = Annotated[Principal, Depends(require_tenant_staff("orders.write"))]

CREDENTIAL_FIELDS = {
    "pathao": ("client_id", "client_secret", "username", "password", "store_id"),
    "steadfast": ("api_key", "secret_key"),
    "redx": ("access_token", "pickup_store_id"),
}
HINT_FIELD = {"pathao": "client_id", "steadfast": "api_key", "redx": "access_token"}
SHIPMENT_SQL = """SELECT s.id, s.courier, s.consignment_id, s.tracking_code, s.tracking_url, s.status,
       s.status_detail, s.cod_amount, s.delivery_fee, s.failed_attempts, s.needs_attention,
       s.attention_reason, s.booked_at, s.picked_up_at, s.delivered_at, s.returned_at, s.last_event_at,
       so.number AS shipment_number, o.number AS order_number, s.vendor_id
FROM shipments s
JOIN sub_orders so ON so.id = s.sub_order_id AND so.tenant_id = s.tenant_id
JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
WHERE s.tenant_id = :t"""


def _overrides(app) -> dict:
    return getattr(app.state, "couriers", {})


# -------------------------------------------------------------------------------------- vendor
class BookIn(BaseModel):
    courier: Literal["pathao", "steadfast", "redx"] | None = None
    note: str | None = Field(default=None, max_length=250)


@vendor.post("/orders/{sub_order_id}/ready")
async def ready(
    sub_order_id: uuid.UUID, request: Request, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    status = await service.mark_ready(db, tenant.id, sub_order_id, p.vid)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="shipment.ready",
        entity="sub_order",
        entity_id=sub_order_id,
        request=request,
    )
    return {"status": status}


@vendor.post("/orders/{sub_order_id}/ship")
async def ship(
    sub_order_id: uuid.UUID,
    body: BookIn,
    request: Request,
    p: VendorSeller,
    tenant: Tenant,
    db: TenantDB,
):
    try:
        shipment = await service.book_shipment(
            db,
            request.app.state.settings,
            tenant.id,
            sub_order_id=sub_order_id,
            vendor_id=p.vid,
            courier=body.courier,
            note=body.note,
            overrides=_overrides(request.app),
        )
    except CourierError as exc:
        raise AppError(
            "The courier could not accept this parcel. Try again or pick another courier.",
            status=502,
            code="courier_error",
        ) from exc
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="shipment.book",
        entity="shipment",
        entity_id=shipment["id"],
        data={"courier": shipment["courier"], "consignment_id": shipment["consignment_id"]},
        request=request,
    )
    return shipment


@vendor.get("/shipments")
async def vendor_shipments(
    p: VendorSeller, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    SHIPMENT_SQL
                    + " AND (CAST(:s AS text) IS NULL OR s.status = :s) ORDER BY s.booked_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/shipments/{shipment_id}")
async def vendor_shipment(shipment_id: uuid.UUID, p: VendorSeller, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(SHIPMENT_SQL + " AND s.id = :i"), {"t": tenant.id, "i": shipment_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    events = (
        (
            await db.execute(
                text(
                    """SELECT raw_status, status, occurred_at, received_at FROM shipment_events
                       WHERE tenant_id = :t AND shipment_id = :i ORDER BY received_at"""
                ),
                {"t": tenant.id, "i": shipment_id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(row), "events": [dict(e) for e in events]}


@vendor.post("/shipments/{shipment_id}/cancel")
async def vendor_cancel(
    shipment_id: uuid.UUID, request: Request, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    out = await service.cancel_shipment(
        db,
        request.app.state.settings,
        tenant.id,
        shipment_id,
        vendor_id=p.vid,
        overrides=_overrides(request.app),
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="shipment.cancel",
        entity="shipment",
        entity_id=shipment_id,
        request=request,
    )
    return out


# --------------------------------------------------------------------------------------- buyer
@buyer.get("/me/orders/{number}/tracking")
async def tracking(number: str, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    rows = (
        (
            await db.execute(
                text(
                    """SELECT s.status, s.courier, s.tracking_code, s.tracking_url, s.booked_at,
                              s.delivered_at, so.number AS shipment_number, v.display_name AS vendor_name
                       FROM shipments s
                       JOIN sub_orders so ON so.id = s.sub_order_id AND so.tenant_id = s.tenant_id
                       JOIN vendors v ON v.id = s.vendor_id AND v.tenant_id = s.tenant_id
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND o.number = :n AND o.user_id = CAST(:u AS uuid)
                       ORDER BY so.number"""
                ),
                {"t": tenant.id, "n": number, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------ webhooks
@webhooks.post("/courier/{courier}/{tenant_public_id}")
async def courier_webhook(courier: str, tenant_public_id: str, request: Request):
    if courier not in COURIERS:
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
            parsed = _json.loads(body)
            form = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            form = {}
    async with request.app.state.db.sessionmaker() as session:
        async with session.begin():
            await scope_session(session, tenant_id)
            try:
                account = await service.load_account(
                    session, request.app.state.settings, tenant_id, courier
                )
            except AppError as exc:
                return problem_response(request, exc)
            adapter = build_courier(courier, account.mode, _overrides(request.app))
            try:
                event = adapter.parse_webhook(
                    credentials=account.credentials,
                    body=body,
                    form=form,
                    headers=dict(request.headers),
                )
            except CourierError as exc:
                return problem_response(
                    request, AppError(str(exc), status=400, code="bad_callback")
                )
            return await service.apply_event(
                session, tenant_id, courier=courier, event=event, app_state=request.app.state
            )


# --------------------------------------------------------------------------------- staff admin
class CourierAccountIn(BaseModel):
    courier: Literal["pathao", "steadfast", "redx"]
    mode: Literal["sandbox", "live"] = "sandbox"
    credentials: dict[str, str]
    pickup_ref: str | None = Field(default=None, max_length=80)


class CourierIn(BaseModel):
    courier: Literal["pathao", "steadfast", "redx"]


class RuleIn(BaseModel):
    courier: Literal["pathao", "steadfast", "redx"]
    districts: list[str] = Field(default_factory=list, max_length=64)
    zones: list[Literal["inside_dhaka", "dhaka_suburb", "outside_dhaka"]] = Field(
        default_factory=list
    )
    max_weight_grams: int | None = Field(default=None, gt=0)
    max_cod_amount: float | None = Field(default=None, ge=0)
    enabled: bool = True


class RulesIn(BaseModel):
    rules: list[RuleIn] = Field(max_length=50)


def _account_view(row, credentials: dict | None) -> dict:
    hint = (credentials or {}).get(HINT_FIELD[row["courier"]], "")
    return {
        "courier": row["courier"],
        "mode": row["mode"],
        "status": row["status"],
        "pickup_ref": row["pickup_ref"],
        "last_checked_at": row["last_checked_at"],
        "last_error": row["last_error"],
        "key_hint": f"••••{last4(hint)}" if hint else None,
    }


@admin.get("/courier-accounts")
async def list_courier_accounts(request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM courier_accounts WHERE tenant_id = :t AND vendor_id IS NULL "
                    "ORDER BY courier"
                ),
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
                db, request.app.state.settings, tenant.id, row["courier"]
            )
            out.append(_account_view(row, account.credentials))
        except (AppError, ValueError):
            out.append(_account_view(row, None))
    return out


@admin.put("/courier-accounts")
async def put_courier_account(
    body: CourierAccountIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    missing = [f for f in CREDENTIAL_FIELDS[body.courier] if not body.credentials.get(f)]
    if missing:
        raise AppError(
            f"Missing credential fields: {', '.join(missing)}",
            status=422,
            code="credentials_incomplete",
        )
    credentials = {k: body.credentials[k] for k in CREDENTIAL_FIELDS[body.courier]}
    ciphertext = service.encrypt_credentials(
        request.app.state.settings, tenant.id, body.courier, None, credentials
    )
    await db.execute(
        text(
            """INSERT INTO courier_accounts (tenant_id, courier, mode, credentials_ciphertext, pickup_ref,
                   status, updated_by)
               VALUES (:t, :c, :m, :ct, :pr, 'unverified', :a)
               ON CONFLICT (tenant_id, courier) WHERE vendor_id IS NULL DO UPDATE
               SET mode = EXCLUDED.mode, credentials_ciphertext = EXCLUDED.credentials_ciphertext,
                   pickup_ref = EXCLUDED.pickup_ref, status = 'unverified', last_error = NULL,
                   updated_by = EXCLUDED.updated_by, updated_at = now()"""
        ),
        {
            "t": tenant.id,
            "c": body.courier,
            "m": body.mode,
            "ct": ciphertext,
            "pr": body.pickup_ref,
            "a": p.sub,
        },
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="courier_account.set",
        entity="courier_account",
        entity_id=body.courier,
        data={"courier": body.courier, "mode": body.mode},
        request=request,
    )
    return await _courier_health(request, db, tenant, body.courier)


@admin.post("/courier-accounts/health")
async def courier_health(
    body: CourierIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    return await _courier_health(request, db, tenant, body.courier)


@admin.post("/courier-accounts/disable")
async def disable_courier(
    body: CourierIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    res = await db.execute(
        text(
            "UPDATE courier_accounts SET status = 'disabled', updated_by = :a, updated_at = now() "
            "WHERE tenant_id = :t AND courier = :c AND vendor_id IS NULL"
        ),
        {"a": p.sub, "t": tenant.id, "c": body.courier},
    )
    if res.rowcount == 0:
        raise NotFound("Not configured")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="courier_account.disable",
        entity="courier_account",
        entity_id=body.courier,
        request=request,
    )
    return Response(status_code=204)


async def _courier_health(request: Request, db, tenant: Tenant, courier: str) -> dict:
    account = await service.load_account(db, request.app.state.settings, tenant.id, courier)
    adapter = build_courier(courier, account.mode, _overrides(request.app))
    status, error = "healthy", None
    try:
        await adapter.health(credentials=account.credentials)
    except Exception as exc:
        status, error = "failing", str(exc)[:500]
    row = (
        (
            await db.execute(
                text(
                    """UPDATE courier_accounts SET status = :s, last_error = :e, last_checked_at = now()
                       WHERE tenant_id = :t AND courier = :c AND vendor_id IS NULL RETURNING *"""
                ),
                {"s": status, "e": error, "t": tenant.id, "c": courier},
            )
        )
        .mappings()
        .one()
    )
    return _account_view(row, account.credentials)


@admin.get("/courier-rules")
async def get_rules(p: SettingsWrite, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT courier, districts, zones, max_weight_grams, max_cod_amount, enabled, priority "
                    "FROM courier_rules WHERE tenant_id = :t ORDER BY priority, created_at"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.put("/courier-rules")
async def put_rules(
    body: RulesIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    """The list is the policy: it is replaced as a whole, in the order given."""
    await db.execute(text("DELETE FROM courier_rules WHERE tenant_id = :t"), {"t": tenant.id})
    for i, rule in enumerate(body.rules):
        await db.execute(
            text(
                """INSERT INTO courier_rules (tenant_id, priority, courier, districts, zones,
                       max_weight_grams, max_cod_amount, enabled)
                   VALUES (:t, :p, :c, CAST(:d AS text[]), CAST(:z AS text[]), :w, :cod, :e)"""
            ),
            {
                "t": tenant.id,
                "p": (i + 1) * 10,
                "c": rule.courier,
                "d": rule.districts,
                "z": [str(z) for z in rule.zones],
                "w": rule.max_weight_grams,
                "cod": rule.max_cod_amount,
                "e": rule.enabled,
            },
        )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="courier_rules.set",
        entity="courier_rules",
        data={"count": len(body.rules)},
        request=request,
    )
    return await get_rules(p, tenant, db)


@admin.get("/shipments")
async def admin_shipments(
    p: OrdersRead,
    tenant: Tenant,
    db: TenantDB,
    status: str | None = None,
    needs_attention: bool | None = None,
    vendor_id: uuid.UUID | None = None,
    limit: int = 50,
):
    rows = (
        (
            await db.execute(
                text(
                    SHIPMENT_SQL
                    + """ AND (CAST(:s AS text) IS NULL OR s.status = :s)
                          AND (CAST(:a AS boolean) IS NULL OR s.needs_attention = :a)
                          AND (CAST(:v AS uuid) IS NULL OR s.vendor_id = :v)
                        ORDER BY s.booked_at DESC LIMIT :l"""
                ),
                {
                    "t": tenant.id,
                    "s": status,
                    "a": needs_attention,
                    "v": vendor_id,
                    "l": min(limit, 200),
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/shipments/{shipment_id}")
async def admin_shipment(shipment_id: uuid.UUID, p: OrdersRead, tenant: Tenant, db: TenantDB):
    row = (
        (
            await db.execute(
                text(SHIPMENT_SQL + " AND s.id = :i"), {"t": tenant.id, "i": shipment_id}
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    events = (
        (
            await db.execute(
                text(
                    """SELECT raw_status, status, occurred_at, received_at, payload FROM shipment_events
                       WHERE tenant_id = :t AND shipment_id = :i ORDER BY received_at"""
                ),
                {"t": tenant.id, "i": shipment_id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(row), "events": [dict(e) for e in events]}


class ResolveIn(BaseModel):
    note: str | None = Field(default=None, max_length=250)


@admin.post("/shipments/{shipment_id}/resolve")
async def resolve_shipment(
    shipment_id: uuid.UUID,
    body: ResolveIn,
    request: Request,
    p: OrdersWrite,
    tenant: Tenant,
    db: TenantDB,
):
    """Ops looked at the parcel: the flag clears, the reason stays in the audit log."""
    res = await db.execute(
        text(
            "UPDATE shipments SET needs_attention = false, attention_reason = NULL "
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"t": tenant.id, "i": shipment_id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="shipment.resolve",
        entity="shipment",
        entity_id=shipment_id,
        data={"note": body.note},
        request=request,
    )
    return {"needs_attention": False}


@admin.post("/courier-settlements", status_code=201)
async def import_settlement(
    request: Request,
    p: OrdersWrite,
    tenant: Tenant,
    db: TenantDB,
    courier: Literal["pathao", "steadfast", "redx"] = Form(...),
    statement_ref: str = Form(..., max_length=80),
    period_start: Annotated[date | None, Form()] = None,
    period_end: Annotated[date | None, Form()] = None,
    file: UploadFile = File(...),
):
    raw = await file.read()
    lines = service.parse_statement_csv(raw)
    out = await service.import_settlement(
        db,
        tenant.id,
        courier=courier,
        statement_ref=statement_ref,
        lines=lines,
        actor_id=p.sub,
        period_start=period_start,
        period_end=period_end,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="courier_settlement.import",
        entity="courier_settlement",
        entity_id=out["id"],
        data={k: str(v) for k, v in out.items() if k != "id"},
        request=request,
    )
    return out


@admin.get("/courier-settlements")
async def list_settlements(p: OrdersRead, tenant: Tenant, db: TenantDB, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM courier_settlements WHERE tenant_id = :t "
                    "ORDER BY imported_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [{k: v for k, v in r.items() if k != "tenant_id"} for r in rows]


@admin.get("/courier-settlement-lines")
async def settlement_lines(
    p: OrdersRead,
    tenant: Tenant,
    db: TenantDB,
    statement_ref: str | None = None,
    status: str | None = None,
    limit: int = 200,
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT l.consignment_id, l.amount, l.fee, l.status, l.note, c.statement_ref, c.courier
                       FROM courier_settlement_lines l
                       JOIN courier_settlements c ON c.id = l.settlement_id AND c.tenant_id = l.tenant_id
                       WHERE l.tenant_id = :t AND (CAST(:r AS text) IS NULL OR c.statement_ref = :r)
                         AND (CAST(:s AS text) IS NULL OR l.status = :s)
                       ORDER BY l.status, l.consignment_id LIMIT :l"""
                ),
                {"t": tenant.id, "r": statement_ref, "s": status, "l": min(limit, 500)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
