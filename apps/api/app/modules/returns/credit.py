"""Store credit: a buyer's balance in one tenant's shop, with an append-only history.

The balance column is a cache of the entries, written in the same transaction and under a row
lock, so two concurrent spends can never both see the same taka.
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.modules.checkout.pricing import money

GRANT_REASONS = ("return_refund", "goodwill", "adjustment", "order_cancelled")
SPEND_REASONS = ("order_payment", "expiry", "adjustment")


class CreditError(AppError):
    status = 409
    code = "insufficient_credit"


async def balance(db: AsyncSession, tenant_id: str, user_id: str) -> Decimal:
    value = (
        await db.execute(
            text("SELECT balance FROM store_credit_accounts WHERE tenant_id = :t AND user_id = :u"),
            {"t": tenant_id, "u": user_id},
        )
    ).scalar()
    return money(value or 0)


async def _move(
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    delta: Decimal,
    *,
    reason: str,
    actor_id: str,
    ref_type: str | None = None,
    ref_id=None,
    note: str | None = None,
    expires_at=None,
) -> Decimal:
    await db.execute(
        text(
            """INSERT INTO store_credit_accounts (tenant_id, user_id, balance) VALUES (:t, :u, 0)
               ON CONFLICT (tenant_id, user_id) DO NOTHING"""
        ),
        {"t": tenant_id, "u": user_id},
    )
    current = (
        await db.execute(
            text(
                "SELECT balance FROM store_credit_accounts WHERE tenant_id = :t AND user_id = :u FOR UPDATE"
            ),
            {"t": tenant_id, "u": user_id},
        )
    ).scalar()
    new_balance = money(Decimal(str(current)) + delta)
    if new_balance < 0:
        raise CreditError("Not enough store credit")
    await db.execute(
        text(
            "UPDATE store_credit_accounts SET balance = :b, updated_at = now() "
            "WHERE tenant_id = :t AND user_id = :u"
        ),
        {"b": new_balance, "t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            """INSERT INTO store_credit_entries (tenant_id, user_id, delta, balance_after, reason,
                   ref_type, ref_id, note, actor_id, expires_at)
               VALUES (:t, :u, :d, :b, :r, :rt, :ri, :n, :a, :e)"""
        ),
        {
            "t": tenant_id,
            "u": user_id,
            "d": money(delta),
            "b": new_balance,
            "r": reason,
            "rt": ref_type,
            "ri": ref_id,
            "n": note,
            "a": actor_id,
            "e": expires_at,
        },
    )
    return new_balance


async def grant(
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    amount: Decimal,
    *,
    reason: str,
    actor_id: str,
    ref_type: str | None = None,
    ref_id=None,
    note: str | None = None,
    expires_at=None,
) -> Decimal:
    if money(amount) <= 0:
        raise AppError("Credit must be positive", status=422, code="invalid_amount")
    return await _move(
        db,
        tenant_id,
        user_id,
        money(amount),
        reason=reason,
        actor_id=actor_id,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
        expires_at=expires_at,
    )


async def spend(
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    amount: Decimal,
    *,
    actor_id: str,
    reason: str = "order_payment",
    ref_type: str | None = None,
    ref_id=None,
) -> Decimal:
    if money(amount) <= 0:
        raise AppError("Amount must be positive", status=422, code="invalid_amount")
    return await _move(
        db,
        tenant_id,
        user_id,
        -money(amount),
        reason=reason,
        actor_id=actor_id,
        ref_type=ref_type,
        ref_id=ref_id,
    )


async def history(db: AsyncSession, tenant_id: str, user_id: str, limit: int = 50) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT delta, balance_after, reason, ref_type, ref_id, note, created_at
                       FROM store_credit_entries WHERE tenant_id = :t AND user_id = :u
                       ORDER BY created_at DESC LIMIT :l"""
                ),
                {"t": tenant_id, "u": user_id, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
