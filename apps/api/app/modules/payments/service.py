"""Payment capture and settlement (ADR 0006, architecture §7).

Three rules this module exists to enforce:

1. **Verify first.** A callback is a nudge, never evidence. Nothing is marked paid until the
   gateway's own verify/validation call, made by us with the tenant's credentials, says so.
2. **Idempotent.** Providers retry. `payment_events` is unique on `(tenant, provider, event_id)`
   and a replay settles nothing twice; the partial unique index `uq_payment_paid_per_order`
   makes a second paid payment for one order impossible even under a race.
3. **Amounts are checked.** A verified payment whose amount is not exactly the order total never
   confirms the order — it is failed as `amount_mismatch` for staff to look at.

Settlement is the single place where prepaid stock moves from reserved to sold, so the money and
the stock ledger can never disagree.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Encryptor
from app.core.errors import AppError
from app.modules.checkout.pricing import money

PREPAID = ("bkash", "sslcommerz")
OPEN_STATUSES = ["initiated", "pending"]
PAID_STATUSES = ["paid", "refunded", "partially_refunded"]


class PaymentError(AppError):
    status = 409
    code = "payment_conflict"


@dataclass
class Account:
    provider: str
    mode: str
    credentials: dict
    status: str


def _context(tenant_id: str, provider: str) -> str:
    return f"payment_account:{tenant_id}:{provider}"


def encrypt_credentials(settings, tenant_id: str, provider: str, credentials: dict) -> str:
    return Encryptor(settings.data_encryption_key).encrypt(
        credentials, context=_context(tenant_id, provider)
    )


async def load_account(db: AsyncSession, settings, tenant_id: str, provider: str) -> Account:
    row = (
        (
            await db.execute(
                text(
                    """SELECT provider, mode, credentials_ciphertext, status FROM payment_accounts
                       WHERE tenant_id = :t AND provider = :p"""
                ),
                {"t": tenant_id, "p": provider},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise PaymentError("This payment method is not configured", code="gateway_unconfigured")
    if row["status"] == "disabled":
        raise PaymentError("This payment method is unavailable", code="gateway_disabled")
    credentials = Encryptor(settings.data_encryption_key).decrypt(
        row["credentials_ciphertext"], context=_context(tenant_id, provider)
    )
    return Account(
        provider=row["provider"], mode=row["mode"], credentials=credentials, status=row["status"]
    )


async def _order_for_payment(db: AsyncSession, tenant_id: str, *, order_id=None, number=None):
    row = (
        (
            await db.execute(
                text(
                    """SELECT o.id, o.number, o.grand_total, o.payment_method, o.payment_due_at,
                              o.contact_phone, o.user_id
                       FROM orders o
                       WHERE o.tenant_id = :t AND (CAST(:o AS uuid) IS NULL OR o.id = :o)
                         AND (CAST(:n AS text) IS NULL OR o.number = :n)
                       FOR UPDATE"""
                ),
                {"t": tenant_id, "o": order_id, "n": number},
            )
        )
        .mappings()
        .first()
    )
    return row


async def _paid_payment(db: AsyncSession, tenant_id: str, order_id) -> str | None:
    """An existing *gateway* capture for this order. Store credit is tender, not a capture."""
    return (
        await db.execute(
            text(
                "SELECT id FROM payments WHERE tenant_id = :t AND order_id = :o "
                "AND provider <> 'store_credit' AND status = ANY(CAST(:st AS text[]))"
            ),
            {"t": tenant_id, "o": order_id, "st": PAID_STATUSES},
        )
    ).scalar()


async def amount_due(db: AsyncSession, tenant_id: str, order_id, grand_total) -> Decimal:
    """What the buyer still owes: the order total minus every payment already standing on it
    (store credit spent at checkout counts), net of anything refunded back."""
    settled = (
        await db.execute(
            text(
                """SELECT coalesce(sum(amount - refunded_amount), 0) FROM payments
                   WHERE tenant_id = :t AND order_id = :o AND status = ANY(CAST(:st AS text[]))"""
            ),
            {"t": tenant_id, "o": order_id, "st": PAID_STATUSES},
        )
    ).scalar()
    return money(Decimal(str(grand_total)) - Decimal(str(settled)))


# ------------------------------------------------------------------------------------ starting
async def start_payment(
    db: AsyncSession,
    tenant_id: str,
    *,
    order_number: str,
    user_id: str | None,
    gateway,
    account: Account,
    return_url: str,
    callback_url: str,
) -> dict:
    """Open (or re-open) a gateway session for an unpaid order. Existing open attempts are cancelled."""
    order = await _order_for_payment(db, tenant_id, number=order_number)
    if order is None or (user_id is not None and str(order["user_id"]) != str(user_id)):
        raise AppError("Order not found", status=404, code="not_found")
    if order["payment_method"] == "cod":
        raise PaymentError("This order is cash on delivery", code="not_prepaid")
    if await _paid_payment(db, tenant_id, order["id"]):
        raise PaymentError("This order is already paid", code="already_paid")
    due = await amount_due(db, tenant_id, order["id"], order["grand_total"])
    if due <= 0:
        raise PaymentError("This order is already paid", code="already_paid")
    pending = (
        await db.execute(
            text(
                "SELECT count(*) FROM sub_orders WHERE tenant_id = :t AND order_id = :o "
                "AND status = 'pending_payment'"
            ),
            {"t": tenant_id, "o": order["id"]},
        )
    ).scalar()
    if not pending:
        raise PaymentError("This order is no longer awaiting payment", code="not_payable")
    if order["payment_due_at"] and order["payment_due_at"] < datetime.now(UTC):
        raise PaymentError("The payment window for this order has closed", code="payment_expired")

    await db.execute(
        text(
            "UPDATE payments SET status = 'cancelled', failure_reason = 'superseded', updated_at = now() "
            "WHERE tenant_id = :t AND order_id = :o AND status = ANY(CAST(:st AS text[]))"
        ),
        {"t": tenant_id, "o": order["id"], "st": OPEN_STATUSES},
    )
    attempt = (
        await db.execute(
            text(
                "SELECT coalesce(max(attempt), 0) + 1 FROM payments WHERE tenant_id = :t AND order_id = :o"
            ),
            {"t": tenant_id, "o": order["id"]},
        )
    ).scalar()
    created = await gateway.create(
        credentials=account.credentials,
        amount=due,
        order_number=order["number"],
        return_url=return_url,
        callback_url=callback_url,
        buyer_phone=order["contact_phone"],
    )
    payment_id = (
        await db.execute(
            text(
                """INSERT INTO payments (tenant_id, order_id, provider, amount, status, provider_ref, attempt)
                   VALUES (:t, :o, :p, :a, 'pending', :r, :n) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "o": order["id"],
                "p": account.provider,
                "a": due,
                "r": created.provider_ref,
                "n": attempt,
            },
        )
    ).scalar()
    return {
        "payment_id": str(payment_id),
        "provider": account.provider,
        "order_number": order["number"],
        "amount": due,
        "redirect_url": created.redirect_url,
        "status": "pending",
    }


