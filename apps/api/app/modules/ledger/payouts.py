"""Vendor payouts: build → approve → export → mark paid (ADR 0009).

Money leaves the tenant's bank here, so every step is deliberate:

* a batch is **idempotent per period** — `payout_lines` is unique on `(tenant, vendor, period_end)`,
  so re-running a weekly build cannot pay a vendor twice for the same week;
* a vendor on hold, below the minimum, or without an active payout method is simply not in it;
* approval is **maker-checker** when the tenant has more than one finance user: the person who
  built the batch cannot be the one who approves it;
* the ledger entry is written when a line is *marked paid* with its bank reference, not when the
  file is exported — an export is a piece of paper, a reference is money that moved.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound
from app.modules.checkout.pricing import ZERO, money
from app.modules.ledger import service as ledger


class PayoutError(AppError):
    status = 409
    code = "payout_conflict"


async def build_batch(db: AsyncSession, tenant_id: str, *, period_end: date, actor_id: str) -> dict:
    existing = (
        await db.execute(
            text("SELECT id, status FROM payout_batches WHERE tenant_id = :t AND period_end = :p"),
            {"t": tenant_id, "p": period_end},
        )
    ).first()
    if existing:
        raise PayoutError("A batch for this period already exists", code="batch_exists")
    settings = (
        (
            await db.execute(
                text(
                    "SELECT payout_min_amount, tds_rate FROM tenant_settings WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    vendors = (
        (
            await db.execute(
                text(
                    """SELECT v.id, v.display_name, m.method, m.account_name, m.last4
                       FROM vendors v
                       LEFT JOIN vendor_payout_methods m ON m.vendor_id = v.id AND m.tenant_id = v.tenant_id
                            AND m.status = 'active'
                       WHERE v.tenant_id = :t AND v.status = 'approved' ORDER BY v.display_name"""
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .all()
    )
    batch_id = (
        await db.execute(
            text(
                """INSERT INTO payout_batches (tenant_id, period_end, created_by)
                   VALUES (:t, :p, :a) RETURNING id"""
            ),
            {"t": tenant_id, "p": period_end, "a": actor_id},
        )
    ).scalar()
    gross_total, tds_total, net_total, count = ZERO, ZERO, ZERO, 0
    skipped = []
    for vendor in vendors:
        statement = await ledger.vendor_statement(db, tenant_id, str(vendor["id"]))
        if statement["on_hold"]:
            skipped.append({"vendor": vendor["display_name"], "reason": "payout_hold"})
            continue
        if vendor["method"] is None:
            skipped.append({"vendor": vendor["display_name"], "reason": "no_payout_method"})
            continue
        gross = statement["available"]
        if gross < money(settings["payout_min_amount"]) or gross <= 0:
            skipped.append({"vendor": vendor["display_name"], "reason": "below_minimum"})
            continue
        paid_this_period = (
            await db.execute(
                text(
                    "SELECT 1 FROM payout_lines WHERE tenant_id = :t AND vendor_id = :v AND period_end = :p"
                ),
                {"t": tenant_id, "v": vendor["id"], "p": period_end},
            )
        ).first()
        if paid_this_period:
            skipped.append({"vendor": vendor["display_name"], "reason": "already_in_a_batch"})
            continue
        tds = money(gross * Decimal(str(settings["tds_rate"])))
        net = money(gross - tds)
        await db.execute(
            text(
                """INSERT INTO payout_lines (tenant_id, batch_id, vendor_id, period_end, gross, tds, net,
                       method, account_last4, account_name)
                   VALUES (:t, :b, :v, :p, :g, :tds, :n, :m, :l4, :an)"""
            ),
            {
                "t": tenant_id,
                "b": batch_id,
                "v": vendor["id"],
                "p": period_end,
                "g": gross,
                "tds": tds,
                "n": net,
                "m": vendor["method"],
                "l4": vendor["last4"],
                "an": vendor["account_name"],
            },
        )
        gross_total += gross
        tds_total += tds
        net_total += net
        count += 1
    await db.execute(
        text(
            """UPDATE payout_batches SET gross_total = :g, tds_total = :tds, net_total = :n,
                      line_count = :c WHERE tenant_id = :t AND id = :i"""
        ),
        {
            "g": gross_total,
            "tds": tds_total,
            "n": net_total,
            "c": count,
            "t": tenant_id,
            "i": batch_id,
        },
    )
    return {
        "id": str(batch_id),
        "period_end": period_end,
        "status": "draft",
        "line_count": count,
        "gross_total": money(gross_total),
        "tds_total": money(tds_total),
        "net_total": money(net_total),
        "skipped": skipped,
    }


async def _batch(db: AsyncSession, tenant_id: str, batch_id):
    row = (
        (
            await db.execute(
                text("SELECT * FROM payout_batches WHERE tenant_id = :t AND id = :i FOR UPDATE"),
                {"t": tenant_id, "i": batch_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return row


async def approve(db: AsyncSession, tenant_id: str, batch_id, *, actor_id: str) -> dict:
    batch = await _batch(db, tenant_id, batch_id)
    if batch["status"] != "draft":
        raise PayoutError("Only a draft batch can be approved", code="bad_state")
    if batch["line_count"] == 0:
        raise PayoutError("There is nothing to pay in this batch", code="empty_batch")
    maker_checker, finance_users = (
        await db.execute(
            text(
                """SELECT ts.maker_checker,
                              (SELECT count(*) FROM staff_members m JOIN staff_roles r ON r.id = m.role_id
                               WHERE m.tenant_id = ts.tenant_id AND m.status = 'active'
                                 AND (r.permissions @> ARRAY['payouts.approve'] OR r.permissions @> ARRAY['*'])
                              ) AS finance_users
                       FROM tenant_settings ts WHERE ts.tenant_id = :t"""
            ),
            {"t": tenant_id},
        )
    ).first()
    if maker_checker and finance_users > 1 and batch["created_by"] == actor_id:
        raise PayoutError(
            "Someone else in finance must approve a batch you prepared", code="maker_checker"
        )
    await db.execute(
        text(
            "UPDATE payout_batches SET status = 'approved', approved_by = :a, approved_at = now() "
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"a": actor_id, "t": tenant_id, "i": batch_id},
    )
    return {"id": str(batch_id), "status": "approved"}


BANK_COLUMNS = ["beneficiary_name", "account_last4", "amount", "reference", "purpose"]
BKASH_COLUMNS = ["wallet_last4", "beneficiary_name", "amount", "reference"]


async def export(db: AsyncSession, tenant_id: str, batch_id, *, method: str, actor_id: str) -> str:
    """A BEFTN/NPSB-shaped bank CSV or a bKash disbursement CSV, whichever the lines need.

    Full account numbers live encrypted in `vendor_payout_methods`; the export carries the last
    four and the beneficiary name, and the bank file is completed in the bank's own portal where
    the account is already on file. Nothing here ever prints a full account number.
    """
    batch = await _batch(db, tenant_id, batch_id)
    if batch["status"] not in ("approved", "exported"):
        raise PayoutError("Approve the batch before exporting it", code="bad_state")
    rows = (
        (
            await db.execute(
                text(
                    """SELECT l.*, v.display_name FROM payout_lines l
                       JOIN vendors v ON v.id = l.vendor_id AND v.tenant_id = l.tenant_id
                       WHERE l.tenant_id = :t AND l.batch_id = :b AND l.method = :m
                         AND l.status = 'pending' ORDER BY v.display_name"""
                ),
                {"t": tenant_id, "b": batch_id, "m": method},
            )
        )
        .mappings()
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    if method == "bank":
        writer.writerow(BANK_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row["account_name"] or row["display_name"],
                    row["account_last4"] or "",
                    f"{money(row['net']):.2f}",
                    f"PAYOUT-{batch['period_end']}-{str(row['id'])[:8]}",
                    "marketplace settlement",
                ]
            )
    else:
        writer.writerow(BKASH_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row["account_last4"] or "",
                    row["account_name"] or row["display_name"],
                    f"{money(row['net']):.2f}",
                    f"PAYOUT-{batch['period_end']}-{str(row['id'])[:8]}",
                ]
            )
    await db.execute(
        text(
            "UPDATE payout_batches SET status = 'exported', exported_at = now() "
            "WHERE tenant_id = :t AND id = :i AND status = 'approved'"
        ),
        {"t": tenant_id, "i": batch_id},
    )
    return buf.getvalue()


async def mark_paid(
    db: AsyncSession, tenant_id: str, line_id, *, reference: str, actor_id: str
) -> dict:
    """The money left the bank. Re-marking is a no-op, so a double click costs nothing."""
    line = (
        (
            await db.execute(
                text("SELECT * FROM payout_lines WHERE tenant_id = :t AND id = :i FOR UPDATE"),
                {"t": tenant_id, "i": line_id},
            )
        )
        .mappings()
        .first()
    )
    if line is None:
        raise NotFound("Not found")
    if line["status"] == "paid":
        return {"id": str(line_id), "status": "paid", "already": True}
    if line["status"] == "cancelled":
        raise PayoutError("This payout line was cancelled", code="bad_state")
    batch = await _batch(db, tenant_id, line["batch_id"])
    if batch["status"] not in ("approved", "exported"):
        raise PayoutError("This batch has not been approved", code="bad_state")
    await db.execute(
        text(
            "UPDATE payout_lines SET status = 'paid', reference = :r, paid_at = now(), "
            "failure_reason = NULL WHERE tenant_id = :t AND id = :i"
        ),
        {"r": reference, "t": tenant_id, "i": line_id},
    )
    await ledger.post_payout(
        db,
        tenant_id,
        line_id=line_id,
        vendor_id=str(line["vendor_id"]),
        gross=line["gross"],
        tds=line["tds"],
        net=line["net"],
    )
    remaining = (
        await db.execute(
            text(
                "SELECT count(*) FROM payout_lines WHERE tenant_id = :t AND batch_id = :b "
                "AND status = 'pending'"
            ),
            {"t": tenant_id, "b": line["batch_id"]},
        )
    ).scalar()
    if not remaining:
        await db.execute(
            text(
                "UPDATE payout_batches SET status = 'completed', completed_at = now() "
                "WHERE tenant_id = :t AND id = :b"
            ),
            {"t": tenant_id, "b": line["batch_id"]},
        )
    return {"id": str(line_id), "status": "paid", "already": False}


async def mark_failed(
    db: AsyncSession, tenant_id: str, line_id, *, reason: str, actor_id: str
) -> dict:
    res = await db.execute(
        text(
            "UPDATE payout_lines SET status = 'failed', failure_reason = :r "
            "WHERE tenant_id = :t AND id = :i AND status = 'pending'"
        ),
        {"r": reason[:300], "t": tenant_id, "i": line_id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    return {"id": str(line_id), "status": "failed"}


def period_end_for(schedule: str, today: date) -> date:
    """The period a payout run closes on: last Saturday for weekly, month end for monthly."""
    from datetime import timedelta

    if schedule == "monthly":
        return today.replace(day=1) - timedelta(days=1)
    days = 7 if schedule == "weekly" else 14
    offset = (today.weekday() - 5) % 7 or 7  # Saturday closes the week in Bangladesh
    end = today - timedelta(days=offset)
    return end if schedule == "weekly" else end - timedelta(days=days - 7)
