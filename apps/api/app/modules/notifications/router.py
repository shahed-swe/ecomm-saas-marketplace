"""Notification, marketing, analytics and support surfaces."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
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
from app.core.errors import Forbidden, NotFound
from app.core.security import Principal
from app.modules.notifications import analytics, campaigns, support

buyer = APIRouter(prefix="/api/v1", tags=["notifications"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:support"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:notifications"])

Marketing = Annotated[Principal, Depends(require_tenant_staff("marketing.write"))]
SupportStaff = Annotated[Principal, Depends(require_tenant_staff("support.tickets"))]
SettingsWrite = Annotated[Principal, Depends(require_tenant_staff("settings.write"))]
VendorSeller = Annotated[Principal, Depends(require_vendor_role("orders.write", approved=True))]


def _buyer(p: Principal) -> Principal:
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    return p


# --------------------------------------------------------------------------- devices & inbox
class DeviceIn(BaseModel):
    token: str = Field(min_length=10, max_length=512)
    platform: Literal["ios", "android", "web"]
    app: Literal["buyer", "vendor"] = "buyer"
    locale: Literal["bn", "en"] = "bn"


@buyer.post("/me/devices", status_code=201)
async def register_device(body: DeviceIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    """A token belongs to one person: re-registering moves it rather than duplicating it."""
    await db.execute(
        text(
            """INSERT INTO device_tokens (tenant_id, user_id, platform, app, token, locale)
               VALUES (:t, :u, :p, :a, :tok, :l)
               ON CONFLICT (tenant_id, token) DO UPDATE
               SET user_id = EXCLUDED.user_id, platform = EXCLUDED.platform, app = EXCLUDED.app,
                   locale = EXCLUDED.locale, last_seen_at = now(), revoked_at = NULL"""
        ),
        {
            "t": tenant.id,
            "u": p.sub,
            "p": body.platform,
            "a": body.app,
            "tok": body.token,
            "l": body.locale,
        },
    )
    return {"registered": True}


@buyer.delete("/me/devices/{token}", status_code=204)
async def revoke_device(token: str, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    await db.execute(
        text(
            "UPDATE device_tokens SET revoked_at = now() WHERE tenant_id = :t AND token = :tok "
            "AND user_id = CAST(:u AS uuid)"
        ),
        {"t": tenant.id, "tok": token, "u": p.sub},
    )
    return Response(status_code=204)


@buyer.get("/me/notifications")
async def inbox(p: CurrentPrincipal, tenant: Tenant, db: TenantDB, limit: int = 30):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, key, title, body, deep_link, status, created_at, read_at
                       FROM notifications
                       WHERE tenant_id = :t AND user_id = CAST(:u AS uuid) AND channel = 'in_app'
                       ORDER BY created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "u": p.sub, "l": min(limit, 100)},
            )
        )
        .mappings()
        .all()
    )
    unread = sum(1 for r in rows if r["read_at"] is None)
    return {"unread": unread, "items": [dict(r) for r in rows]}


@buyer.post("/me/notifications/read", status_code=204)
async def mark_read(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    await db.execute(
        text(
            "UPDATE notifications SET read_at = now(), status = 'read' WHERE tenant_id = :t "
            "AND user_id = CAST(:u AS uuid) AND channel = 'in_app' AND read_at IS NULL"
        ),
        {"t": tenant.id, "u": p.sub},
    )
    return Response(status_code=204)


class PreferencesIn(BaseModel):
    marketing_push: bool = True
    marketing_sms: bool = False
    marketing_email: bool = True
    quiet_hours_start: int | None = Field(default=None, ge=0, le=23)
    quiet_hours_end: int | None = Field(default=None, ge=0, le=23)


@buyer.get("/me/notification-preferences")
async def get_preferences(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM notification_preferences WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)"
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .first()
    )
    return (
        {k: v for k, v in row.items() if k not in ("tenant_id", "user_id")}
        if row
        else PreferencesIn().model_dump()
    )


@buyer.put("/me/notification-preferences")
async def set_preferences(body: PreferencesIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    await db.execute(
        text(
            """INSERT INTO notification_preferences (tenant_id, user_id, marketing_push, marketing_sms,
                   marketing_email, quiet_hours_start, quiet_hours_end)
               VALUES (:t, :u, :p, :s, :e, :qs, :qe)
               ON CONFLICT (tenant_id, user_id) DO UPDATE
               SET marketing_push = EXCLUDED.marketing_push, marketing_sms = EXCLUDED.marketing_sms,
                   marketing_email = EXCLUDED.marketing_email,
                   quiet_hours_start = EXCLUDED.quiet_hours_start,
                   quiet_hours_end = EXCLUDED.quiet_hours_end, updated_at = now()"""
        ),
        {
            "t": tenant.id,
            "u": p.sub,
            "p": body.marketing_push,
            "s": body.marketing_sms,
            "e": body.marketing_email,
            "qs": body.quiet_hours_start,
            "qe": body.quiet_hours_end,
        },
    )
    return body.model_dump()


# ------------------------------------------------------------------------------- push campaigns
class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    segment: Literal["all", "buyers_with_orders", "abandoned_cart", "inactive_30d"] = "all"
    title: str = Field(min_length=2, max_length=80)
    body: str = Field(min_length=2, max_length=300)
    deep_link: str | None = Field(default=None, max_length=200)


@admin.post("/push-campaigns", status_code=201)
async def create_campaign(
    body: CampaignIn, request: Request, p: Marketing, tenant: Tenant, db: TenantDB
):
    campaign_id = (
        await db.execute(
            text(
                """INSERT INTO push_campaigns (tenant_id, name, segment, title, body, deep_link, created_by)
                   VALUES (:t, :n, :s, :title, :body, :link, :a) RETURNING id"""
            ),
            {
                "t": tenant.id,
                "n": body.name,
                "s": body.segment,
                "title": body.title,
                "body": body.body,
                "link": body.deep_link,
                "a": p.sub,
            },
        )
    ).scalar()
    audience = await campaigns.audience(db, tenant.id, body.segment)
    await db.execute(
        text("UPDATE push_campaigns SET audience_count = :a WHERE tenant_id = :t AND id = :i"),
        {"a": len(audience), "t": tenant.id, "i": campaign_id},
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="push_campaign.create",
        entity="push_campaign",
        entity_id=campaign_id,
        data={"segment": body.segment, "audience": len(audience)},
        request=request,
    )
    return {"id": str(campaign_id), "status": "draft", "audience": len(audience)}


@admin.get("/push-campaigns")
async def list_campaigns(p: Marketing, tenant: Tenant, db: TenantDB, limit: int = 50):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM push_campaigns WHERE tenant_id = :t ORDER BY created_at DESC LIMIT :l"
                ),
                {"t": tenant.id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [{k: v for k, v in r.items() if k != "tenant_id"} for r in rows]


@admin.post("/push-campaigns/{campaign_id}/send")
async def send_campaign(
    campaign_id: uuid.UUID, request: Request, p: Marketing, tenant: Tenant, db: TenantDB
):
    out = await campaigns.send_campaign(db, request.app.state, tenant.id, campaign_id)
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="push_campaign.send",
        entity="push_campaign",
        entity_id=campaign_id,
        data={k: v for k, v in out.items() if k != "id"},
        request=request,
    )
    return out


@admin.post("/push-campaigns/{campaign_id}/cancel", status_code=204)
async def cancel_campaign(campaign_id: uuid.UUID, p: Marketing, tenant: Tenant, db: TenantDB):
    res = await db.execute(
        text(
            "UPDATE push_campaigns SET status = 'cancelled' WHERE tenant_id = :t AND id = :i "
            "AND status IN ('draft','scheduled')"
        ),
        {"t": tenant.id, "i": campaign_id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    return Response(status_code=204)


# --------------------------------------------------------------------------------- templates
class TemplateIn(BaseModel):
    key: str = Field(min_length=3, max_length=60)
    channel: Literal["push", "sms", "email", "in_app"]
    locale: Literal["bn", "en"] = "bn"
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=2, max_length=2000)
    enabled: bool = True


@admin.put("/notification-templates")
async def put_template(body: TemplateIn, p: Marketing, tenant: Tenant, db: TenantDB):
    """Tenants may reword anything; the platform's defaults remain the fallback."""
    await db.execute(
        text(
            """INSERT INTO notification_templates (tenant_id, key, channel, locale, subject, body,
                   enabled, updated_by)
               VALUES (:t, :k, :c, :l, :s, :b, :e, :a)
               ON CONFLICT (tenant_id, key, channel, locale) DO UPDATE
               SET subject = EXCLUDED.subject, body = EXCLUDED.body, enabled = EXCLUDED.enabled,
                   updated_by = EXCLUDED.updated_by, updated_at = now()"""
        ),
        {
            "t": tenant.id,
            "k": body.key,
            "c": body.channel,
            "l": body.locale,
            "s": body.subject,
            "b": body.body,
            "e": body.enabled,
            "a": p.sub,
        },
    )
    return body.model_dump()


