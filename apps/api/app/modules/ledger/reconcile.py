"""Nightly reconciliation: the ledger against the systems it claims to describe (ADR 0003 §7.3.6).

Drift is **reported, never corrected**. A ledger that quietly adjusts itself to match a provider is
worse than one that disagrees loudly: the disagreement is the signal that something upstream went
wrong, and it is the only chance to find out what.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.checkout.pricing import ZERO, money
from app.modules.ledger import accounts as acct
from app.modules.ledger import service as ledger


async def _ledger_accounts(db: AsyncSession, tenant_id: str, prefix: str) -> list[str]:
    """The suffixes (provider or courier) the ledger itself has accounts for."""
    rows = (
        (
            await db.execute(
                text(
                    "SELECT DISTINCT account FROM ledger_entries WHERE tenant_id = :t "
                    "AND account LIKE :p || '%'"
                ),
                {"t": tenant_id, "p": prefix},
            )
        )
        .scalars()
        .all()
    )
    return [a.removeprefix(prefix) for a in rows]


async def reconcile(db: AsyncSession, tenant_id: str) -> dict:
    checks: list[dict] = []

    balance = await ledger.trial_balance(db, tenant_id)
    checks.append(
        {
            "check": "trial_balance",
            "expected": money(balance["total_credits"]),
            "ledger": money(balance["total_debits"]),
            "drift": money(
                Decimal(str(balance["total_debits"])) - Decimal(str(balance["total_credits"]))
            ),
        }
    )

    # What the gateways owe us, per provider, against what we captured and did not refund.
    # The list comes from BOTH sides: a provider that vanished from one of them is exactly the
    # case worth catching, so it must not be able to hide by producing no row.
    providers = dict(
        (p, Decimal(str(n)))
        for p, n in (
            await db.execute(
                text(
                    """SELECT provider, coalesce(sum(amount - refunded_amount), 0) AS net FROM payments
                       WHERE tenant_id = :t AND provider IN ('bkash','sslcommerz')
                         AND status IN ('paid','partially_refunded','refunded')
                       GROUP BY provider"""
                ),
                {"t": tenant_id},
            )
        ).all()
    )
    for row in await _ledger_accounts(db, tenant_id, "gateway_clearing:"):
        providers.setdefault(row, ZERO)
    for provider, net in sorted(providers.items()):
        account = acct.gateway_clearing(provider)
        checks.append(
            {
                "check": account,
                "expected": money(net),
                "ledger": await ledger.account_balance(db, tenant_id, account),
                "drift": money(
                    await ledger.account_balance(db, tenant_id, account) - Decimal(str(net))
                ),
            }
        )

    # What each courier is holding: receivables collected but not yet settled.
    couriers = dict(
        (c, Decimal(str(d)))
        for c, d in (
            await db.execute(
                text(
                    """SELECT courier, coalesce(sum(amount), 0) AS due FROM cod_receivables
                       WHERE tenant_id = :t AND status = 'collected' AND courier IS NOT NULL
                       GROUP BY courier"""
                ),
                {"t": tenant_id},
            )
        ).all()
    )
    for row in await _ledger_accounts(db, tenant_id, "courier_cod_receivable:"):
        couriers.setdefault(row, ZERO)
    for courier, due in sorted(couriers.items()):
        account = acct.cod_receivable(courier)
        in_ledger = await ledger.account_balance(db, tenant_id, account)
        checks.append(
            {
                "check": account,
                "expected": money(due),
                "ledger": in_ledger,
                "drift": money(in_ledger - Decimal(str(due))),
            }
        )

    # What we owe buyers whose refunds finance has not paid yet.
    pending_refunds = (
        await db.execute(
            text(
                """SELECT coalesce(sum(amount), 0) FROM refunds
                   WHERE tenant_id = :t AND status = 'pending'
                     AND method IN ('manual_bank','manual_bkash')"""
            ),
            {"t": tenant_id},
        )
    ).scalar()
    owed = money(-await ledger.account_balance(db, tenant_id, acct.BUYER_REFUND_PAYABLE))
    checks.append(
        {
            "check": acct.BUYER_REFUND_PAYABLE,
            "expected": money(pending_refunds),
            "ledger": owed,
            "drift": money(owed - Decimal(str(pending_refunds))),
        }
    )

    # What buyers hold in their wallets.
    wallets = (
        await db.execute(
            text(
                "SELECT coalesce(sum(balance), 0) FROM store_credit_accounts WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
    ).scalar()
    liability = money(-await ledger.account_balance(db, tenant_id, acct.STORE_CREDIT_LIABILITY))
    checks.append(
        {
            "check": acct.STORE_CREDIT_LIABILITY,
            "expected": money(wallets),
            "ledger": liability,
            "drift": money(liability - Decimal(str(wallets))),
        }
    )

    drifted = [c for c in checks if c["drift"] != ZERO]
    return {"checks": checks, "drift_count": len(drifted), "clean": not drifted}
