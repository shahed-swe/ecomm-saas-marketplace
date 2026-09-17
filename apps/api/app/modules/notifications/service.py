"""One notification service for every channel (architecture §11).

The rules that matter:

* **Transactional always goes; marketing asks permission.** An order update reaches the buyer
  whatever their marketing preferences say. A "still in your cart" push does not, and it never
  arrives during the tenant's quiet hours either.
* **Send once.** Every notification may carry a `dedupe_key`; a unique index makes a retried job or
  a replayed webhook harmless.
* **A failed message never breaks the thing it describes.** Delivery errors are written to the
  notification row, not raised into the caller's transaction.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.modules.notifications import templates

CHANNELS = ("push", "sms", "email", "in_app")


@dataclass
class Recipient:
    user_id: str | None = None
    vendor_id: str | None = None
    staff_id: str | None = None
    phone: str | None = None
    email: str | None = None
    locale: str = "bn"


async def recipient_for_user(db: AsyncSession, tenant_id: str, user_id: str) -> Recipient | None:
    row = (
        (
            await db.execute(
                text(
                    "SELECT id, phone, email, locale FROM users WHERE tenant_id = :t AND id = CAST(:u AS uuid)"
                ),
                {"t": tenant_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return Recipient(
        user_id=str(row["id"]),
        phone=row["phone"],
        email=row["email"],
        locale=row["locale"] or "bn",
    )


async def _tenant_context(db: AsyncSession, tenant_id: str) -> dict:
    row = (
        (
            await db.execute(
                text(
                    """SELECT t.name, t.default_locale, ts.marketing_quiet_start, ts.marketing_quiet_end
                       FROM tenants t JOIN tenant_settings ts ON ts.tenant_id = t.id WHERE t.id = :t"""
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {}


async def _template(
    db: AsyncSession, tenant_id: str, key: str, channel: str, locale: str, data: dict
) -> tuple[str | None, str] | None:
    """A tenant's own wording wins; ours is the safety net."""
    row = (
        (
            await db.execute(
                text(
                    """SELECT subject, body, enabled FROM notification_templates
                       WHERE tenant_id = :t AND key = :k AND channel = :c
                         AND locale IN (:l, 'bn', 'en')
                       ORDER BY (locale = :l) DESC, (locale = 'bn') DESC LIMIT 1"""
                ),
                {"t": tenant_id, "k": key, "c": channel, "l": locale},
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        if not row["enabled"]:
            return None
        safe = templates._Safe(data)
        return (
            row["subject"].format_map(safe) if row["subject"] else None,
            row["body"].format_map(safe),
        )
    rendered = templates.render(key, channel, locale, data)
    if rendered is None and channel == "in_app":
        # The inbox mirrors the push copy: one wording to maintain, one thing the buyer reads.
        return await _template(db, tenant_id, key, "push", locale, data)
    return rendered


def _in_quiet_hours(now: datetime, start: int | None, end: int | None) -> bool:
    """Bangladesh is UTC+6; quiet hours are expressed in the tenant's local clock.

    Equal start and end means "no quiet hours" — the simplest way for a tenant to switch them off.
    """
    if start is None or end is None or start == end:
        return False
    hour = (now.astimezone(UTC).hour + 6) % 24
    return start <= hour or hour < end if start > end else start <= hour < end


async def _marketing_allowed(
    db: AsyncSession, tenant_id: str, recipient: Recipient, channel: str, context: dict
) -> bool:
    if recipient.user_id is None:
        return True
    prefs = (
        (
            await db.execute(
                text(
                    "SELECT * FROM notification_preferences WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)"
                ),
                {"t": tenant_id, "u": recipient.user_id},
            )
        )
        .mappings()
        .first()
    )
    column = {"push": "marketing_push", "sms": "marketing_sms", "email": "marketing_email"}.get(
        channel
    )
    if prefs is not None and column and not prefs[column]:
        return False
    start = (prefs or {}).get("quiet_hours_start", context.get("marketing_quiet_start"))
    end = (prefs or {}).get("quiet_hours_end", context.get("marketing_quiet_end"))
    return not _in_quiet_hours(datetime.now(UTC), start, end)


async def notify(
    db: AsyncSession,
    app_state,
    tenant_id: str,
    *,
    key: str,
    recipient: Recipient,
    data: dict,
    channels: tuple[str, ...] = ("push", "in_app"),
    dedupe_key: str | None = None,
    deep_link: str | None = None,
) -> list[dict]:
    """Render, decide, record, deliver — in that order, and never raising into the caller."""
    context = await _tenant_context(db, tenant_id)
    kind = "marketing" if key in templates.MARKETING_KEYS else "transactional"
    locale = recipient.locale or context.get("default_locale") or "bn"
    payload = {"store": context.get("name", ""), **data}
    results = []
    for channel in channels:
        rendered = await _template(db, tenant_id, key, channel, locale, payload)
        if rendered is None:
            continue
        title, body = rendered
        suppressed = None
        if kind == "marketing" and not await _marketing_allowed(
            db, tenant_id, recipient, channel, context
        ):
            suppressed = "opted_out_or_quiet_hours"
        if channel == "sms" and not recipient.phone:
            suppressed = "no_phone"
        if channel == "email" and not recipient.email:
            suppressed = "no_email"
        row = (
            await db.execute(
                text(
                    """INSERT INTO notifications (tenant_id, user_id, vendor_id, staff_id, channel, key,
                           kind, locale, title, body, data, deep_link, status, dedupe_key)
                       VALUES (:t, :u, :v, :st, :c, :k, :kind, :l, :title, :body, CAST(:d AS jsonb),
                               :link, :status, :dk)
                       ON CONFLICT (tenant_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                       RETURNING id"""
                ),
                {
                    "t": tenant_id,
                    "u": recipient.user_id,
                    "v": recipient.vendor_id,
                    "st": recipient.staff_id,
                    "c": channel,
                    "k": key,
                    "kind": kind,
                    "l": locale,
                    "title": title,
                    "body": body,
                    "d": json.dumps(payload, default=str),
                    "link": deep_link,
                    "status": "suppressed" if suppressed else "queued",
                    "dk": f"{dedupe_key}:{channel}" if dedupe_key else None,
                },
            )
        ).first()
        if row is None:
            results.append({"channel": channel, "status": "duplicate"})
            continue
        if suppressed:
            await db.execute(
                text("UPDATE notifications SET error = :e WHERE tenant_id = :t AND id = :i"),
                {"e": suppressed, "t": tenant_id, "i": row.id},
            )
            results.append({"channel": channel, "status": "suppressed", "reason": suppressed})
            continue
        status, error, ref = await _deliver(
            db, app_state, tenant_id, channel, recipient, title, body, payload
        )
        await db.execute(
            text(
                """UPDATE notifications SET status = :s, error = :e, provider_ref = :r,
                          sent_at = CASE WHEN :s = 'sent' THEN now() ELSE NULL END
                   WHERE tenant_id = :t AND id = :i"""
            ),
            {"s": status, "e": error, "r": ref, "t": tenant_id, "i": row.id},
        )
        results.append({"channel": channel, "status": status, "id": str(row.id)})
    return results


async def _deliver(
    db: AsyncSession,
    app_state,
    tenant_id: str,
    channel: str,
    recipient: Recipient,
    title: str | None,
    body: str,
    data: dict,
) -> tuple[str, str | None, str | None]:
    try:
        if channel == "in_app":
            return "sent", None, None
        if channel == "sms":
            await app_state.sms.send(tenant_id=tenant_id, phone=recipient.phone, message=body)
            return "sent", None, None
        if channel == "email":
            ref = await app_state.email.send(
                tenant_id=tenant_id, to=recipient.email, subject=title or "", body=body
            )
            return "sent", None, ref
        if channel == "push":
            tokens = await _tokens(db, tenant_id, recipient)
            if not tokens:
                return "suppressed", "no_device", None
            delivered = await app_state.push.send(
                tenant_id=tenant_id, tokens=tokens, title=title or "", body=body, data=data
            )
            return ("sent" if delivered else "failed"), None, None
    except Exception as exc:  # delivery must never break the caller's transaction
        log.warning("notification_failed", channel=channel, error=str(exc)[:200])
        return "failed", str(exc)[:500], None
    return "failed", f"unknown channel {channel}", None


async def _tokens(db: AsyncSession, tenant_id: str, recipient: Recipient) -> list[str]:
    if not recipient.user_id:
        return []
    return list(
        (
            await db.execute(
                text(
                    """SELECT token FROM device_tokens
                       WHERE tenant_id = :t AND user_id = CAST(:u AS uuid) AND revoked_at IS NULL
                       ORDER BY last_seen_at DESC LIMIT 10"""
                ),
                {"t": tenant_id, "u": recipient.user_id},
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------------------------------- event helpers
async def on_order_placed(db, app_state, tenant_id: str, *, order: dict) -> None:
    recipient = await recipient_for_user(db, tenant_id, order["user_id"])
    if recipient is None:
        return
    await notify(
        db,
        app_state,
        tenant_id,
        key="order.placed",
        recipient=recipient,
        data={"number": order["number"], "total": order["total"]},
        channels=("push", "sms", "in_app"),
        dedupe_key=f"order.placed:{order['id']}",
        deep_link=f"/orders/{order['number']}",
    )


async def on_payment_paid(db, app_state, tenant_id: str, *, order_id) -> None:
    order = (
        (
            await db.execute(
                text(
                    "SELECT id, number, user_id, grand_total FROM orders WHERE tenant_id = :t AND id = :o"
                ),
                {"t": tenant_id, "o": order_id},
            )
        )
        .mappings()
        .first()
    )
    if order is None or order["user_id"] is None:
        return
    recipient = await recipient_for_user(db, tenant_id, str(order["user_id"]))
    if recipient is None:
        return
    await notify(
        db,
        app_state,
        tenant_id,
        key="payment.paid",
        recipient=recipient,
        data={"number": order["number"], "total": order["grand_total"]},
        channels=("push", "sms", "in_app"),
        dedupe_key=f"payment.paid:{order['id']}",
        deep_link=f"/orders/{order['number']}",
    )


async def on_shipment_status(db, app_state, tenant_id: str, *, shipment_id, status: str) -> None:
    # Couriers skip statuses, so anything from pick-up onwards means "it is on its way"; the
    # dedupe key is the shipment, so the buyer hears that once however many events arrive.
    key = {
        "picked_up": "shipment.shipped",
        "in_transit": "shipment.shipped",
        "out_for_delivery": "shipment.shipped",
        "delivered": "shipment.delivered",
        "partial_delivered": "shipment.delivered",
    }.get(status)
    if key is None:
        return
    row = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.courier, s.tracking_code, so.number, o.user_id, o.number AS order_number
                       FROM shipments s
                       JOIN sub_orders so ON so.id = s.sub_order_id AND so.tenant_id = s.tenant_id
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.id = :i"""
                ),
                {"t": tenant_id, "i": shipment_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None or row["user_id"] is None:
        return
    recipient = await recipient_for_user(db, tenant_id, str(row["user_id"]))
    if recipient is None:
        return
    await notify(
        db,
        app_state,
        tenant_id,
        key=key,
        recipient=recipient,
        data={
            "number": row["number"],
            "courier": row["courier"],
            "tracking": row["tracking_code"] or "",
        },
        channels=("push", "in_app") if key == "shipment.delivered" else ("push", "sms", "in_app"),
        dedupe_key=f"{key}:{row['id']}",
        deep_link=f"/orders/{row['order_number']}",
    )


async def on_refund_completed(
    db, app_state, tenant_id: str, *, user_id: str, amount, method: str, refund_id
) -> None:
    recipient = await recipient_for_user(db, tenant_id, user_id)
    if recipient is None:
        return
    await notify(
        db,
        app_state,
        tenant_id,
        key="refund.completed",
        recipient=recipient,
        data={"amount": amount, "method": method.replace("manual_", "")},
        channels=("push", "sms", "in_app"),
        dedupe_key=f"refund.completed:{refund_id}",
    )
