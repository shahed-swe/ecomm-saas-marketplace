"""Trust surfaces: reviews (buyer, public, vendor reply, staff moderation), disputes, chat, scores."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core import audit
from app.core.cache_keys import tkey
from app.core.deps import (
    CurrentPrincipal,
    Tenant,
    TenantDB,
    require_tenant_staff,
    require_vendor_role,
)
from app.core.errors import Forbidden, NotFound
from app.core.ratelimit import hit
from app.core.security import Principal
from app.modules.trust import service

buyer = APIRouter(prefix="/api/v1", tags=["trust"])
vendor = APIRouter(prefix="/api/v1/vendor", tags=["vendor:trust"])
admin = APIRouter(prefix="/api/v1/admin", tags=["admin:trust"])

VendorSeller = Annotated[Principal, Depends(require_vendor_role("orders.write", approved=True))]
Moderator = Annotated[Principal, Depends(require_tenant_staff("catalog.moderate"))]
Support = Annotated[Principal, Depends(require_tenant_staff("support.tickets"))]
VendorsRead = Annotated[Principal, Depends(require_tenant_staff("vendors.read"))]

PUBLIC_REVIEW_SQL = """SELECT r.id, r.rating, r.title, r.body, r.helpful_count, r.vendor_reply,
       r.replied_at, r.created_at, u.email