# ---------------------------------------------------------------------------------- settlement
async def settle(
    db: AsyncSession, tenant_id: str, *, payment_id, gateway, account: Account, app_state=None
) -> dict:
    """Verify with the provider and, only then, pay the order. Safe to call repeatedly."""
    payment = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.order_id, p.provider, p.amount, p.status, p.provider_ref, o.number,
                              o.grand_total
                       FROM payments p JOIN orders o ON o.id = p.order_id AND o.tenant_id = p.tenant_id
                       WHERE p.tenant_id = :t AND p.id = :p FOR UPDATE OF p"""
                ),
                {"t": tenant_id, "p": payment_id},
            )
        )
        .mappings()
        .first()
    )
    if payment is None:
        raise AppError("Payment not found", status=404, code="not_found")
    if payment["status"] in PAID_STATUSES:
        return {"status": payment["status"], "order_number": payment["number"], "settled": False}
    if payment["provider_ref"] is None:
        return {"status": payment["status"], "order_number": payment["number"], "settled": False}

    # Network call inside the request transaction: gateways answer in <20s and the row lock is
    # narrow (this payment only). The order row is locked *after* verification, never across it.
    verified = await gateway.verify(
        credentials=account.credentials, provider_ref=payment["provider_ref"]
    )
    if verified.status != "paid":
        await db.execute(
            text(
                """UPDATE payments SET status = :s, failure_reason = :r, updated_at = now()
                   WHERE tenant_id = :t AND id = :p"""
            ),
            {
                "s": verified.status,
                "r": (verified.failure_reason or "")[:500] or None,
                "t": tenant_id,
                "p": payment["id"],
            },
        )
        return {
            "status": verified.status,
            "order_number": payment["number"],
            "settled": False,
        }
    if verified.amount is None or money(verified.amount) != money(payment["amount"]):
        await db.execute(
            text(
                """UPDATE payments SET status = 'failed', failure_reason = 'amount_mismatch',
                          payer_ref = :pr, updated_at = now() WHERE tenant_id = :t AND id = :p"""
            ),
            {"pr": verified.payer_ref, "t": tenant_id, "p": payment["id"]},
        )
        return {"status": "failed", "order_number": payment["number"], "settled": False}

    order = await _order_for_payment(db, tenant_id, order_id=payment["order_id"])
    other = await _paid_payment(db, tenant_id, payment["order_id"])
    if other is not None:
        await db.execute(
            text(
                "UPDATE payments SET status = 'cancelled', failure_reason = 'already_paid', "
                "updated_at = now() WHERE tenant_id = :t AND id = :p"
            ),
            {"t": tenant_id, "p": payment["id"]},
        )
        return {"status": "duplicate", "order_number": order["number"], "settled": False}

    await db.execute(
        text(
            """UPDATE payments SET status = 'paid', verified_at = now(), payer_ref = :pr, fee = :fee,
                      failure_reason = NULL, updated_at = now()
               WHERE tenant_id = :t AND id = :p"""
        ),
        {
            "pr": verified.payer_ref,
            "fee": money(verified.fee or Decimal("0")),
            "t": tenant_id,
            "p": payment["id"],
        },
    )
    due = await amount_due(db, tenant_id, payment["order_id"], order["grand_total"])
    confirmed = 0
    if due <= 0:
        confirmed = await confirm_paid_order(
            db, tenant_id, payment["order_id"], ref=order["number"], app_state=app_state
        )
    return {
        "status": "paid",
        "order_number": order["number"],
        "settled": True,
        "confirmed_sub_orders": confirmed,
        "amount_due": due,
    }


async def confirm_paid_order(
    db: AsyncSession, tenant_id: str, order_id, *, ref: str, app_state=None
) -> int:
    """pending_payment -> confirmed, reservations consumed into the stock ledger, money booked.
    Idempotent: the ledger refuses a second posting of the same capture."""
    subs = (
        await db.execute(
            text(
                """UPDATE sub_orders SET status = 'confirmed', updated_at = now()
                   WHERE tenant_id = :t AND order_id = :o AND status = 'pending_payment' RETURNING id"""
            ),
            {"t": tenant_id, "o": order_id},
        )
    ).all()
    await consume_reservations(db, tenant_id, order_id, ref=ref)
    await db.execute(
        text("UPDATE orders SET payment_due_at = NULL WHERE tenant_id = :t AND id = :o"),
        {"t": tenant_id, "o": order_id},
    )
    from app.modules.ledger import service as ledger

    await ledger.post_capture(db, tenant_id, order_id)
    await _announce_paid_order(db, tenant_id, order_id, app_state)
    return len(subs)


async def _announce_paid_order(db: AsyncSession, tenant_id: str, order_id, app_state) -> None:
    """Tell the buyer, and tell the tenant's own analytics — both exactly once per order."""
    from app.modules.notifications import analytics

    order = (
        (
            await db.execute(
                text(
                    """SELECT o.id, o.number, o.grand_total, o.user_id, o.contact_email, o.contact_phone
                       FROM orders o WHERE o.tenant_id = :t AND o.id = :o"""
                ),
                {"t": tenant_id, "o": order_id},
            )
        )
        .mappings()
        .first()
    )
    if order is None:
        return
    items = (
        (
            await db.execute(
                text(
                    """SELECT i.title_snapshot AS item_name, i.sku_snapshot AS item_id, i.qty AS quantity,
                              i.unit_price AS price
                       FROM order_items i
                       JOIN sub_orders s ON s.id = i.sub_order_id AND s.tenant_id = i.tenant_id
                       WHERE i.tenant_id = :t AND s.order_id = :o"""
                ),
                {"t": tenant_id, "o": order_id},
            )
        )
        .mappings()
        .all()
    )
    await analytics.queue_purchase(
        db,
        tenant_id,
        order_id=order_id,
        number=order["number"],
        value=order["grand_total"],
        items=[{k: str(v) for k, v in dict(i).items()} for i in items],
        email=order["contact_email"],
        phone=order["contact_phone"],
    )
    if app_state is not None:
        from app.modules.notifications import service as notifications

        await notifications.on_payment_paid(db, app_state, tenant_id, order_id=order_id)


