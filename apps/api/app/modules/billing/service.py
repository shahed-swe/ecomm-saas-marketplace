"""SaaS billing (ADR 0011). Runs on the platform DB role; tenants only read their rows.

Amounts are Decimal, rounded half-up to 0.01 BDT. Invoice generation is idempotent per
(tenant, period_start). Dunning is a pure function of dates so it is testable and re-runnable.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound

CENT = Decimal("0.01")
DUE_DAYS = 7
PAST_DUE_AFTER = 7
SUSPEND_AFTER = 14


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class EntitlementError(AppError):
    status = 402
    code = "plan_limit"


async def start_subscription(
    db: AsyncSession, tenant_id, plan_code: str = "starter", today: date | None = None
) -> None:
    today = today or datetime.now(UTC).date()
    plan = (
        await db.execute(text("SELECT trial_days FROM plans WHERE code = :c"), {"c": plan_code})
    ).first()
    if plan is None:
        raise NotFound("Plan not found")
    trial_end = today + timedelta(days=plan.trial_days)
    await db.execute(
        text("""INSERT INTO tenant_subscriptions (tenant_id, plan_code, status, trial_ends_at,
                    current_period_start, current_period_end)
                VALUES (:t, :p, 'trial', :te, :ps, :pe)"""),
        {
            "t": str(tenant_id),
            "p": plan_code,
            "te": datetime.combine(trial_end, datetime.min.time(), UTC),
            "ps": today,
            "pe": trial_end if plan.trial_days else add_months(today, 1),
        },
    )


@dataclass
class Entitlements:
    plan_code: str
    status: str
    limits: dict


async def entitlements(db: AsyncSession, tenant_id: str) -> Entitlements:
    row = (
        await db.execute(
            text("""SELECT s.plan_code, s.status, p.limits FROM tenant_subscriptions s
                JOIN plans p ON p.code = s.plan_code WHERE s.tenant_id = :t"""),
            {"t": tenant_id},
        )
    ).first()
    if row is None:
        return Entitlements("none", "none", {})
    return Entitlements(row.plan_code, row.status, dict(row.limits))


async def require_feature(db: AsyncSession, tenant_id: str, key: str) -> None:
    ent = await entitlements(db, tenant_id)
    if not ent.limits.get(key):
        raise EntitlementError(f"Your plan does not include {key.replace('_', ' ')}")


async def require_quota(
    db: AsyncSession, tenant_id: str, key: str, used: int, adding: int = 1
) -> None:
    ent = await entitlements(db, tenant_id)
    limit = ent.limits.get(key)
    if limit is None or used + adding > int(limit):
        raise EntitlementError(f"Plan limit reached for {key.replace('_', ' ')} ({limit})")


async def record_usage(
    db: AsyncSession,
    tenant_id: str,
    metric: str,
    period_date: date,
    quantity: Decimal,
    source_ref: str,
) -> bool:
    """Idempotent on (tenant, metric, source_ref). Phase 14 feeds GMV from the tenant ledger."""
    res = await db.execute(
        text("""INSERT INTO usage_records (tenant_id, metric, period_date, quantity, source_ref)
                VALUES (:t, :m, :d, :q, :s) ON CONFLICT (tenant_id, metric, source_ref) DO NOTHING"""),
        {"t": tenant_id, "m": metric, "d": period_date, "q": money(quantity), "s": source_ref},
    )
    return res.rowcount == 1


async def generate_invoice(
    db: AsyncSession, tenant_id: str, *, vat_rate: Decimal = Decimal("0"), today: date | None = None
) -> dict | None:
    """Close the current period if it has ended: invoice subscription (+ setup fee once) and the
    GMV fee for the period, then roll the period forward. Returns None if nothing is due."""
    today = today or datetime.now(UTC).date()
    sub = (
        (
            await db.execute(
                text("""SELECT s.*, p.name AS plan_name, p.monthly_price, p.yearly_price, p.gmv_fee_rate, p.setup_fee
                FROM tenant_subscriptions s JOIN plans p ON p.code = s.plan_code
                WHERE s.tenant_id = :t FOR UPDATE OF s"""),
                {"t": tenant_id},
            )
        )
        .mappings()
        .first()
    )
    if sub is None or sub["status"] == "cancelled" or sub["current_period_end"] > today:
        return None
    period_start, period_end = sub["current_period_start"], sub["current_period_end"]
    existing = (
        await db.execute(
            text("SELECT id FROM platform_invoices WHERE tenant_id = :t AND period_start = :ps"),
            {"t": tenant_id, "ps": period_start},
        )
    ).first()
    if existing:
        return None

    lines: list[tuple[str, str, Decimal, Decimal]] = []  # kind, description, qty, unit
    in_trial = sub["status"] == "trial"
    if not in_trial:
        price = (
            sub["price_override"]
            if sub["price_override"] is not None
            else (sub["yearly_price"] if sub["interval"] == "yearly" else sub["monthly_price"])
        )
        lines.append(
            (
                "subscription",
                f"{sub['plan_name']} plan ({sub['interval']})",
                Decimal(1),
                Decimal(price),
            )
        )
        if not sub["setup_fee_invoiced"] and Decimal(sub["setup_fee"]) > 0:
            lines.append(("setup_fee", "One-time setup", Decimal(1), Decimal(sub["setup_fee"])))
    gmv = (
        await db.execute(
            text("""SELECT coalesce(sum(quantity), 0) FROM usage_records
                WHERE tenant_id = :t AND metric = 'gmv' AND period_date >= :ps AND period_date < :pe"""),
            {"t": tenant_id, "ps": period_start, "pe": period_end},
        )
    ).scalar()
    rate = (
        sub["gmv_fee_rate_override"]
        if sub["gmv_fee_rate_override"] is not None
        else sub["gmv_fee_rate"]
    )
    if Decimal(gmv) > 0 and Decimal(rate) > 0:
        lines.append(
            (
                "gmv_fee",
                f"Transaction fee {Decimal(rate) * 100:.2f}% of GMV",
                Decimal(gmv),
                Decimal(rate),
            )
        )

    next_start = period_end
    next_end = add_months(next_start, 12 if sub["interval"] == "yearly" else 1)
    new_status = "active" if in_trial else sub["status"]
    await db.execute(
        text("""UPDATE tenant_subscriptions SET current_period_start = :ns, current_period_end = :ne,
                   status = :st, setup_fee_invoiced = setup_fee_invoiced OR :setup, updated_at = now()
                WHERE tenant_id = :t"""),
        {
            "ns": next_start,
            "ne": next_end,
            "st": new_status,
            "t": tenant_id,
            "setup": any(k == "setup_fee" for k, *_ in lines),
        },
    )
    if in_trial:
        await db.execute(
            text("UPDATE tenants SET status = 'active' WHERE id = :t AND status = 'trial'"),
            {"t": tenant_id},
        )
    if not lines:
        return None

    subtotal = sum((money(q * u) for _, _, q, u in lines), Decimal("0.00"))
    vat = money(subtotal * vat_rate)
    seq = (await db.execute(text("SELECT nextval('platform_invoice_seq')"))).scalar()
    number = f"INV-{today.year}-{seq:06d}"
    inv = (
        await db.execute(
            text("""INSERT INTO platform_invoices (tenant_id, number, period_start, period_end, subtotal,
                    vat_rate, vat_amount, total, due_date)
                VALUES (:t, :n, :ps, :pe, :sub, :vr, :va, :tot, :due) RETURNING id"""),
            {
                "t": tenant_id,
                "n": number,
                "ps": period_start,
                "pe": period_end,
                "sub": subtotal,
                "vr": vat_rate,
                "va": vat,
                "tot": subtotal + vat,
                "due": today + timedelta(days=DUE_DAYS),
            },
        )
    ).scalar()
    for kind, desc, qty, unit in lines:
        await db.execute(
            text("""INSERT INTO platform_invoice_lines (invoice_id, tenant_id, kind, description, quantity,
                        unit_amount, amount) VALUES (:i, :t, :k, :d, :q, :u, :a)"""),
            {
                "i": inv,
                "t": tenant_id,
                "k": kind,
                "d": desc,
                "q": qty,
                "u": unit,
                "a": money(qty * unit),
            },
        )
    return {"id": inv, "number": number, "total": subtotal + vat}


async def mark_paid(db: AsyncSession, invoice_id: str, *, method: str, reference: str) -> dict:
    inv = (
        (
            await db.execute(
                text(
                    "SELECT id, tenant_id, status FROM platform_invoices WHERE id = :i FOR UPDATE"
                ),
                {"i": invoice_id},
            )
        )
        .mappings()
        .first()
    )
    if inv is None:
        raise NotFound("Not found")
    if inv["status"] == "paid":
        return {"id": inv["id"], "status": "paid", "changed": False}  # idempotent
    if inv["status"] != "open":
        raise AppError("Invoice is not payable", status=409, code="not_payable")
    await db.execute(
        text("""UPDATE platform_invoices SET status='paid', paid_at=now(), payment_method=:m,
                   payment_reference=:r WHERE id = :i"""),
        {"m": method, "r": reference, "i": invoice_id},
    )
    await reconcile_status(db, str(inv["tenant_id"]))
    return {"id": inv["id"], "status": "paid", "changed": True}


def dunning_state(oldest_unpaid_due: date | None, today: date) -> str:
    if oldest_unpaid_due is None or today <= oldest_unpaid_due:
        return "current"
    overdue = (today - oldest_unpaid_due).days
    if overdue >= SUSPEND_AFTER:
        return "suspended"
    if overdue >= PAST_DUE_AFTER:
        return "past_due"
    return "overdue"


async def reconcile_status(db: AsyncSession, tenant_id: str, today: date | None = None) -> str:
    """Move subscription + tenant status to match unpaid invoices. Never touches cancelled tenants."""
    today = today or datetime.now(UTC).date()
    row = (
        (
            await db.execute(
                text("""SELECT t.status AS tenant_status, s.status AS sub_status,
                   (SELECT min(due_date) FROM platform_invoices i
                     WHERE i.tenant_id = t.id AND i.status = 'open') AS oldest_due
                FROM tenants t JOIN tenant_subscriptions s ON s.tenant_id = t.id WHERE t.id = :t"""),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    if row["tenant_status"] in ("cancelled", "purged") or row["sub_status"] == "trial":
        return row["sub_status"]
    state = dunning_state(row["oldest_due"], today)
    target = {
        "current": "active",
        "overdue": "active",
        "past_due": "past_due",
        "suspended": "suspended",
    }[state]
    if target != row["sub_status"] or target != row["tenant_status"]:
        await db.execute(
            text(
                "UPDATE tenant_subscriptions SET status = :s, updated_at = now() WHERE tenant_id = :t"
            ),
            {"s": target, "t": tenant_id},
        )
        await db.execute(
            text("UPDATE tenants SET status = :s, updated_at = now() WHERE id = :t"),
            {"s": target, "t": tenant_id},
        )
    return target


async def run_billing_cycle(
    db: AsyncSession, today: date | None = None, vat_rate: Decimal = Decimal("0")
) -> dict:
    today = today or datetime.now(UTC).date()
    ids = (
        (
            await db.execute(
                text("""SELECT t.id FROM tenants t JOIN tenant_subscriptions s ON s.tenant_id = t.id
                WHERE t.status NOT IN ('cancelled','purged')""")
            )
        )
        .scalars()
        .all()
    )
    invoiced = 0
    for tid in ids:
        if await generate_invoice(db, str(tid), vat_rate=vat_rate, today=today):
            invoiced += 1
        await reconcile_status(db, str(tid), today)
    return {"tenants": len(ids), "invoiced": invoiced}
