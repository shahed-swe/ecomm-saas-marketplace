"""Trust & safety: verified reviews, disputes that hold money, vendor scoring, safe messaging.

The shape of the rules here:

* **A review requires a purchase.** One review per order item, only after delivery, inside the
  tenant's review window. There is no route to a review without a paid, delivered line.
* **A dispute holds the vendor's money, not the vendor.** Opening one places a payout hold for the
  amount in question; resolving it releases the hold, whichever way it went.
* **A score describes behaviour, never opinion.** Every input is a fact the system already has:
  late deliveries, cancellations, returns, disputes, and verified ratings.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound
from app.modules.checkout.pricing import money
from app.modules.trust import filters

BANDS = (("good", Decimal("80")), ("watch", Decimal("60")), ("risk", Decimal("0")))


class TrustError(AppError):
    status = 409
    code = "trust_conflict"


# -------------------------------------------------------------------------------------- reviews
async def create_review(
    db: AsyncSession,
    tenant_id: str,
    *,
    user_id: str,
    order_item_id,
    rating: int,
    title: str | None,
    body: str | None,
) -> dict:
    line = (
        (
            await db.execute(
                text(
                    """SELECT i.id, i.product_id, i.vendor_id, s.status, o.user_id,
                              sh.delivered_at, ts.review_mode, ts.review_window_days
                       FROM order_items i
                       JOIN sub_orders s ON s.id = i.sub_order_id AND s.tenant_id = i.tenant_id
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       JOIN tenant_settings ts ON ts.tenant_id = i.tenant_id
                       LEFT JOIN shipments sh ON sh.sub_order_id = s.id AND sh.tenant_id = s.tenant_id
                       WHERE i.tenant_id = :t AND i.id = :i"""
                ),
                {"t": tenant_id, "i": order_item_id},
            )
        )
        .mappings()
        .first()
    )
    if line is None or str(line["user_id"]) != str(user_id):
        raise NotFound("Not found")
    if line["status"] != "delivered":
        raise TrustError("You can review an item once it has been delivered", code="not_delivered")
    delivered = line["delivered_at"] or datetime.now(UTC)
    if datetime.now(UTC) > delivered + timedelta(days=line["review_window_days"]):
        raise TrustError("The review window for this item has closed", code="window_closed")

    text_blob = " ".join(x for x in (title, body) if x)
    flags = filters.scan(text_blob) if text_blob else []
    status = "pending" if (line["review_mode"] == "manual" or flags) else "published"
    try:
        review_id = (
            await db.execute(
                text(
                    """INSERT INTO reviews (tenant_id, vendor_id, product_id, user_id, order_item_id,
                           rating, title, body, status, flagged_reasons)
                       VALUES (:t, :v, :p, :u, :i, :r, :title, :body, :s, CAST(:f AS text[]))
                       RETURNING id"""
                ),
                {
                    "t": tenant_id,
                    "v": line["vendor_id"],
                    "p": line["product_id"],
                    "u": user_id,
                    "i": order_item_id,
                    "r": rating,
                    "title": title,
                    "body": body,
                    "s": status,
                    "f": flags,
                },
            )
        ).scalar()
    except Exception as exc:  # unique (tenant, order_item_id)
        if "uq" in str(exc) or "unique" in str(exc).lower():
            raise TrustError(
                "You have already reviewed this item", code="already_reviewed"
            ) from exc
        raise
    if status == "published":
        await refresh_ratings(
            db, tenant_id, product_id=line["product_id"], vendor_id=line["vendor_id"]
        )
    return {"id": str(review_id), "status": status, "flags": flags}


async def moderate_review(
    db: AsyncSession, tenant_id: str, review_id, *, decision: str, actor_id: str
) -> dict:
    row = (
        (
            await db.execute(
                text(
                    "SELECT product_id, vendor_id, status FROM reviews WHERE tenant_id = :t AND id = :i"
                ),
                {"t": tenant_id, "i": review_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    status = {"approve": "published", "reject": "rejected", "hide": "hidden"}[decision]
    await db.execute(
        text(
            "UPDATE reviews SET status = :s, moderated_by = :a, moderated_at = now(), updated_at = now() "
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"s": status, "a": actor_id, "t": tenant_id, "i": review_id},
    )
    await refresh_ratings(db, tenant_id, product_id=row["product_id"], vendor_id=row["vendor_id"])
    return {"id": str(review_id), "status": status}


async def reply_to_review(
    db: AsyncSession, tenant_id: str, review_id, *, vendor_id: str, reply: str
) -> dict:
    """A vendor may answer once — publicly, and through the same filter as any other message."""
    # Another vendor's (or tenant's) review does not exist here; a 409 would confirm that it does.
    existing = (
        (
            await db.execute(
                text(
                    "SELECT status, vendor_reply FROM reviews WHERE tenant_id = :t AND id = :i "
                    "AND vendor_id = CAST(:v AS uuid)"
                ),
                {"t": tenant_id, "i": review_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if existing is None:
        raise NotFound("Not found")
    if existing["status"] != "published" or existing["vendor_reply"] is not None:
        raise TrustError("This review cannot be answered", code="cannot_reply")
    cleaned, flags, _ = filters.redact(reply)
    await db.execute(
        text(
            """UPDATE reviews SET vendor_reply = :r, replied_at = now(), updated_at = now()
               WHERE tenant_id = :t AND id = :i AND vendor_id = CAST(:v AS uuid)"""
        ),
        {"r": cleaned, "t": tenant_id, "i": review_id, "v": vendor_id},
    )
    return {"id": str(review_id), "reply": cleaned, "flags": flags}


async def refresh_ratings(db: AsyncSession, tenant_id: str, *, product_id, vendor_id) -> None:
    """Published reviews only — a pending or rejected review never moves a star."""
    await db.execute(
        text(
            """UPDATE products p SET rating_avg = COALESCE(s.avg, 0), rating_count = COALESCE(s.n, 0)
               FROM (SELECT round(avg(rating)::numeric, 2) AS avg, count(*) AS n FROM reviews
                     WHERE tenant_id = :t AND product_id = :p AND status = 'published') s
               WHERE p.tenant_id = :t AND p.id = :p"""
        ),
        {"t": tenant_id, "p": product_id},
    )
    await db.execute(
        text(
            """UPDATE vendors v SET rating_avg = COALESCE(s.avg, 0), rating_count = COALESCE(s.n, 0)
               FROM (SELECT round(avg(rating)::numeric, 2) AS avg, count(*) AS n FROM reviews
                     WHERE tenant_id = :t AND vendor_id = :v AND status = 'published') s
               WHERE v.tenant_id = :t AND v.id = :v"""
        ),
        {"t": tenant_id, "v": vendor_id},
    )


# ------------------------------------------------------------------------------------- disputes
async def open_dispute(
    db: AsyncSession,
    tenant_id: str,
    *,
    sub_order_id,
    user_id: str,
    reason: str,
    detail: str | None,
) -> dict:
    sub = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.vendor_id, s.order_id, s.total, s.status, o.user_id,
                              ts.dispute_response_hours
                       FROM sub_orders s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       JOIN tenant_settings ts ON ts.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.id = :s"""
                ),
                {"t": tenant_id, "s": sub_order_id},
            )
        )
        .mappings()
        .first()
    )
    if sub is None or str(sub["user_id"]) != str(user_id):
        raise NotFound("Not found")
    if sub["status"] in ("pending_payment", "cancelled"):
        raise TrustError("There is nothing to dispute on this shipment", code="not_disputable")
    open_already = (
        await db.execute(
            text(
                """SELECT id FROM disputes WHERE tenant_id = :t AND sub_order_id = :s
                   AND status NOT IN ('resolved_buyer','resolved_vendor','withdrawn','cancelled')"""
            ),
            {"t": tenant_id, "s": sub_order_id},
        )
    ).scalar()
    if open_already:
        raise TrustError("A dispute is already open for this shipment", code="already_open")
    number = (
        await db.execute(
            text(
                """UPDATE tenant_settings SET next_dispute_number = next_dispute_number + 1
                   WHERE tenant_id = :t RETURNING 'DSP-' || (next_dispute_number - 1)"""
            ),
            {"t": tenant_id},
        )
    ).scalar()
    dispute_id = (
        await db.execute(
            text(
                """INSERT INTO disputes (tenant_id, vendor_id, order_id, sub_order_id, user_id, number,
                       reason, detail, amount, due_at)
                   VALUES (:t, :v, :o, :s, :u, :n, :r, :d, :amt,
                           now() + make_interval(hours => :h)) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": sub["vendor_id"],
                "o": sub["order_id"],
                "s": sub_order_id,
                "u": user_id,
                "n": number,
                "r": reason,
                "d": detail,
                "amt": money(sub["total"]),
                "h": sub["dispute_response_hours"],
            },
        )
    ).scalar()
    # The money stops moving while the question is open (ADR 0009).
    hold_id = (
        await db.execute(
            text(
                """INSERT INTO payout_holds (tenant_id, vendor_id, reason, created_by)
                   VALUES (:t, :v, 'dispute', 'system') RETURNING id"""
            ),
            {"t": tenant_id, "v": sub["vendor_id"]},
        )
    ).scalar()
    await db.execute(
        text("UPDATE disputes SET hold_id = :h WHERE tenant_id = :t AND id = :i"),
        {"h": hold_id, "t": tenant_id, "i": dispute_id},
    )
    return {
        "id": str(dispute_id),
        "number": number,
        "status": "open",
        "amount": money(sub["total"]),
    }


async def add_dispute_message(
    db: AsyncSession,
    tenant_id: str,
    dispute_id,
    *,
    author_kind: str,
    author_id: str,
    body: str,
    vendor_id: str | None = None,
) -> dict:
    dispute = await load_dispute(db, tenant_id, dispute_id, vendor_id=vendor_id)
    if dispute["status"] in ("resolved_buyer", "resolved_vendor", "withdrawn", "cancelled"):
        raise TrustError("This dispute is closed", code="closed")
    cleaned, flags, _ = filters.redact(body)
    await db.execute(
        text(
            """INSERT INTO dispute_messages (tenant_id, dispute_id, author_kind, author_id, body)
               VALUES (:t, :d, :k, :a, :b)"""
        ),
        {"t": tenant_id, "d": dispute_id, "k": author_kind, "a": author_id, "b": cleaned},
    )
    next_status = {"buyer": "needs_vendor", "vendor": "under_review", "staff": "under_review"}[
        author_kind
    ]
    await db.execute(
        text(
            "UPDATE disputes SET status = :s, updated_at = now() WHERE tenant_id = :t AND id = :i"
        ),
        {"s": next_status, "t": tenant_id, "i": dispute_id},
    )
    return {"status": next_status, "flags": flags}


async def load_dispute(
    db: AsyncSession, tenant_id: str, dispute_id, *, vendor_id=None, user_id=None
):
    row = (
        (
            await db.execute(
                text(
                    """SELECT d.*, s.number AS shipment_number, o.number AS order_number
                       FROM disputes d
                       JOIN sub_orders s ON s.id = d.sub_order_id AND s.tenant_id = d.tenant_id
                       JOIN orders o ON o.id = d.order_id AND o.tenant_id = d.tenant_id
                       WHERE d.tenant_id = :t AND d.id = :i
                         AND (CAST(:v AS uuid) IS NULL OR d.vendor_id = CAST(:v AS uuid))
                         AND (CAST(:u AS uuid) IS NULL OR d.user_id = CAST(:u AS uuid))"""
                ),
                {"t": tenant_id, "i": dispute_id, "v": vendor_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return row


async def resolve_dispute(
    db: AsyncSession, tenant_id: str, dispute_id, *, in_favour_of: str, note: str, actor_id: str
) -> dict:
    """Whoever wins, the hold comes off: a dispute may cost a vendor money, never their liquidity
    forever."""
    dispute = await load_dispute(db, tenant_id, dispute_id)
    if dispute["status"] in ("resolved_buyer", "resolved_vendor", "withdrawn", "cancelled"):
        raise TrustError("This dispute is already closed", code="closed")
    status = "resolved_buyer" if in_favour_of == "buyer" else "resolved_vendor"
    await db.execute(
        text(
            """UPDATE disputes SET status = :s, resolution = :n, resolved_by = :a, resolved_at = now(),
                      updated_at = now() WHERE tenant_id = :t AND id = :i"""
        ),
        {"s": status, "n": note, "a": actor_id, "t": tenant_id, "i": dispute_id},
    )
    if dispute["hold_id"]:
        await db.execute(
            text(
                "UPDATE payout_holds SET released_at = now(), released_by = :a "
                "WHERE tenant_id = :t AND id = :h AND released_at IS NULL"
            ),
            {"a": actor_id, "t": tenant_id, "h": dispute["hold_id"]},
        )
    return {"id": str(dispute_id), "status": status}


async def escalate_overdue_disputes(db: AsyncSession, tenant_id: str) -> int:
    res = await db.execute(
        text(
            """UPDATE disputes SET status = 'under_review', updated_at = now()
               WHERE tenant_id = :t AND status IN ('open','needs_vendor')
                 AND due_at IS NOT NULL AND due_at < now()"""
        ),
        {"t": tenant_id},
    )
    return res.rowcount


# ------------------------------------------------------------------------------------ messaging
@dataclass
class Message:
    id: str
    body: str
    flags: list[str]
    redacted: bool


async def ensure_conversation(
    db: AsyncSession, tenant_id: str, *, vendor_id: str, user_id: str, sub_order_id=None
) -> str:
    enabled = (
        await db.execute(
            text("SELECT messaging_enabled FROM tenant_settings WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
    ).scalar()
    if not enabled:
        raise TrustError("Messaging is turned off in this store", code="messaging_disabled")
    row = (
        await db.execute(
            text(
                """SELECT id, status FROM conversations
                   WHERE tenant_id = :t AND vendor_id = CAST(:v AS uuid) AND user_id = CAST(:u AS uuid)
                     AND sub_order_id IS NOT DISTINCT FROM CAST(:s AS uuid)"""
            ),
            {"t": tenant_id, "v": vendor_id, "u": user_id, "s": sub_order_id},
        )
    ).first()
    if row is not None:
        if row.status == "blocked":
            raise TrustError("This conversation is closed", code="conversation_blocked")
        return str(row.id)
    return str(
        (
            await db.execute(
                text(
                    """INSERT INTO conversations (tenant_id, vendor_id, user_id, sub_order_id)
                       VALUES (:t, :v, :u, :s) RETURNING id"""
                ),
                {"t": tenant_id, "v": vendor_id, "u": user_id, "s": sub_order_id},
            )
        ).scalar()
    )


async def send_message(
    db: AsyncSession,
    tenant_id: str,
    *,
    conversation_id,
    sender_kind: str,
    sender_id: str | None,
    body: str,
    vendor_id: str | None = None,
    user_id: str | None = None,
) -> Message:
    convo = (
        (
            await db.execute(
                text(
                    """SELECT * FROM conversations WHERE tenant_id = :t AND id = :i
                       AND (CAST(:v AS uuid) IS NULL OR vendor_id = CAST(:v AS uuid))
                       AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))"""
                ),
                {"t": tenant_id, "i": conversation_id, "v": vendor_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    if convo is None:
        raise NotFound("Not found")
    if convo["status"] != "open":
        raise TrustError("This conversation is closed", code="conversation_closed")
    cleaned, flags, redacted = filters.redact(body)
    message_id = (
        await db.execute(
            text(
                """INSERT INTO messages (tenant_id, vendor_id, conversation_id, sender_kind, sender_id,
                       body, original_hash, redacted, flags)
                   VALUES (:t, :v, :c, :k, :s, :b, :h, :r, CAST(:f AS text[])) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": convo["vendor_id"],
                "c": conversation_id,
                "k": sender_kind,
                "s": sender_id if sender_kind != "staff" else None,
                "b": cleaned,
                "h": filters.fingerprint(body) if redacted else None,
                "r": redacted,
                "f": flags,
            },
        )
    ).scalar()
    unread = "vendor_unread" if sender_kind == "buyer" else "buyer_unread"
    await db.execute(
        text(
            f"UPDATE conversations SET {unread} = {unread} + 1, last_message_at = now() "  # noqa: S608 - fixed column name
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"t": tenant_id, "i": conversation_id},
    )
    return Message(id=str(message_id), body=cleaned, flags=flags, redacted=redacted)