async def consume_reservations(db: AsyncSession, tenant_id: str, order_id, *, ref: str) -> int:
    """Reserved -> sold: on_hand and reserved both drop, one append-only movement per variant."""
    rows = (
        await db.execute(
            text(
                """UPDATE stock_reservations SET consumed_at = now()
                   WHERE tenant_id = :t AND order_id = :o AND released_at IS NULL AND consumed_at IS NULL
                   RETURNING vendor_id, variant_id, qty"""
            ),
            {"t": tenant_id, "o": order_id},
        )
    ).all()
    for vendor_id, variant_id, qty in sorted(rows, key=lambda r: str(r[1])):
        balance = (
            await db.execute(
                text(
                    """UPDATE product_variants
                       SET stock_on_hand = stock_on_hand - :q, stock_reserved = stock_reserved - :q,
                           updated_at = now()
                       WHERE tenant_id = :t AND id = :v RETURNING stock_on_hand"""
                ),
                {"q": qty, "t": tenant_id, "v": variant_id},
            )
        ).scalar()
        await db.execute(
            text(
                """INSERT INTO inventory_movements (tenant_id, vendor_id, variant_id, delta, balance_after,
                       reason, ref, actor_id)
                   VALUES (:t, :vend, :v, :d, :b, 'order', :ref, 'system')"""
            ),
            {
                "t": tenant_id,
                "vend": vendor_id,
                "v": variant_id,
                "d": -qty,
                "b": balance,
                "ref": ref,
            },
        )
    return len(rows)


