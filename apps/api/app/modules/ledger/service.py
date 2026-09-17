"""The tenant ledger: every taka that moves, in balanced groups, append-only (ADR 0003).

Three rules the rest of the system depends on:

1. **Every group balances.** `post()` refuses an unbalanced group, and a deferred constraint
   trigger refuses it again at COMMIT, so no code path can write a half-entry.
2. **Every business event posts once.** A unique index on `(entry_type, ref_type, ref_id, account,
   vendor_id)` makes a replayed webhook, a retried job or a double-clicked button harmless.
3. **Nothing is ever edited.** Mistakes are corrected with a reversing group, so the history of
   what we believed, and when, survives.

Commission and VAT are taken from the snapshot on the sub-order, never recomputed from today's
rates, so changing a commission rate can never rewrite last month's money.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.modules.checkout.pricing import ZERO, money
from app.modules.ledger import accounts as acct


class LedgerError(AppError):
    status = 500
    code = "ledger_error"


@dataclass
class Line:
    account: str
    direction: str
    amount: Decimal
    vendor_id: str | None = None
    memo: str | None = None


async def already_posted(
    db: AsyncSession, tenant_id: str, *, entry_type: str, ref_type: str, ref_id
) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    """SELECT 1 FROM ledger_entries WHERE tenant_id = :t AND entry_type = :e
                       AND ref_type = :rt AND ref_id = :ri LIMIT 1"""
                ),
                {"t": tenant_id, "e": entry_type, "rt": ref_type, "ri": ref_id},
            )
        ).first()
    )


async def post(
    db: AsyncSession,
    tenant_id: str,
    *,
    entry_type: str,
    ref_type: str,
    ref_id,
    lines: list[Line],
    memo: str | None = None,
) -> str | None:
    """Write one balanced group. Returns None when this event was already posted."""
    lines = [ln for ln in lines if money(ln.amount) > 0]
    if not lines:
        return None
    if await already_posted(db, tenant_id, entry_type=entry_type, ref_type=ref_type, ref_id=ref_id):
        return None
    delta = sum(
        (money(ln.amount) if ln.direction == acct.DEBIT else -money(ln.amount)) for ln in lines
    )
    if delta != 0:
        raise LedgerError(f"{entry_type} group does not balance (off by {delta})")
    group_id = uuid.uuid4()
    for ln in lines:
        await db.execute(
            text(
                """INSERT INTO ledger_entries (tenant_id, group_id, account, vendor_id, direction,
                       amount, entry_type, ref_type, ref_id, memo)
                   VALUES (:t, :g, :a, :v, :d, :amt, :e, :rt, :ri, :m)"""
            ),
            {
                "t": tenant_id,
                "g": group_id,
                "a": ln.account,
                "v": ln.vendor_id,
                "d": ln.direction,
                "amt": money(ln.amount),
                "e": entry_type,
                "rt": ref_type,
                "ri": ref_id,
                "m": ln.memo or memo,
            },
        )
    return str(group_id)


# ------------------------------------------------------------------------------- order postings
async def _sub_orders(db: AsyncSession, tenant_id: str, order_id) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.vendor_id, s.number, s.items_subtotal, s.discount_total,
                              s.shipping_fee, s.shipping_waived, s.shipping_waiver_funded_by,
                              s.vat_total, s.total, s.commission_rate, s.status, o.vat_pricing
                       FROM sub_orders s JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.order_id = :o ORDER BY s.number"""
                ),
                {"t": tenant_id, "o": order_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def split_sub_order(sub: dict) -> dict:
    """How one shipment's money divides. Everything here comes from the sub-order snapshot."""
    gross_items = money(Decimal(str(sub["items_subtotal"])) - Decimal(str(sub["discount_total"])))
    vat = money(sub["vat_total"])
    shipping = money(sub["shipping_fee"])
    taxable = money(gross_items - vat) if sub["vat_pricing"] == "inclusive" else gross_items
    commission = money(taxable * Decimal(str(sub["commission_rate"])))
    return {
        "taxable": taxable,
        "vat": vat,
        "shipping": shipping,
        "commission": commission,
        "vendor_payable": money(taxable - commission),
        "total": money(sub["total"]),
    }


async def _tenders(db: AsyncSession, tenant_id: str, order_id) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT provider, amount FROM payments
                       WHERE tenant_id = :t AND order_id = :o AND status IN ('paid','partially_refunded','refunded')
                       ORDER BY created_at"""
                ),
                {"t": tenant_id, "o": order_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _allocate(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split `amount` across weights, largest-remainder, so the parts always sum exactly."""
    total = sum(weights)
    if total <= 0:
        return [ZERO for _ in weights]
    raw = [Decimal(str(amount)) * w / total for w in weights]
    out = [money(x) for x in raw]
    drift = money(amount) - sum(out)
    if drift != 0:
        order = sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=drift > 0)
        step = money(Decimal("0.01")) * (1 if drift > 0 else -1)
        for i in range(int(abs(drift) / Decimal("0.01"))):
            out[order[i % len(out)]] += step
    return out


