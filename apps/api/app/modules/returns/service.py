"""Returns, refunds and credit notes (ADR 0008).

The state machine is deliberately strict:

    requested → approved → pickup_booked → picked_up → received → qc_passed → refunded
                       ↘ rejected                              ↘ qc_failed → returned_to_buyer

Money only moves at the end, and only once: `uq_refund_open_per_return` makes a second live refund
for one return impossible, and refunding to the original rail re-uses the payment the buyer
actually made. Restocking happens on QC pass, never on the buyer's say-so — a parcel that was
never inspected has not come back.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Encryptor
from app.core.errors import AppError, NotFound
from app.modules.checkout.pricing import ZERO, money
from app.modules.returns import credit as store_credit

VENDOR_FAULT = ("damaged", "wrong_item", "not_as_described", "missing_parts")
RETURNABLE_SUB_ORDER_STATUSES = ("delivered",)
TRANSITIONS = {
    "requested": ("approved", "rejected", "cancelled"),
    "approved": ("pickup_booked", "picked_up", "received", "cancelled"),
    "pickup_booked": ("picked_up", "received", "cancelled"),
    "picked_up": ("received",),
    "received": ("qc_passed", "qc_failed"),
    "qc_passed": ("refunded",),
    "qc_failed": ("returned_to_buyer",),
    "rejected": (),
    "refunded": (),
    "returned_to_buyer": (),
    "cancelled": (),
}


class ReturnError(AppError):
    status = 409
    code = "return_conflict"


@dataclass
class Window:
    days: int
    expires_at: datetime
    open: bool


def _context(tenant_id: str, return_id) -> str:
    return f"refund_target:{tenant_id}:{return_id}"


async def window_for(db: AsyncSession, tenant_id: str, sub_order_id) -> Window:
    """The shortest window that applies: the category's, else the tenant's, from delivery."""
    row = (
        (
            await db.execute(
                text(
                    """SELECT s.status, sh.delivered_at, ts.return_window_days AS tenant_days,
                              min(c.return_window_days) AS category_days
                       FROM sub_orders s
                       JOIN tenant_settings ts ON ts.tenant_id = s.tenant_id
                       LEFT JOIN shipments sh ON sh.sub_order_id = s.id AND sh.tenant_id = s.tenant_id
                       LEFT JOIN order_items i ON i.sub_order_id = s.id AND i.tenant_id = s.tenant_id
                       LEFT JOIN products p ON p.id = i.product_id AND p.tenant_id = s.tenant_id
                       LEFT JOIN categories c ON c.id = p.category_id AND c.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.id = :s
                       GROUP BY s.status, sh.delivered_at, ts.return_window_days"""
                ),
                {"t": tenant_id, "s": sub_order_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    days = row["category_days"] if row["category_days"] is not None else row["tenant_days"]
    start = row["delivered_at"] or datetime.now(UTC)
    expires = start + timedelta(days=days)
    return Window(days=days, expires_at=expires, open=datetime.now(UTC) <= expires)


async def create_request(
    db: AsyncSession,
    settings,
    tenant_id: str,
    *,
    sub_order_id,
    user_id: str,
    reason: str,
    items: list[dict],
    note: str | None,
    refund_method: str,
    refund_target: dict | None,
) -> dict:
    sub = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.vendor_id, s.order_id, s.status, o.user_id, o.number AS order_number,
                              o.payment_method
                       FROM sub_orders s JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
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
    if sub["status"] not in RETURNABLE_SUB_ORDER_STATUSES:
        raise ReturnError("Only delivered shipments can be returned", code="not_delivered")
    window = await window_for(db, tenant_id, sub_order_id)
    if not window.open:
        raise ReturnError(
            f"The {window.days}-day return window for this shipment has closed",
            code="window_closed",
        )
    open_return = (
        await db.execute(
            text(
                """SELECT id FROM return_requests WHERE tenant_id = :t AND sub_order_id = :s
                   AND status NOT IN ('rejected','cancelled','returned_to_buyer')"""
            ),
            {"t": tenant_id, "s": sub_order_id},
        )
    ).scalar()
    if open_return:
        raise ReturnError("A return for this shipment is already open", code="already_open")
    if refund_method == "store_credit":
        enabled = (
            await db.execute(
                text("SELECT store_credit_enabled FROM tenant_settings WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).scalar()
        if not enabled:
            raise ReturnError("Store credit is not available here", code="credit_disabled")
    if sub["payment_method"] == "cod" and refund_method == "original":
        raise ReturnError(
            "Choose where the refund should go: bKash, bank or store credit",
            code="refund_target_required",
        )

    lines, total, vat_total = await _price_items(db, tenant_id, sub_order_id, items)
    settings_row = (
        (
            await db.execute(
                text(
                    """UPDATE tenant_settings SET next_return_number = next_return_number + 1
                       WHERE tenant_id = :t RETURNING next_return_number - 1 AS n,
                             return_auto_approve_reasons, qc_sla_hours"""
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    number = f"RET-{settings_row['n']}"
    payer = "vendor" if reason in VENDOR_FAULT else "buyer"
    return_id = (
        await db.execute(
            text(
                """INSERT INTO return_requests (tenant_id, vendor_id, order_id, sub_order_id, user_id, number,
                       reason, reason_note, shipping_payer, refund_method, refund_total)
                   VALUES (:t, :v, :o, :s, :u, :n, :r, :note, :payer, :m, :amt) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": sub["vendor_id"],
                "o": sub["order_id"],
                "s": sub_order_id,
                "u": user_id,
                "n": number,
                "r": reason,
                "note": note,
                "payer": payer,
                "m": refund_method,
                "amt": total,
            },
        )
    ).scalar()
    if refund_target:
        await db.execute(
            text("UPDATE return_requests SET refund_target_ciphertext = :c WHERE id = :i"),
            {
                "c": Encryptor(settings.data_encryption_key).encrypt(
                    refund_target, context=_context(tenant_id, return_id)
                ),
                "i": return_id,
            },
        )
    for line in lines:
        await db.execute(
            text(
                """INSERT INTO return_items (tenant_id, return_id, order_item_id, variant_id, qty,
                       refund_amount, vat_amount)
                   VALUES (:t, :r, :oi, :v, :q, :a, :vat)"""
            ),
            {
                "t": tenant_id,
                "r": return_id,
                "oi": line["order_item_id"],
                "v": line["variant_id"],
                "q": line["qty"],
                "a": line["refund_amount"],
                "vat": line["vat_amount"],
            },
        )
    auto = reason in (settings_row["return_auto_approve_reasons"] or [])
    if auto:
        await _set_status(
            db, tenant_id, return_id, "approved", decided_by="auto", note="auto-approved by rule"
        )
    return {
        "id": str(return_id),
        "number": number,
        "status": "approved" if auto else "requested",
        "refund_total": total,
        "vat_total": vat_total,
        "shipping_payer": payer,
        "window_expires_at": window.expires_at,
    }