# ----------------------------------------------------------------------------- cash on delivery
async def register_cod(db: AsyncSession, tenant_id: str, order_id, *, amount: Decimal) -> None:
    """A COD order owes money from the moment it is placed: one receivable per shipment."""
    await db.execute(
        text(
            """INSERT INTO payments (tenant_id, order_id, provider, amount, status)
               VALUES (:t, :o, 'cod', :a, 'pending')"""
        ),
        {"t": tenant_id, "o": order_id, "a": amount},
    )
    await db.execute(
        text(
            """INSERT INTO cod_receivables (tenant_id, vendor_id, order_id, sub_order_id, amount)
               SELECT s.tenant_id, s.vendor_id, s.order_id, s.id, s.total FROM sub_orders s
               WHERE s.tenant_id = :t AND s.order_id = :o
               ON CONFLICT (sub_order_id) DO NOTHING"""
        ),
        {"t": tenant_id, "o": order_id},
    )


async def cancel_cod_receivables(
    db: AsyncSession, tenant_id: str, sub_order_ids: list[str]
) -> None:
    if not sub_order_ids:
        return
    await db.execute(
        text(
            """UPDATE cod_receivables SET status = 'cancelled'
               WHERE tenant_id = :t AND sub_order_id = ANY(CAST(:s AS uuid[])) AND status = 'due'"""
        ),
        {"t": tenant_id, "s": sub_order_ids},
    )
    await db.execute(
        text(
            """UPDATE payments p SET status = 'cancelled', failure_reason = 'order_cancelled',
                      updated_at = now()
               WHERE p.tenant_id = :t AND p.provider = 'cod' AND p.status = 'pending'
                 AND NOT EXISTS (SELECT 1 FROM cod_receivables r
                                 WHERE r.tenant_id = p.tenant_id AND r.order_id = p.order_id
                                   AND r.status <> 'cancelled')
                 AND p.order_id IN (SELECT order_id FROM cod_receivables
                                    WHERE tenant_id = :t AND sub_order_id = ANY(CAST(:s AS uuid[])))"""
        ),
        {"t": tenant_id, "s": sub_order_ids},
    )