async def post_capture(db: AsyncSession, tenant_id: str, order_id) -> list[str]:
    """Prepaid money has arrived (or store credit covered it): book it against each shipment.

    Debits follow the tender the buyer actually used — gateway clearing for a card or wallet
    capture, the store-credit liability for credit spent — so the balance sheet knows where the
    money is sitting, not merely that it arrived.
    """
    subs = await _sub_orders(db, tenant_id, order_id)
    subs = [s for s in subs if s["status"] not in ("cancelled", "pending_payment")]
    if not subs:
        return []
    tenders = await _tenders(db, tenant_id, order_id)
    if not tenders:
        return []
    totals = [money(s["total"]) for s in subs]
    groups = []
    for sub, split in ((s, split_sub_order(s)) for s in subs):
        idx = subs.index(sub)
        lines: list[Line] = []
        for tender in tenders:
            share = _allocate(money(tender["amount"]), totals)[idx]
            if share <= 0:
                continue
            account = (
                acct.STORE_CREDIT_LIABILITY
                if tender["provider"] == "store_credit"
                else acct.gateway_clearing(tender["provider"])
            )
            lines.append(Line(account, acct.DEBIT, share))
        if not lines:
            continue
        debit_total = sum(ln.amount for ln in lines)
        credits = [
            Line(acct.VENDOR_PAYABLE, acct.CREDIT, split["vendor_payable"], str(sub["vendor_id"])),
            Line(acct.COMMISSION_REVENUE, acct.CREDIT, split["commission"]),
            Line(acct.VAT_PAYABLE, acct.CREDIT, split["vat"]),
            Line(acct.SHIPPING_REVENUE, acct.CREDIT, split["shipping"]),
        ]
        credit_total = sum(money(c.amount) for c in credits)
        # A partially-settled order books only what was actually received.
        if debit_total != credit_total:
            scaled = _allocate(debit_total, [money(c.amount) for c in credits])
            credits = [
                Line(c.account, c.direction, amount, c.vendor_id)
                for c, amount in zip(credits, scaled, strict=True)
            ]
        group = await post(
            db,
            tenant_id,
            entry_type="capture",
            ref_type="sub_order",
            ref_id=sub["id"],
            lines=lines + credits,
            memo=sub["number"],
        )
        if group:
            groups.append(group)
    return groups


async def post_cod_delivery(
    db: AsyncSession, tenant_id: str, sub_order_id, courier: str
) -> str | None:
    """The courier took the cash: the tenant is owed by the courier, the vendor by the tenant."""
    sub = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.vendor_id, s.number, s.items_subtotal, s.discount_total,
                              s.shipping_fee, s.shipping_waived, s.shipping_waiver_funded_by,
                              s.vat_total, s.total, s.commission_rate, s.status, o.vat_pricing
                       FROM sub_orders s JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.id = :s"""
                ),
                {"t": tenant_id, "s": sub_order_id},
            )
        )
        .mappings()
        .first()
    )
    if sub is None:
        return None
    split = split_sub_order(dict(sub))
    return await post(
        db,
        tenant_id,
        entry_type="cod_delivery",
        ref_type="sub_order",
        ref_id=sub["id"],
        lines=[
            Line(acct.cod_receivable(courier), acct.DEBIT, split["total"]),
            Line(acct.VENDOR_PAYABLE, acct.CREDIT, split["vendor_payable"], str(sub["vendor_id"])),
            Line(acct.COMMISSION_REVENUE, acct.CREDIT, split["commission"]),
            Line(acct.VAT_PAYABLE, acct.CREDIT, split["vat"]),
            Line(acct.SHIPPING_REVENUE, acct.CREDIT, split["shipping"]),
        ],
        memo=sub["number"],
    )


async def post_cod_settlement(
    db: AsyncSession,
    tenant_id: str,
    *,
    shipment_id,
    courier: str,
    amount: Decimal,
    fee: Decimal,
) -> str | None:
    """The courier remitted: cash lands in the bank, the courier's fee becomes an expense."""
    amount, fee = money(amount), money(fee)
    return await post(
        db,
        tenant_id,
        entry_type="cod_settlement",
        ref_type="shipment",
        ref_id=shipment_id,
        lines=[
            Line(acct.TENANT_BANK, acct.DEBIT, money(amount - fee)),
            Line(acct.COURIER_FEE, acct.DEBIT, fee),
            Line(acct.cod_receivable(courier), acct.CREDIT, amount),
        ],
    )