async def _price_items(db: AsyncSession, tenant_id: str, sub_order_id, items: list[dict]):
    """Refund value comes from the order line, never from the buyer's request.

    Under exclusive VAT pricing the buyer paid the line *plus* VAT, so that is what comes back;
    under inclusive pricing the VAT is already inside the line total."""
    lines, total, vat_total = [], ZERO, ZERO
    for item in items:
        row = (
            (
                await db.execute(
                    text(
                        """SELECT i.id, i.variant_id, i.qty, i.line_total, i.vat_amount, o.vat_pricing
                           FROM order_items i
                           JOIN sub_orders s ON s.id = i.sub_order_id AND s.tenant_id = i.tenant_id
                           JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                           WHERE i.tenant_id = :t AND i.sub_order_id = :s AND i.id = :i"""
                    ),
                    {"t": tenant_id, "s": sub_order_id, "i": item["order_item_id"]},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Item not found on this shipment")
        qty = int(item["qty"])
        if qty < 1 or qty > row["qty"]:
            raise ReturnError("You cannot return more than you bought", code="bad_quantity")
        share = Decimal(qty) / Decimal(row["qty"])
        paid = Decimal(str(row["line_total"]))
        if row["vat_pricing"] == "exclusive":
            paid += Decimal(str(row["vat_amount"]))
        amount = money(paid * share)
        vat = money(Decimal(str(row["vat_amount"])) * share)
        total += amount
        vat_total += vat
        lines.append(
            {
                "order_item_id": row["id"],
                "variant_id": row["variant_id"],
                "qty": qty,
                "refund_amount": amount,
                "vat_amount": vat,
            }
        )
    if not lines:
        raise ReturnError("Choose at least one item to return", code="no_items")
    return lines, money(total), money(vat_total)


async def load(db: AsyncSession, tenant_id: str, return_id, *, vendor_id=None, user_id=None):
    row = (
        (
            await db.execute(
                text(
                    """SELECT r.*, o.number AS order_number, o.payment_method, s.number AS shipment_number
                       FROM return_requests r
                       JOIN orders o ON o.id = r.order_id AND o.tenant_id = r.tenant_id
                       JOIN sub_orders s ON s.id = r.sub_order_id AND s.tenant_id = r.tenant_id
                       WHERE r.tenant_id = :t AND r.id = :i
                         AND (CAST(:v AS uuid) IS NULL OR r.vendor_id = CAST(:v AS uuid))
                         AND (CAST(:u AS uuid) IS NULL OR r.user_id = CAST(:u AS uuid))"""
                ),
                {"t": tenant_id, "i": return_id, "v": vendor_id, "u": user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return row


async def _set_status(
    db: AsyncSession,
    tenant_id: str,
    return_id,
    status: str,
    *,
    decided_by: str | None = None,
    note: str | None = None,
    qc_by: str | None = None,
    qc_due_hours: int | None = None,
) -> None:
    sets = ["status = :s", "updated_at = now()"]
    params = {"s": status, "t": tenant_id, "i": return_id}
    if decided_by is not None:
        sets += ["decided_by = :by", "decided_at = now()", "decision_note = :note"]
        params |= {"by": decided_by, "note": note}
    if qc_by is not None:
        sets += ["qc_by = :qby", "qc_at = now()", "qc_note = :qnote"]
        params |= {"qby": qc_by, "qnote": note}
    if qc_due_hours is not None:
        sets.append("qc_due_at = now() + make_interval(hours => :h)")
        params["h"] = qc_due_hours
    await db.execute(
        text(
            "UPDATE return_requests SET " + ", ".join(sets) + " WHERE tenant_id = :t AND id = :i"  # noqa: S608 - fixed column names
        ),
        params,
    )


def _check(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, ()):
        raise ReturnError(f"A {current} return cannot become {target}", code="bad_transition")


async def decide(
    db: AsyncSession,
    tenant_id: str,
    return_id,
    *,
    approve: bool,
    actor_id: str,
    note: str | None,
    vendor_id=None,
) -> dict:
    ret = await load(db, tenant_id, return_id, vendor_id=vendor_id)
    target = "approved" if approve else "rejected"
    _check(ret["status"], target)
    sla = (
        await db.execute(
            text("SELECT qc_sla_hours FROM tenant_settings WHERE tenant_id = :t"), {"t": tenant_id}
        )
    ).scalar()
    await _set_status(
        db,
        tenant_id,
        return_id,
        target,
        decided_by=actor_id,
        note=note,
        qc_due_hours=sla if approve else None,
    )
    return {"id": str(return_id), "status": target}


async def book_reverse_pickup(
    db: AsyncSession,
    settings,
    tenant_id: str,
    return_id,
    *,
    actor_id: str,
    courier: str | None,
    vendor_id=None,
    overrides: dict | None = None,
) -> dict:
    """The same courier adapters, in the other direction (ADR 0007/0008)."""
    from app.modules.fulfilment import service as fulfilment

    ret = await load(db, tenant_id, return_id, vendor_id=vendor_id)
    _check(ret["status"], "pickup_booked")
    original = (
        (
            await db.execute(
                text(
                    "SELECT courier, consignment_id FROM shipments WHERE tenant_id = :t AND sub_order_id = :s "
                    "AND status <> 'cancelled'"
                ),
                {"t": tenant_id, "s": ret["sub_order_id"]},
            )
        )
        .mappings()
        .first()
    )
    courier = courier or (original["courier"] if original else None)
    if courier is None:
        raise ReturnError("No courier to collect this return", code="no_courier")
    order = (
        (
            await db.execute(
                text(
                    "SELECT shipping_address, contact_phone FROM orders WHERE tenant_id = :t AND id = :o"
                ),
                {"t": tenant_id, "o": ret["order_id"]},
            )
        )
        .mappings()
        .one()
    )
    account = await fulfilment.load_account(db, settings, tenant_id, courier, str(ret["vendor_id"]))
    adapter = fulfilment.build_courier(courier, account.mode, overrides)
    address = order["shipping_address"]
    booking = await adapter.book(
        credentials=account.credentials,
        pickup_ref=account.pickup_ref,
        to=fulfilment.Address(
            name=address.get("recipient_name", ""),
            phone=address.get("phone") or order["contact_phone"],
            district=address.get("district_code", ""),
            upazila=address.get("upazila"),
            area=address.get("area"),
            line=address.get("address_line", ""),
        ),
        reference=f"{ret['number']}-RTN",
        weight_grams=500,
        cod_amount=Decimal("0"),
        note=f"Reverse pickup for {ret['order_number']}",
    )
    await db.execute(
        text(
            """UPDATE return_requests SET status = 'pickup_booked', pickup_courier = :c,
                      pickup_consignment_id = :cid, pickup_tracking_url = :url, updated_at = now()
               WHERE tenant_id = :t AND id = :i"""
        ),
        {
            "c": courier,
            "cid": booking.consignment_id,
            "url": booking.tracking_url,
            "t": tenant_id,
            "i": return_id,
        },
    )
    return {
        "id": str(return_id),
        "status": "pickup_booked",
        "courier": courier,
        "consignment_id": booking.consignment_id,
        "tracking_url": booking.tracking_url,
    }


async def mark(
    db: AsyncSession, tenant_id: str, return_id, status: str, *, actor_id: str, vendor_id=None
) -> dict:
    ret = await load(db, tenant_id, return_id, vendor_id=vendor_id)
    _check(ret["status"], status)
    await _set_status(db, tenant_id, return_id, status)
    return {"id": str(return_id), "status": status}


async def qc(
    db: AsyncSession,
    tenant_id: str,
    return_id,
    *,
    passed: bool,
    results: dict[str, str] | None,
    note: str | None,
    actor_id: str,
    vendor_id=None,
) -> dict:
    """Vendor inspects what came back. A pass restocks the goods; a fail never does."""
    ret = await load(db, tenant_id, return_id, vendor_id=vendor_id)
    target = "qc_passed" if passed else "qc_failed"
    _check(ret["status"], target)
    items = (
        (
            await db.execute(
                text(
                    "SELECT id, variant_id, qty FROM return_items WHERE tenant_id = :t AND return_id = :r "
                    "ORDER BY variant_id"
                ),
                {"t": tenant_id, "r": return_id},
            )
        )
        .mappings()
        .all()
    )
    restocked = 0
    for item in items:
        result = (results or {}).get(str(item["id"]), "restock" if passed else "write_off")
        await db.execute(
            text("UPDATE return_items SET qc_result = :r WHERE tenant_id = :t AND id = :i"),
            {"r": result, "t": tenant_id, "i": item["id"]},
        )
        if result != "restock":
            continue
        balance = (
            await db.execute(
                text(
                    "UPDATE product_variants SET stock_on_hand = stock_on_hand + :q, updated_at = now() "
                    "WHERE tenant_id = :t AND id = :v RETURNING stock_on_hand"
                ),
                {"q": item["qty"], "t": tenant_id, "v": item["variant_id"]},
            )
        ).scalar()
        await db.execute(
            text(
                """INSERT INTO inventory_movements (tenant_id, vendor_id, variant_id, delta, balance_after,
                       reason, ref, actor_id)
                   VALUES (:t, :vend, :v, :d, :b, 'return', :ref, :a)"""
            ),
            {
                "t": tenant_id,
                "vend": ret["vendor_id"],
                "v": item["variant_id"],
                "d": item["qty"],
                "b": balance,
                "ref": ret["number"],
                "a": actor_id,
            },
        )
        restocked += 1
    await _set_status(db, tenant_id, return_id, target, qc_by=actor_id, note=note)
    return {"id": str(return_id), "status": target, "restocked_lines": restocked}


# ------------------------------------------------------------------------------------ refunds
async def refund(
    db: AsyncSession,
    settings,
    tenant_id: str,
    return_id,
    *,
    actor_id: str,
    overrides: dict | None = None,
) -> dict:
    """Money goes back the way it came, unless the buyer chose store credit or paid cash."""
    from app.modules.payments.gateways import GatewayError, build_gateway
    from app.modules.payments.service import load_account

    ret = await load(db, tenant_id, return_id)
    _check(ret["status"], "refunded")
    amount = money(ret["refund_total"])
    if ret["shipping_payer"] == "buyer":
        amount = money(amount - Decimal(str(ret["return_shipping_fee"])))
    if amount <= 0:
        raise ReturnError("There is nothing left to refund", code="nothing_to_refund")

    method = ret["refund_method"]
    payment = (
        (
            await db.execute(
                text(
                    """SELECT id, provider, amount, refunded_amount, provider_ref FROM payments
                       WHERE tenant_id = :t AND order_id = :o AND status IN ('paid','partially_refunded')
                       ORDER BY (provider = 'store_credit'), created_at DESC LIMIT 1"""
                ),
                {"t": tenant_id, "o": ret["order_id"]},
            )
        )
        .mappings()
        .first()
    )
    gateway_capture = (
        payment is not None
        and payment["provider"] in ("bkash", "sslcommerz")
        and payment["provider_ref"]
    )
    if method == "original":
        if payment is None:
            raise ReturnError("This order has no capture to refund", code="no_capture")
        method = payment["provider"]
    if method == "store_credit":
        rail = "store_credit"
    elif method == "bank":
        rail = "manual_bank"
    elif method in ("bkash", "sslcommerz") and gateway_capture and payment["provider"] == method:
        # money goes back down the rail it came up
        rail = method
    else:
        # a wallet the buyer named, with no capture behind it (COD): finance pays it by hand
        rail = "manual_bkash" if method == "bkash" else "manual_bank"

    refund_id = (
        await db.execute(
            text(
                """INSERT INTO refunds (tenant_id, return_id, order_id, payment_id, method, amount,
                       status, requested_by)
                   VALUES (:t, :r, :o, :p, :m, :a, 'processing', :by) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "r": return_id,
                "o": ret["order_id"],
                "p": payment["id"] if payment else None,
                "m": rail,
                "a": amount,
                "by": actor_id,
            },
        )
    ).scalar()

    if rail == "store_credit":
        await store_credit.grant(
            db,
            tenant_id,
            str(ret["user_id"]),
            amount,
            reason="return_refund",
            actor_id=actor_id,
            ref_type="return",
            ref_id=return_id,
            note=ret["number"],
        )
        await _complete_refund(db, tenant_id, refund_id, actor_id, provider_ref=ret["number"])
    elif rail in ("bkash", "sslcommerz"):
        account = await load_account(db, settings, tenant_id, rail)
        gateway = build_gateway(rail, account.mode, overrides)
        try:
            result = await gateway.refund(
                credentials=account.credentials,
                provider_ref=payment["provider_ref"],
                amount=amount,
                reason=ret["reason"],
            )
        except GatewayError as exc:
            await db.execute(
                text(
                    "UPDATE refunds SET status = 'failed', failure_reason = :e WHERE tenant_id = :t AND id = :i"
                ),
                {"e": str(exc)[:500], "t": tenant_id, "i": refund_id},
            )
            raise ReturnError(
                "The payment provider refused the refund. Finance can retry or pay manually.",
                status=502,
                code="refund_failed",
            ) from exc
        await _complete_refund(
            db,
            tenant_id,
            refund_id,
            actor_id,
            provider_ref=str(result.get("refundTransactionId") or result.get("bank_tran_id") or ""),
        )
    else:
        # manual rails (COD to bKash or bank): finance pays and marks it, nothing moves yet
        await db.execute(
            text("UPDATE refunds SET status = 'pending' WHERE tenant_id = :t AND id = :i"),
            {"t": tenant_id, "i": refund_id},
        )
        return {
            "id": str(refund_id),
            "status": "pending",
            "method": rail,
            "amount": amount,
            "return_status": ret["status"],
        }

    if payment is not None and rail != "store_credit":
        await _record_payment_refund(db, tenant_id, payment, amount)
    await _set_status(db, tenant_id, return_id, "refunded")
    await _post_refund_ledger(db, tenant_id, ret, refund_id=refund_id, amount=amount, rail=rail)
    note = await issue_credit_note(db, tenant_id, return_id, actor_id=actor_id)
    return {
        "id": str(refund_id),
        "status": "completed",
        "method": rail,
        "amount": amount,
        "return_status": "refunded",
        "credit_note": note["number"],
    }


async def _post_refund_ledger(
    db: AsyncSession, tenant_id: str, ret, *, refund_id, amount, rail
) -> None:
    """Unwind the money: the vendor's payable, the tenant's commission and the VAT all come back."""
    from app.modules.ledger import service as ledger

    vat = (
        await db.execute(
            text(
                "SELECT coalesce(sum(vat_amount), 0) FROM return_items WHERE tenant_id = :t AND return_id = :r"
            ),
            {"t": tenant_id, "r": ret["id"]},
        )
    ).scalar()
    commission_rate = (
        await db.execute(
            text("SELECT commission_rate FROM sub_orders WHERE tenant_id = :t AND id = :s"),
            {"t": tenant_id, "s": ret["sub_order_id"]},
        )
    ).scalar()
    await ledger.post_refund(
        db,
        tenant_id,
        refund_id=refund_id,
        sub_order_id=ret["sub_order_id"],
        vendor_id=str(ret["vendor_id"]),
        amount=amount,
        vat_amount=money(min(Decimal(str(vat)), Decimal(str(amount)))),
        commission_rate=commission_rate or Decimal("0"),
        rail=rail,
    )


async def _record_payment_refund(
    db: AsyncSession, tenant_id: str, payment, amount: Decimal
) -> None:
    refunded = money(Decimal(str(payment["refunded_amount"])) + amount)
    status = "refunded" if refunded >= money(payment["amount"]) else "partially_refunded"
    await db.execute(
        text(
            "UPDATE payments SET refunded_amount = :r, status = :s, updated_at = now() "
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"r": refunded, "s": status, "t": tenant_id, "i": payment["id"]},
    )


async def _complete_refund(
    db: AsyncSession, tenant_id: str, refund_id, actor_id: str, *, provider_ref: str | None
) -> None:
    await db.execute(
        text(
            """UPDATE refunds SET status = 'completed', completed_at = now(), completed_by = :a,
                      provider_ref = :r WHERE tenant_id = :t AND id = :i"""
        ),
        {"a": actor_id, "r": provider_ref or None, "t": tenant_id, "i": refund_id},
    )


async def complete_manual_refund(
    db: AsyncSession, tenant_id: str, refund_id, *, reference: str, actor_id: str
) -> dict:
    """Finance paid a COD refund by hand; the reference is what makes it auditable."""
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM refunds WHERE tenant_id = :t AND id = :i AND status = 'pending'"
                ),
                {"t": tenant_id, "i": refund_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    await _complete_refund(db, tenant_id, refund_id, actor_id, provider_ref=reference)
    if row["return_id"]:
        ret = await load(db, tenant_id, row["return_id"])
        await _set_status(db, tenant_id, row["return_id"], "refunded")
        await _post_refund_ledger(
            db, tenant_id, ret, refund_id=refund_id, amount=row["amount"], rail=row["method"]
        )
        from app.modules.ledger import service as ledger

        # the tenant owed the buyer, and has now actually paid it
        await ledger.post_manual_refund_paid(
            db, tenant_id, refund_id=refund_id, amount=row["amount"]
        )
        note = await issue_credit_note(db, tenant_id, row["return_id"], actor_id=actor_id)
        return {"id": str(refund_id), "status": "completed", "credit_note": note["number"]}
    return {"id": str(refund_id), "status": "completed"}


async def issue_credit_note(db: AsyncSession, tenant_id: str, return_id, *, actor_id: str) -> dict:
    """A refund against a tax invoice needs a credit note with the VAT stated (ADR 0005)."""
    existing = (
        (
            await db.execute(
                text(
                    "SELECT number, amount, vat_amount FROM credit_notes WHERE tenant_id = :t AND return_id = :r"
                ),
                {"t": tenant_id, "r": return_id},
            )
        )
        .mappings()
        .first()
    )
    if existing:
        return dict(existing)
    ret = await load(db, tenant_id, return_id)
    vat = (
        await db.execute(
            text(
                "SELECT coalesce(sum(vat_amount), 0) FROM return_items WHERE tenant_id = :t AND return_id = :r"
            ),
            {"t": tenant_id, "r": return_id},
        )
    ).scalar()
    n = (
        await db.execute(
            text(
                """UPDATE tenant_settings SET next_credit_note_number = next_credit_note_number + 1
                   WHERE tenant_id = :t RETURNING next_credit_note_number - 1"""
            ),
            {"t": tenant_id},
        )
    ).scalar()
    number = f"CN-{n}"
    await db.execute(
        text(
            """INSERT INTO credit_notes (tenant_id, vendor_id, number, return_id, sub_order_id, amount,
                   vat_amount, reason, issued_by)
               VALUES (:t, :v, :n, :r, :s, :a, :vat, :reason, :by)"""
        ),
        {
            "t": tenant_id,
            "v": ret["vendor_id"],
            "n": number,
            "r": return_id,
            "s": ret["sub_order_id"],
            "a": ret["refund_total"],
            "vat": vat,
            "reason": ret["reason"],
            "by": actor_id,
        },
    )
    return {"number": number, "amount": ret["refund_total"], "vat_amount": vat}


async def escalate_overdue_qc(db: AsyncSession, tenant_id: str) -> int:
    """QC past its SLA becomes the tenant's problem, not the buyer's."""
    res = await db.execute(
        text(
            """UPDATE return_requests SET escalated = true, updated_at = now()
               WHERE tenant_id = :t AND status IN ('received','picked_up') AND NOT escalated
                 AND qc_due_at IS NOT NULL AND qc_due_at < now()"""
        ),
        {"t": tenant_id},
    )
    return res.rowcount


def decrypt_target(settings, tenant_id: str, return_id, ciphertext: str | None) -> dict | None:
    if not ciphertext:
        return None
    return Encryptor(settings.data_encryption_key).decrypt(
        ciphertext, context=_context(tenant_id, return_id)
    )


def mask_target(target: dict | None) -> dict | None:
    if not target:
        return None
    from app.core.crypto import last4

    return {k: f"••••{last4(v)}" for k, v in target.items() if isinstance(v, str)}


def _json(payload: dict) -> str:
    return json.dumps(payload, default=str)