async def collect_cod(
    db: AsyncSession, tenant_id: str, sub_order_id, *, courier: str | None = None
) -> bool:
    """Courier delivered and holds the cash: the receivable is collected, not yet settled to us."""
    updated = (
        await db.execute(
            text(
                """UPDATE cod_receivables SET status = 'collected', collected_at = now(), courier = :c
                   WHERE tenant_id = :t AND sub_order_id = :s AND status = 'due' RETURNING order_id"""
            ),
            {"t": tenant_id, "s": sub_order_id, "c": courier},
        )
    ).first()
    if updated is None:
        return False
    await db.execute(
        text(
            """UPDATE payments p SET status = 'paid', verified_at = now(), updated_at = now()
               WHERE p.tenant_id = :t AND p.order_id = :o AND p.provider = 'cod' AND p.status = 'pending'
                 AND NOT EXISTS (SELECT 1 FROM cod_receivables r
                                 WHERE r.tenant_id = p.tenant_id AND r.order_id = p.order_id
                                   AND r.status = 'due')"""
        ),
        {"t": tenant_id, "o": updated.order_id},
    )
    # COD money exists from the moment the courier takes it, not when it reaches the bank.
    from app.modules.ledger import service as ledger

    await ledger.post_cod_delivery(db, tenant_id, sub_order_id, courier or "unknown")
    return True


# -------------------------------------------------------------------------------------- events
async def record_event(
    db: AsyncSession,
    tenant_id: str,
    *,
    provider: str,
    event_id: str,
    kind: str,
    payload: dict,
    payment_id=None,
) -> bool:
    """False when this exact provider event was already stored — the caller must then do nothing."""
    res = await db.execute(
        text(
            """INSERT INTO payment_events (tenant_id, provider, event_id, kind, payload, payment_id)
               VALUES (:t, :p, :e, :k, CAST(:d AS jsonb), :pay)
               ON CONFLICT (tenant_id, provider, event_id) DO NOTHING RETURNING id"""
        ),
        {
            "t": tenant_id,
            "p": provider,
            "e": event_id,
            "k": kind,
            "d": json.dumps(payload),
            "pay": payment_id,
        },
    )
    return res.first() is not None


async def payment_for_callback(
    db: AsyncSession, tenant_id: str, *, provider: str, provider_ref: str, order_number: str | None
):
    """Find the attempt a callback belongs to; SSLCommerz only learns its val_id at IPN time."""
    row = (
        await db.execute(
            text(
                "SELECT id FROM payments WHERE tenant_id = :t AND provider = :p AND provider_ref = :r"
            ),
            {"t": tenant_id, "p": provider, "r": provider_ref},
        )
    ).first()
    if row is not None:
        return row.id
    if not order_number:
        return None
    row = (
        await db.execute(
            text(
                """UPDATE payments SET provider_ref = :r, updated_at = now()
                    WHERE tenant_id = :t AND provider = :p AND status = ANY(CAST(:st AS text[]))
                      AND order_id = (SELECT id FROM orders WHERE tenant_id = :t AND number = :n)
                    RETURNING id"""
            ),
            {
                "r": provider_ref,
                "t": tenant_id,
                "p": provider,
                "n": order_number,
                "st": OPEN_STATUSES,
            },
        )
    ).first()
    return row.id if row else None