async def post_refund(
    db: AsyncSession,
    tenant_id: str,
    *,
    refund_id,
    sub_order_id,
    vendor_id: str,
    amount: Decimal,
    vat_amount: Decimal,
    commission_rate: Decimal,
    rail: str,
) -> str | None:
    """Money going back: the vendor's payable, the tenant's commission and the VAT all unwind."""
    amount, vat_amount = money(amount), money(vat_amount)
    taxable = money(amount - vat_amount)
    commission = money(taxable * Decimal(str(commission_rate)))
    credit_account = {
        "store_credit": acct.STORE_CREDIT_LIABILITY,
        "manual_bank": acct.BUYER_REFUND_PAYABLE,
        "manual_bkash": acct.BUYER_REFUND_PAYABLE,
    }.get(rail, acct.gateway_clearing(rail))
    return await post(
        db,
        tenant_id,
        entry_type="return_reversal",
        ref_type="refund",
        ref_id=refund_id,
        lines=[
            Line(acct.VENDOR_PAYABLE, acct.DEBIT, money(taxable - commission), vendor_id),
            Line(acct.COMMISSION_REVENUE, acct.DEBIT, commission),
            Line(acct.VAT_PAYABLE, acct.DEBIT, vat_amount),
            Line(credit_account, acct.CREDIT, amount),
        ],
        memo=str(sub_order_id),
    )


async def post_manual_refund_paid(
    db: AsyncSession, tenant_id: str, *, refund_id, amount: Decimal
) -> str | None:
    """Finance actually paid a refund the tenant owed: the liability clears against the bank."""
    return await post(
        db,
        tenant_id,
        entry_type="refund",
        ref_type="refund_payment",
        ref_id=refund_id,
        lines=[
            Line(acct.BUYER_REFUND_PAYABLE, acct.DEBIT, money(amount)),
            Line(acct.TENANT_BANK, acct.CREDIT, money(amount)),
        ],
    )


async def post_store_credit_issue(
    db: AsyncSession, tenant_id: str, *, ref_id, amount: Decimal, reason: str
) -> str | None:
    """Goodwill credit is an expense the moment it is promised, not when it is spent."""
    return await post(
        db,
        tenant_id,
        entry_type="store_credit_issue",
        ref_type="store_credit",
        ref_id=ref_id,
        lines=[
            Line(acct.PROMO_TENANT, acct.DEBIT, money(amount), memo=reason),
            Line(acct.STORE_CREDIT_LIABILITY, acct.CREDIT, money(amount)),
        ],
    )


async def post_payout(
    db: AsyncSession,
    tenant_id: str,
    *,
    line_id,
    vendor_id: str,
    gross: Decimal,
    tds: Decimal,
    net: Decimal,
) -> str | None:
    return await post(
        db,
        tenant_id,
        entry_type="payout",
        ref_type="payout_line",
        ref_id=line_id,
        lines=[
            Line(acct.VENDOR_PAYABLE, acct.DEBIT, money(gross), vendor_id),
            Line(acct.TDS_PAYABLE, acct.CREDIT, money(tds)),
            Line(acct.TENANT_BANK, acct.CREDIT, money(net)),
        ],
    )


# ------------------------------------------------------------------------------------ balances
async def account_balance(db: AsyncSession, tenant_id: str, account: str) -> Decimal:
    value = (
        await db.execute(
            text(
                """SELECT coalesce(sum(CASE WHEN direction = 'debit' THEN amount ELSE -amount END), 0)
                   FROM ledger_entries WHERE tenant_id = :t AND account = :a"""
            ),
            {"t": tenant_id, "a": account},
        )
    ).scalar()
    return money(value)