async def mark_read(db: AsyncSession, tenant_id: str, conversation_id, *, side: str) -> None:
    column = "buyer_unread" if side == "buyer" else "vendor_unread"
    await db.execute(
        text(
            f"UPDATE conversations SET {column} = 0 WHERE tenant_id = :t AND id = :i"  # noqa: S608 - fixed column name
        ),
        {"t": tenant_id, "i": conversation_id},
    )


# ------------------------------------------------------------------------------ vendor scoring
async def score_vendor(
    db: AsyncSession, tenant_id: str, vendor_id: str, *, window_days: int = 30
) -> dict:
    """One number from five facts, so a vendor can argue with the facts rather than the number."""
    stats = (
        (
            await db.execute(
                text(
                    """SELECT
                         count(*) FILTER (WHERE s.created_at > now() - make_interval(days => :d)) AS orders,
                         count(*) FILTER (WHERE s.status = 'cancelled'
                                            AND s.created_at > now() - make_interval(days => :d)) AS cancelled,
                         count(*) FILTER (WHERE s.status = 'returned'
                                            AND s.created_at > now() - make_interval(days => :d)) AS returned,
                         count(sh.id) FILTER (WHERE sh.delivered_at IS NOT NULL
                                            AND s.created_at > now() - make_interval(days => :d)) AS delivered,
                         count(sh.id) FILTER (WHERE sh.delivered_at IS NOT NULL
                                            AND sh.delivered_at <= sh.booked_at + interval '5 days'
                                            AND s.created_at > now() - make_interval(days => :d)) AS on_time
                       FROM sub_orders s
                       LEFT JOIN shipments sh ON sh.sub_order_id = s.id AND sh.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.vendor_id = CAST(:v AS uuid)"""
                ),
                {"t": tenant_id, "v": vendor_id, "d": window_days},
            )
        )
        .mappings()
        .one()
    )
    disputes = (
        await db.execute(
            text(
                """SELECT count(*) FROM disputes WHERE tenant_id = :t AND vendor_id = CAST(:v AS uuid)
                   AND created_at > now() - make_interval(days => :d)"""
            ),
            {"t": tenant_id, "v": vendor_id, "d": window_days},
        )
    ).scalar()
    rating = (
        await db.execute(
            text("SELECT rating_avg FROM vendors WHERE tenant_id = :t AND id = CAST(:v AS uuid)"),
            {"t": tenant_id, "v": vendor_id},
        )
    ).scalar()
    orders = int(stats["orders"] or 0)
    delivered = int(stats["delivered"] or 0)

    def ratio(n: int, d: int) -> Decimal | None:
        return (Decimal(n) / Decimal(d)).quantize(Decimal("0.0001")) if d else None

    on_time_rate = ratio(int(stats["on_time"] or 0), delivered)
    cancel_rate = ratio(int(stats["cancelled"] or 0), orders)
    return_rate = ratio(int(stats["returned"] or 0), orders)
    dispute_rate = ratio(int(disputes or 0), orders)

    score = Decimal("100")
    score -= (Decimal("1") - (on_time_rate if on_time_rate is not None else Decimal("1"))) * 30
    score -= (cancel_rate or Decimal("0")) * 30
    score -= (return_rate or Decimal("0")) * 20
    score -= (dispute_rate or Decimal("0")) * 40
    if rating and Decimal(str(rating)) > 0:
        score -= (Decimal("5") - Decimal(str(rating))) * 4
    score = max(Decimal("0"), min(Decimal("100"), score)).quantize(Decimal("0.01"))
    band = next(name for name, floor in BANDS if score >= floor)
    await db.execute(
        text(
            """INSERT INTO vendor_scores (tenant_id, vendor_id, window_days, orders, on_time_rate,
                   cancel_rate, return_rate, dispute_rate, rating_avg, score, band)
               VALUES (:t, :v, :w, :o, :ot, :c, :r, :dsp, :rate, :s, :b)
               ON CONFLICT (tenant_id, vendor_id, window_days) DO UPDATE
               SET orders = EXCLUDED.orders, on_time_rate = EXCLUDED.on_time_rate,
                   cancel_rate = EXCLUDED.cancel_rate, return_rate = EXCLUDED.return_rate,
                   dispute_rate = EXCLUDED.dispute_rate, rating_avg = EXCLUDED.rating_avg,
                   score = EXCLUDED.score, band = EXCLUDED.band, computed_at = now()"""
        ),
        {
            "t": tenant_id,
            "v": vendor_id,
            "w": window_days,
            "o": orders,
            "ot": on_time_rate,
            "c": cancel_rate,
            "r": return_rate,
            "dsp": dispute_rate,
            "rate": rating,
            "s": score,
            "b": band,
        },
    )
    return {
        "vendor_id": str(vendor_id),
        "window_days": window_days,
        "orders": orders,
        "on_time_rate": on_time_rate,
        "cancel_rate": cancel_rate,
        "return_rate": return_rate,
        "dispute_rate": dispute_rate,
        "rating_avg": rating,
        "score": score,
        "band": band,
    }


async def score_all(db: AsyncSession, tenant_id: str, *, window_days: int = 30) -> dict:
    vendors = (
        (
            await db.execute(
                text("SELECT id FROM vendors WHERE tenant_id = :t AND status = 'approved'"),
                {"t": tenant_id},
            )
        )
        .scalars()
        .all()
    )
    bands = {"good": 0, "watch": 0, "risk": 0}
    for vendor_id in vendors:
        result = await score_vendor(db, tenant_id, str(vendor_id), window_days=window_days)
        bands[result["band"]] += 1
    return {"scored": len(vendors), **bands}