# ------------------------------------------------------------------------------ reconciliation
async def reconcile_pending(
    db: AsyncSession,
    settings,
    *,
    tenant_id: str,
    older_than_minutes: int = 10,
    overrides: dict | None = None,
    limit: int = 100,
    app_state=None,
) -> dict:
    """Ask the provider about every attempt that never came back.

    Buyers close tabs, IPNs get lost, networks fail mid-redirect. Without this, money taken by the
    gateway sits against an order that still looks unpaid. Nothing here trusts a stored status:
    each attempt is re-verified and settled through the same path a callback uses.
    """
    from app.modules.payments.gateways import build_gateway

    rows = (
        (
            await db.execute(
                text(
                    """SELECT p.id, p.provider FROM payments p
                       WHERE p.tenant_id = :t AND p.status = 'pending' AND p.provider <> 'cod'
                         AND p.provider_ref IS NOT NULL
                         AND p.updated_at < now() - make_interval(mins => :m)
                       ORDER BY p.created_at LIMIT :l"""
                ),
                {"t": tenant_id, "m": older_than_minutes, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    out = {"checked": 0, "paid": 0, "closed": 0, "unresolved": 0}
    accounts: dict[str, Account] = {}
    for row in rows:
        provider = row["provider"]
        try:
            if provider not in accounts:
                accounts[provider] = await load_account(db, settings, tenant_id, provider)
            account = accounts[provider]
            gateway = build_gateway(provider, account.mode, overrides)
            result = await settle(
                db,
                tenant_id,
                payment_id=row["id"],
                gateway=gateway,
                account=account,
                app_state=app_state,
            )
        except Exception:  # a provider outage must not abort the rest of the sweep
            out["unresolved"] += 1
            continue
        out["checked"] += 1
        if result["status"] == "paid":
            out["paid"] += 1
        elif result["status"] in ("failed", "cancelled", "duplicate"):
            out["closed"] += 1
        else:
            out["unresolved"] += 1
    return out


async def reconciliation_summary(db: AsyncSession, tenant_id: str, *, days: int = 7) -> dict:
    """What the tenant's own books should say, straight from the payment and COD tables."""
    gateway_rows = (
        (
            await db.execute(
                text(
                    """SELECT provider, status, count(*) AS n, coalesce(sum(amount), 0) AS amount,
                              coalesce(sum(fee), 0) AS fee
                       FROM payments WHERE tenant_id = :t AND created_at > now() - make_interval(days => :d)
                       GROUP BY provider, status ORDER BY provider, status"""
                ),
                {"t": tenant_id, "d": days},
            )
        )
        .mappings()
        .all()
    )
    cod_rows = (
        (
            await db.execute(
                text(
                    """SELECT status, count(*) AS n, coalesce(sum(amount), 0) AS amount
                       FROM cod_receivables WHERE tenant_id = :t
                         AND created_at > now() - make_interval(days => :d)
                       GROUP BY status ORDER BY status"""
                ),
                {"t": tenant_id, "d": days},
            )
        )
        .mappings()
        .all()
    )
    unmatched = (
        await db.execute(
            text(
                """SELECT count(*) FROM payment_events e
                   WHERE e.tenant_id = :t AND e.payment_id IS NULL
                     AND e.received_at > now() - make_interval(days => :d)"""
            ),
            {"t": tenant_id, "d": days},
        )
    ).scalar()
    return {
        "days": days,
        "gateways": [dict(r) for r in gateway_rows],
        "cod": [dict(r) for r in cod_rows],
        "events_without_payment": unmatched,
    }