async def vendor_payable(db: AsyncSession, tenant_id: str, vendor_id: str) -> Decimal:
    """Credits minus debits on the vendor's payable: what the tenant owes them in total."""
    value = (
        await db.execute(
            text(
                """SELECT coalesce(sum(CASE WHEN direction = 'credit' THEN amount ELSE -amount END), 0)
                   FROM ledger_entries
                   WHERE tenant_id = :t AND account = :a AND vendor_id = CAST(:v AS uuid)"""
            ),
            {"t": tenant_id, "a": acct.VENDOR_PAYABLE, "v": vendor_id},
        )
    ).scalar()
    return money(value)


async def vendor_reserve(db: AsyncSession, tenant_id: str, vendor_id: str) -> Decimal:
    """One reserve rule per tenant (ADR 0003 invariant 4) — percent or window, never both."""
    settings = (
        (
            await db.execute(
                text(
                    "SELECT reserve_mode, reserve_percent, reserve_days FROM tenant_settings "
                    "WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    if settings["reserve_mode"] == "none":
        return ZERO
    if settings["reserve_mode"] == "percent":
        payable = await vendor_payable(db, tenant_id, vendor_id)
        return money(max(payable, ZERO) * Decimal(str(settings["reserve_percent"])))
    held = (
        await db.execute(
            text(
                """SELECT coalesce(sum(CASE WHEN e.direction = 'credit' THEN e.amount ELSE -e.amount END), 0)
                   FROM ledger_entries e
                   JOIN sub_orders s ON s.id = e.ref_id AND s.tenant_id = e.tenant_id
                   LEFT JOIN shipments sh ON sh.sub_order_id = s.id AND sh.tenant_id = s.tenant_id
                   WHERE e.tenant_id = :t AND e.account = :a AND e.vendor_id = CAST(:v AS uuid)
                     AND e.ref_type = 'sub_order'
                     AND coalesce(sh.delivered_at, e.created_at) > now() - make_interval(days => :d)"""
            ),
            {
                "t": tenant_id,
                "a": acct.VENDOR_PAYABLE,
                "v": vendor_id,
                "d": settings["reserve_days"],
            },
        )
    ).scalar()
    return money(max(Decimal(str(held)), ZERO))


async def vendor_holds(db: AsyncSession, tenant_id: str, vendor_id: str) -> Decimal:
    """A hold blocks the whole payout, so it is reported as the full payable when active."""
    active = (
        await db.execute(
            text(
                """SELECT count(*) FROM payout_holds
                   WHERE tenant_id = :t AND vendor_id = CAST(:v AS uuid) AND released_at IS NULL
                     AND (hold_until IS NULL OR hold_until > now())"""
            ),
            {"t": tenant_id, "v": vendor_id},
        )
    ).scalar()
    return Decimal(active or 0)


async def vendor_statement(db: AsyncSession, tenant_id: str, vendor_id: str) -> dict:
    payable = await vendor_payable(db, tenant_id, vendor_id)
    reserve = await vendor_reserve(db, tenant_id, vendor_id)
    holds = await vendor_holds(db, tenant_id, vendor_id)
    available = ZERO if holds else money(max(payable - reserve, ZERO))
    return {
        "payable": payable,
        "reserve": reserve,
        "on_hold": bool(holds),
        "available": available,
    }


async def trial_balance(db: AsyncSession, tenant_id: str, *, since: date | None = None) -> dict:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT account,
                              sum(CASE WHEN direction = 'debit' THEN amount ELSE 0 END) AS debits,
                              sum(CASE WHEN direction = 'credit' THEN amount ELSE 0 END) AS credits
                       FROM ledger_entries
                       WHERE tenant_id = :t AND (CAST(:s AS date) IS NULL OR created_at >= :s)
                       GROUP BY account ORDER BY account"""
                ),
                {"t": tenant_id, "s": since},
            )
        )
        .mappings()
        .all()
    )
    debits = sum(Decimal(str(r["debits"])) for r in rows)
    credits = sum(Decimal(str(r["credits"])) for r in rows)
    return {
        "accounts": [
            {
                "account": r["account"],
                "debits": money(r["debits"]),
                "credits": money(r["credits"]),
                "balance": money(Decimal(str(r["debits"])) - Decimal(str(r["credits"]))),
            }
            for r in rows
        ],
        "total_debits": money(debits),
        "total_credits": money(credits),
        "balanced": money(debits) == money(credits),
    }