@admin.get("/notification-templates")
async def list_templates(p: Marketing, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT key, channel, locale, subject, body, enabled, updated_at "
                    "FROM notification_templates WHERE tenant_id = :t ORDER BY key, channel"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/notifications")
async def notification_log(
    p: Marketing, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, channel, key, kind, status, error, created_at, sent_at
                       FROM notifications WHERE tenant_id = :t
                         AND (CAST(:s AS text) IS NULL OR status = :s)
                       ORDER BY created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------------- analytics
class DestinationIn(BaseModel):
    provider: Literal["ga4", "meta"]
    public_id: str = Field(min_length=3, max_length=80)
    secret: str | None = Field(default=None, max_length=400)
    server_side: bool = True


@admin.put("/analytics-destinations")
async def put_destination(
    body: DestinationIn, request: Request, p: SettingsWrite, tenant: Tenant, db: TenantDB
):
    ciphertext = (
        analytics.encrypt_secret(request.app.state.settings, tenant.id, body.provider, body.secret)
        if body.secret
        else None
    )
    await db.execute(
        text(
            """INSERT INTO analytics_destinations (tenant_id, provider, public_id, secret_ciphertext,
                   server_side, updated_by)
               VALUES (:t, :p, :pub, :sec, :ss, :a)
               ON CONFLICT (tenant_id, provider) DO UPDATE
               SET public_id = EXCLUDED.public_id,
                   secret_ciphertext = COALESCE(EXCLUDED.secret_ciphertext,
                                                analytics_destinations.secret_ciphertext),
                   server_side = EXCLUDED.server_side, status = 'active',
                   updated_by = EXCLUDED.updated_by, updated_at = now()"""
        ),
        {
            "t": tenant.id,
            "p": body.provider,
            "pub": body.public_id,
            "sec": ciphertext,
            "ss": body.server_side,
            "a": p.sub,
        },
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="analytics_destination.set",
        entity="analytics_destination",
        entity_id=body.provider,
        data={"provider": body.provider, "public_id": body.public_id},
        request=request,
    )
    return await list_destinations(p, tenant, db)


@admin.get("/analytics-destinations")
async def list_destinations(p: SettingsWrite, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT provider, public_id, server_side, status, secret_ciphertext, updated_at "
                    "FROM analytics_destinations WHERE tenant_id = :t ORDER BY provider"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "provider": r["provider"],
            "public_id": r["public_id"],
            "server_side": r["server_side"],
            "status": r["status"],
            "secret_set": bool(r["secret_ciphertext"]),
            "secret_hint": f"••••{last4(r['secret_ciphertext'])}"
            if r["secret_ciphertext"]
            else None,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@buyer.get("/storefront/analytics")
async def storefront_analytics(tenant: Tenant, db: TenantDB):
    """What the storefront may put in the page: public ids only, never a secret."""
    rows = (
        (
            await db.execute(
                text(
                    "SELECT provider, public_id FROM analytics_destinations "
                    "WHERE tenant_id = :t AND status = 'active' AND public_id IS NOT NULL"
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return {r["provider"]: r["public_id"] for r in rows}


# ----------------------------------------------------------------------------- support tickets
class TicketIn(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=8000)
    category: Literal["order", "payment", "delivery", "return", "account", "vendor", "other"] = (
        "other"
    )
    order_number: str | None = Field(default=None, max_length=40)


class TicketMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    internal: bool = False


class TicketStatusIn(BaseModel):
    status: Literal["open", "pending_customer", "pending_staff", "resolved", "closed"]
    assignee_id: uuid.UUID | None = None


@buyer.post("/me/tickets", status_code=201)
async def open_ticket(body: TicketIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    return await support.create_ticket(
        db,
        tenant.id,
        subject=body.subject,
        body=body.body,
        category=body.category,
        user_id=p.sub,
        order_number=body.order_number,
    )


@buyer.get("/me/tickets")
async def my_tickets(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, number, subject, category, status, created_at, updated_at
                       FROM support_tickets WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)
                       ORDER BY updated_at DESC"""
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@buyer.get("/me/tickets/{ticket_id}")
async def my_ticket(ticket_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    ticket = await support.thread(db, tenant.id, ticket_id, include_internal=False)
    if str(ticket["user_id"]) != str(p.sub):
        raise NotFound("Not found")
    return ticket


@buyer.post("/me/tickets/{ticket_id}/messages", status_code=201)
async def reply_ticket(
    ticket_id: uuid.UUID, body: TicketMessageIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    ticket = await support.thread(db, tenant.id, ticket_id, include_internal=False)
    if str(ticket["user_id"]) != str(p.sub):
        raise NotFound("Not found")
    return await support.add_message(
        db, tenant.id, ticket_id, author_kind="customer", author_id=p.sub, body=body.body
    )


@vendor.post("/tickets", status_code=201)
async def vendor_ticket(body: TicketIn, p: VendorSeller, tenant: Tenant, db: TenantDB):
    return await support.create_ticket(
        db,
        tenant.id,
        subject=body.subject,
        body=body.body,
        category=body.category,
        vendor_id=p.vid,
    )


@admin.get("/tickets")
async def staff_tickets(
    p: SupportStaff, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT t.id, t.number, t.subject, t.category, t.priority, t.status,
                              t.first_response_at, t.created_at, t.updated_at, u.email AS customer_email,
                              v.display_name AS vendor_name
                       FROM support_tickets t
                       LEFT JOIN users u ON u.id = t.user_id AND u.tenant_id = t.tenant_id
                       LEFT JOIN vendors v ON v.id = t.vendor_id AND v.tenant_id = t.tenant_id
                       WHERE t.tenant_id = :t AND (CAST(:s AS text) IS NULL OR t.status = :s)
                       ORDER BY t.updated_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.get("/tickets/{ticket_id}")
async def staff_ticket(ticket_id: uuid.UUID, p: SupportStaff, tenant: Tenant, db: TenantDB):
    return await support.thread(db, tenant.id, ticket_id, include_internal=True)


@admin.post("/tickets/{ticket_id}/messages", status_code=201)
async def staff_reply(
    ticket_id: uuid.UUID, body: TicketMessageIn, p: SupportStaff, tenant: Tenant, db: TenantDB
):
    return await support.add_message(
        db,
        tenant.id,
        ticket_id,
        author_kind="staff",
        author_id=p.sub,
        body=body.body,
        internal=body.internal,
    )


@admin.post("/tickets/{ticket_id}/status")
async def staff_ticket_status(
    ticket_id: uuid.UUID,
    body: TicketStatusIn,
    request: Request,
    p: SupportStaff,
    tenant: Tenant,
    db: TenantDB,
):
    out = await support.set_status(
        db,
        tenant.id,
        ticket_id,
        status=body.status,
        assignee_id=str(body.assignee_id) if body.assignee_id else None,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="ticket.status",
        entity="ticket",
        entity_id=ticket_id,
        data={"status": body.status},
        request=request,
    )
    return out