FROM reviews r JOIN users u ON u.id = r.user_id AND u.tenant_id = r.tenant_id
WHERE r.tenant_id = :t AND r.status = 'published'"""


def _buyer(p: Principal) -> Principal:
    if p.kind != "buyer":
        raise Forbidden("Sign in as a customer")
    return p


def _display_name(email: str) -> str:
    """Reviews show a first name and an initial, never a full email address."""
    local = (email or "").split("@")[0]
    parts = [x for x in local.replace(".", " ").replace("_", " ").split() if x]
    if not parts:
        return "Customer"
    first = parts[0].title()
    return f"{first} {parts[1][0].upper()}." if len(parts) > 1 else first


# ------------------------------------------------------------------------------------- reviews
class ReviewIn(BaseModel):
    order_item_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=2000)


@buyer.post("/me/reviews", status_code=201)
async def write_review(
    body: ReviewIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "review", p.sub), 20, 3600)
    return await service.create_review(
        db,
        tenant.id,
        user_id=p.sub,
        order_item_id=body.order_item_id,
        rating=body.rating,
        title=body.title,
        body=body.body,
    )


@buyer.get("/me/reviews")
async def my_reviews(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    """SELECT r.id, r.rating, r.title, r.body, r.status, r.vendor_reply, r.created_at,
                              p.title_en AS product_title
                       FROM reviews r JOIN products p ON p.id = r.product_id AND p.tenant_id = r.tenant_id
                       WHERE r.tenant_id = :t AND r.user_id = CAST(:u AS uuid)
                       ORDER BY r.created_at DESC"""
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@buyer.get("/products/{product_id}/reviews")
async def product_reviews(
    product_id: uuid.UUID, tenant: Tenant, db: TenantDB, limit: int = 20, offset: int = 0
):
    rows = (
        (
            await db.execute(
                text(
                    PUBLIC_REVIEW_SQL
                    + " AND r.product_id = :p ORDER BY r.helpful_count DESC, r.created_at DESC "
                    "LIMIT :l OFFSET :o"
                ),
                {"t": tenant.id, "p": product_id, "l": min(limit, 50), "o": offset},
            )
        )
        .mappings()
        .all()
    )
    summary = (
        (
            await db.execute(
                text(
                    """SELECT rating_avg, rating_count,
                              (SELECT json_object_agg(rating, n) FROM (
                                 SELECT rating, count(*) AS n FROM reviews
                                 WHERE tenant_id = :t AND product_id = :p AND status = 'published'
                                 GROUP BY rating) x) AS histogram
                       FROM products WHERE tenant_id = :t AND id = :p"""
                ),
                {"t": tenant.id, "p": product_id},
            )
        )
        .mappings()
        .first()
    )
    if summary is None:
        raise NotFound("Not found")
    return {
        **dict(summary),
        "reviews": [
            {**{k: v for k, v in r.items() if k != "email"}, "author": _display_name(r["email"])}
            for r in rows
        ],
    }


@buyer.post("/reviews/{review_id}/helpful", status_code=204)
async def vote_helpful(review_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    res = await db.execute(
        text(
            """INSERT INTO review_votes (tenant_id, review_id, user_id) VALUES (:t, :r, :u)
               ON CONFLICT DO NOTHING RETURNING 1"""
        ),
        {"t": tenant.id, "r": review_id, "u": p.sub},
    )
    if res.first() is not None:
        await db.execute(
            text(
                "UPDATE reviews SET helpful_count = helpful_count + 1 WHERE tenant_id = :t AND id = :r"
            ),
            {"t": tenant.id, "r": review_id},
        )
    return Response(status_code=204)


class ReplyIn(BaseModel):
    reply: str = Field(min_length=2, max_length=1000)


@vendor.get("/reviews")
async def vendor_reviews(
    p: VendorSeller, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT r.id, r.rating, r.title, r.body, r.status, r.vendor_reply, r.created_at,
                              pr.title_en AS product_title
                       FROM reviews r JOIN products pr ON pr.id = r.product_id AND pr.tenant_id = r.tenant_id
                       WHERE r.tenant_id = :t AND (CAST(:s AS text) IS NULL OR r.status = :s)
                       ORDER BY r.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.post("/reviews/{review_id}/reply")
async def reply_review(
    review_id: uuid.UUID, body: ReplyIn, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    return await service.reply_to_review(
        db, tenant.id, review_id, vendor_id=p.vid, reply=body.reply
    )


class ModerateIn(BaseModel):
    decision: Literal["approve", "reject", "hide"]


@admin.get("/reviews")
async def admin_reviews(
    p: Moderator, tenant: Tenant, db: TenantDB, status: str | None = None, limit: int = 50
):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT r.id, r.rating, r.title, r.body, r.status, r.flagged_reasons, r.created_at,
                              pr.title_en AS product_title, v.display_name AS vendor_name
                       FROM reviews r
                       JOIN products pr ON pr.id = r.product_id AND pr.tenant_id = r.tenant_id
                       JOIN vendors v ON v.id = r.vendor_id AND v.tenant_id = r.tenant_id
                       WHERE r.tenant_id = :t AND (CAST(:s AS text) IS NULL OR r.status = :s)
                       ORDER BY r.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.post("/reviews/{review_id}/moderate")
async def moderate_review(
    review_id: uuid.UUID,
    body: ModerateIn,
    request: Request,
    p: Moderator,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.moderate_review(
        db, tenant.id, review_id, decision=body.decision, actor_id=p.sub
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="review.moderate",
        entity="review",
        entity_id=review_id,
        data={"decision": body.decision},
        request=request,
    )
    return out


# ------------------------------------------------------------------------------------ disputes
class DisputeIn(BaseModel):
    sub_order_id: uuid.UUID
    reason: Literal["not_received", "not_as_described", "damaged", "refund_not_received", "other"]
    detail: str | None = Field(default=None, max_length=2000)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class ResolveIn(BaseModel):
    in_favour_of: Literal["buyer", "vendor"]
    note: str = Field(min_length=3, max_length=1000)


@buyer.post("/me/disputes", status_code=201)
async def open_dispute(
    body: DisputeIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    out = await service.open_dispute(
        db,
        tenant.id,
        sub_order_id=body.sub_order_id,
        user_id=p.sub,
        reason=body.reason,
        detail=body.detail,
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="dispute.open",
        entity="dispute",
        entity_id=out["id"],
        data={"reason": body.reason, "number": out["number"]},
        request=request,
    )
    return out


@buyer.get("/me/disputes")
async def my_disputes(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    return await _disputes(db, tenant.id, user_id=p.sub)


@buyer.post("/me/disputes/{dispute_id}/messages", status_code=201)
async def buyer_dispute_message(
    dispute_id: uuid.UUID, body: MessageIn, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    await service.load_dispute(db, tenant.id, dispute_id, user_id=p.sub)
    return await service.add_dispute_message(
        db, tenant.id, dispute_id, author_kind="buyer", author_id=p.sub, body=body.body
    )


async def _disputes(db, tenant_id: str, *, user_id=None, vendor_id=None, status=None, limit=50):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT d.id, d.number, d.reason, d.status, d.amount, d.due_at, d.resolution,
                              d.created_at, s.number AS shipment_number, o.number AS order_number,
                              v.display_name AS vendor_name
                       FROM disputes d
                       JOIN sub_orders s ON s.id = d.sub_order_id AND s.tenant_id = d.tenant_id
                       JOIN orders o ON o.id = d.order_id AND o.tenant_id = d.tenant_id
                       JOIN vendors v ON v.id = d.vendor_id AND v.tenant_id = d.tenant_id
                       WHERE d.tenant_id = :t
                         AND (CAST(:u AS uuid) IS NULL OR d.user_id = CAST(:u AS uuid))
                         AND (CAST(:v AS uuid) IS NULL OR d.vendor_id = CAST(:v AS uuid))
                         AND (CAST(:s AS text) IS NULL OR d.status = :s)
                       ORDER BY d.created_at DESC LIMIT :l"""
                ),
                {"t": tenant_id, "u": user_id, "v": vendor_id, "s": status, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/disputes")
async def vendor_disputes(p: VendorSeller, tenant: Tenant, db: TenantDB, status: str | None = None):
    return await _disputes(db, tenant.id, vendor_id=p.vid, status=status)


@vendor.post("/disputes/{dispute_id}/messages", status_code=201)
async def vendor_dispute_message(
    dispute_id: uuid.UUID, body: MessageIn, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    return await service.add_dispute_message(
        db,
        tenant.id,
        dispute_id,
        author_kind="vendor",
        author_id=p.sub,
        body=body.body,
        vendor_id=p.vid,
    )


@admin.get("/disputes")
async def admin_disputes(p: Support, tenant: Tenant, db: TenantDB, status: str | None = None):
    return await _disputes(db, tenant.id, status=status)


@admin.get("/disputes/{dispute_id}")
async def admin_dispute(dispute_id: uuid.UUID, p: Support, tenant: Tenant, db: TenantDB):
    dispute = await service.load_dispute(db, tenant.id, dispute_id)
    messages = (
        (
            await db.execute(
                text(
                    """SELECT author_kind, body, created_at FROM dispute_messages
                       WHERE tenant_id = :t AND dispute_id = :d ORDER BY created_at"""
                ),
                {"t": tenant.id, "d": dispute_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        **{k: v for k, v in dispute.items() if k != "tenant_id"},
        "messages": [dict(m) for m in messages],
    }


@admin.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: uuid.UUID,
    body: ResolveIn,
    request: Request,
    p: Support,
    tenant: Tenant,
    db: TenantDB,
):
    out = await service.resolve_dispute(
        db, tenant.id, dispute_id, in_favour_of=body.in_favour_of, note=body.note, actor_id=p.sub
    )
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="dispute.resolve",
        entity="dispute",
        entity_id=dispute_id,
        data={"in_favour_of": body.in_favour_of},
        request=request,
    )
    return out


# ----------------------------------------------------------------------------------- messaging
class ConversationIn(BaseModel):
    vendor_id: uuid.UUID
    sub_order_id: uuid.UUID | None = None
    body: str = Field(min_length=1, max_length=4000)


@buyer.post("/me/messages", status_code=201)
async def buyer_send(
    body: ConversationIn, request: Request, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    await hit(request.app.state.redis, tkey(tenant.id, "rl", "chat", p.sub), 60, 600)
    conversation_id = await service.ensure_conversation(
        db,
        tenant.id,
        vendor_id=str(body.vendor_id),
        user_id=p.sub,
        sub_order_id=str(body.sub_order_id) if body.sub_order_id else None,
    )
    message = await service.send_message(
        db,
        tenant.id,
        conversation_id=conversation_id,
        sender_kind="buyer",
        sender_id=p.sub,
        body=body.body,
        user_id=p.sub,
    )
    return {
        "conversation_id": conversation_id,
        "id": message.id,
        "body": message.body,
        "redacted": message.redacted,
        "flags": message.flags,
    }


@buyer.get("/me/messages")
async def buyer_threads(p: CurrentPrincipal, tenant: Tenant, db: TenantDB):
    _buyer(p)
    rows = (
        (
            await db.execute(
                text(
                    """SELECT c.id, c.status, c.buyer_unread, c.last_message_at, v.display_name AS vendor_name,
                              s.number AS shipment_number
                       FROM conversations c
                       JOIN vendors v ON v.id = c.vendor_id AND v.tenant_id = c.tenant_id
                       LEFT JOIN sub_orders s ON s.id = c.sub_order_id AND s.tenant_id = c.tenant_id
                       WHERE c.tenant_id = :t AND c.user_id = CAST(:u AS uuid)
                       ORDER BY c.last_message_at DESC NULLS LAST"""
                ),
                {"t": tenant.id, "u": p.sub},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@buyer.get("/me/messages/{conversation_id}")
async def buyer_thread(
    conversation_id: uuid.UUID, p: CurrentPrincipal, tenant: Tenant, db: TenantDB
):
    _buyer(p)
    messages = await _thread(db, tenant.id, conversation_id, user_id=p.sub)
    await service.mark_read(db, tenant.id, conversation_id, side="buyer")
    return messages


async def _thread(db, tenant_id: str, conversation_id, *, user_id=None, vendor_id=None):
    convo = (
        await db.execute(
            text(
                """SELECT id FROM conversations WHERE tenant_id = :t AND id = :i
                   AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))
                   AND (CAST(:v AS uuid) IS NULL OR vendor_id = CAST(:v AS uuid))"""
            ),
            {"t": tenant_id, "i": conversation_id, "u": user_id, "v": vendor_id},
        )
    ).first()
    if convo is None:
        raise NotFound("Not found")
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, sender_kind, body, redacted, flags, created_at FROM messages
                       WHERE tenant_id = :t AND conversation_id = :c ORDER BY created_at"""
                ),
                {"t": tenant_id, "c": conversation_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/messages")
async def vendor_threads(p: VendorSeller, tenant: Tenant, db: TenantDB):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT c.id, c.status, c.vendor_unread, c.last_message_at, s.number AS shipment_number
                       FROM conversations c
                       LEFT JOIN sub_orders s ON s.id = c.sub_order_id AND s.tenant_id = c.tenant_id
                       WHERE c.tenant_id = :t ORDER BY c.last_message_at DESC NULLS LAST"""
                ),
                {"t": tenant.id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@vendor.get("/messages/{conversation_id}")
async def vendor_thread(conversation_id: uuid.UUID, p: VendorSeller, tenant: Tenant, db: TenantDB):
    messages = await _thread(db, tenant.id, conversation_id, vendor_id=p.vid)
    await service.mark_read(db, tenant.id, conversation_id, side="vendor")
    return messages


@vendor.post("/messages/{conversation_id}", status_code=201)
async def vendor_send(
    conversation_id: uuid.UUID, body: MessageIn, p: VendorSeller, tenant: Tenant, db: TenantDB
):
    message = await service.send_message(
        db,
        tenant.id,
        conversation_id=conversation_id,
        sender_kind="vendor",
        sender_id=p.sub,
        body=body.body,
        vendor_id=p.vid,
    )
    return {
        "id": message.id,
        "body": message.body,
        "redacted": message.redacted,
        "flags": message.flags,
    }


@admin.get("/messages/flagged")
async def flagged_messages(p: Support, tenant: Tenant, db: TenantDB, limit: int = 50):
    """What staff need to see: attempts to take a deal off the platform, with who and when."""
    rows = (
        (
            await db.execute(
                text(
                    """SELECT m.id, m.conversation_id, m.sender_kind, m.body, m.flags, m.created_at,
                              v.display_name AS vendor_name
                       FROM messages m
                       JOIN vendors v ON v.id = m.vendor_id AND v.tenant_id = m.tenant_id
                       WHERE m.tenant_id = :t AND cardinality(m.flags) > 0
                       ORDER BY m.created_at DESC LIMIT :l"""
                ),
                {"t": tenant.id, "l": min(limit, 200)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@admin.post("/conversations/{conversation_id}/block", status_code=204)
async def block_conversation(
    conversation_id: uuid.UUID, request: Request, p: Support, tenant: Tenant, db: TenantDB
):
    res = await db.execute(
        text("UPDATE conversations SET status = 'blocked' WHERE tenant_id = :t AND id = :i"),
        {"t": tenant.id, "i": conversation_id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    await audit.record(
        db,
        tenant_id=tenant.id,
        actor=p,
        action="conversation.block",
        entity="conversation",
        entity_id=conversation_id,
        request=request,
    )
    return Response(status_code=204)


# ------------------------------------------------------------------------------ vendor scoring
@admin.get("/vendor-scores")
async def vendor_scores(p: VendorsRead, tenant: Tenant, db: TenantDB, window_days: int = 30):
    rows = (
        (
            await db.execute(
                text(
                    """SELECT s.*, v.display_name AS vendor_name, v.status AS vendor_status
                       FROM vendor_scores s JOIN vendors v ON v.id = s.vendor_id AND v.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.window_days = :w
                       ORDER BY s.score"""
                ),
                {"t": tenant.id, "w": window_days},
            )
        )
        .mappings()
        .all()
    )
    return [{k: v for k, v in r.items() if k != "tenant_id"} for r in rows]


@admin.post("/vendor-scores/recompute")
async def recompute_scores(p: VendorsRead, tenant: Tenant, db: TenantDB, window_days: int = 30):
    return await service.score_all(db, tenant.id, window_days=window_days)


@vendor.get("/scorecard")
async def my_scorecard(p: VendorSeller, tenant: Tenant, db: TenantDB, window_days: int = 30):
    """A vendor can always see the same numbers the tenant sees about them."""
    return await service.score_vendor(db, tenant.id, p.vid, window_days=window_days)
