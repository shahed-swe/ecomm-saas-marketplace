"""Push campaigns and the abandoned-cart nudge.

Both are marketing, so both obey opt-outs and quiet hours through `service.notify`. A campaign
records what it *tried* as well as what it sent, because "we sent 4,000" and "3,100 were eligible"
are different facts and a tenant deserves both.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound
from app.modules.notifications import service as notifications

SEGMENTS = {
    "all": """SELECT DISTINCT u.id FROM users u JOIN device_tokens d ON d.user_id = u.id AND d.tenant_id = u.tenant_id
              WHERE u.tenant_id = :t AND d.revoked_at IS NULL AND u.status = 'active'""",
    "buyers_with_orders": """SELECT DISTINCT o.user_id AS id FROM orders o
              WHERE o.tenant_id = :t AND o.user_id IS NOT NULL""",
    "abandoned_cart": """SELECT DISTINCT c.user_id AS id FROM carts c
              JOIN cart_items i ON i.cart_id = c.id AND i.tenant_id = c.tenant_id
              WHERE c.tenant_id = :t AND c.user_id IS NOT NULL
                AND c.updated_at < now() - interval '6 hours'""",
    "inactive_30d": """SELECT u.id FROM users u
              WHERE u.tenant_id = :t AND u.status = 'active'
                AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.tenant_id = u.tenant_id
                                AND o.user_id = u.id AND o.placed_at > now() - interval '30 days')""",
}


class CampaignError(AppError):
    status = 409
    code = "campaign_conflict"


async def audience(db: AsyncSession, tenant_id: str, segment: str) -> list[str]:
    sql = SEGMENTS.get(segment)
    if sql is None:
        raise CampaignError("Unknown segment", code="bad_segment")
    rows = (await db.execute(text(sql), {"t": tenant_id})).scalars().all()  # noqa: S608 - fixed SQL
    return [str(r) for r in rows if r]


async def send_campaign(db: AsyncSession, app_state, tenant_id: str, campaign_id) -> dict:
    campaign = (
        (
            await db.execute(
                text("SELECT * FROM push_campaigns WHERE tenant_id = :t AND id = :i FOR UPDATE"),
                {"t": tenant_id, "i": campaign_id},
            )
        )
        .mappings()
        .first()
    )
    if campaign is None:
        raise NotFound("Not found")
    if campaign["status"] in ("sending", "sent"):
        return {"id": str(campaign_id), "status": campaign["status"], "already": True}
    if campaign["status"] == "cancelled":
        raise CampaignError("This campaign was cancelled", code="cancelled")
    await db.execute(
        text("UPDATE push_campaigns SET status = 'sending' WHERE tenant_id = :t AND id = :i"),
        {"t": tenant_id, "i": campaign_id},
    )
    user_ids = await audience(db, tenant_id, campaign["segment"])
    sent = suppressed = 0
    for user_id in user_ids:
        recipient = await notifications.recipient_for_user(db, tenant_id, user_id)
        if recipient is None:
            continue
        results = await notifications.notify(
            db,
            app_state,
            tenant_id,
            key="campaign.push",
            recipient=recipient,
            data={"title": campaign["title"], "body": campaign["body"]},
            channels=("push",),
            dedupe_key=f"campaign:{campaign_id}:{user_id}",
            deep_link=campaign["deep_link"],
        )
        for result in results:
            if result["status"] == "sent":
                sent += 1
            elif result["status"] in ("suppressed", "duplicate"):
                suppressed += 1
    await db.execute(
        text(
            """UPDATE push_campaigns SET status = 'sent', sent_at = now(), audience_count = :a,
                      sent_count = :s, suppressed_count = :x WHERE tenant_id = :t AND id = :i"""
        ),
        {"a": len(user_ids), "s": sent, "x": suppressed, "t": tenant_id, "i": campaign_id},
    )
    return {
        "id": str(campaign_id),
        "status": "sent",
        "audience": len(user_ids),
        "sent": sent,
        "suppressed": suppressed,
    }


async def nudge_abandoned_carts(
    db: AsyncSession, app_state, tenant_id: str, *, limit: int = 200
) -> dict:
    """One nudge per cart per day, and only for carts that have sat untouched long enough."""
    hours = (
        await db.execute(
            text("SELECT abandoned_cart_hours FROM tenant_settings WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar() or 6
    carts = (
        (
            await db.execute(
                text(
                    """SELECT c.id, c.user_id, count(i.*) AS items, max(i.added_at) AS last_added
                       FROM carts c JOIN cart_items i ON i.cart_id = c.id AND i.tenant_id = c.tenant_id
                       WHERE c.tenant_id = :t AND c.user_id IS NOT NULL
                         AND c.updated_at < now() - make_interval(hours => :h)
                       GROUP BY c.id, c.user_id LIMIT :l"""
                ),
                {"t": tenant_id, "h": hours, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    nudged = 0
    for cart in carts:
        recipient = await notifications.recipient_for_user(db, tenant_id, str(cart["user_id"]))
        if recipient is None:
            continue
        day = cart["last_added"].date().isoformat() if cart["last_added"] else "x"
        results = await notifications.notify(
            db,
            app_state,
            tenant_id,
            key="cart.abandoned",
            recipient=recipient,
            data={"items": cart["items"]},
            channels=("push",),
            dedupe_key=f"cart.abandoned:{cart['id']}:{day}",
            deep_link="/cart",
        )
        nudged += sum(1 for r in results if r["status"] == "sent")
    return {"carts": len(carts), "nudged": nudged}
