"""Account deletion, done properly.

Deleting a person's account cannot mean deleting their orders: the tenant must keep sales records
for tax and the vendor must keep proof of what was sold. So deletion **anonymises the person** —
their identifiers, addresses, device tokens and messages stop pointing at a human — while the
money records keep their numbers and lose their names.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def due_requests(db: AsyncSession, tenant_id: str, *, limit: int = 100) -> list[str]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id FROM account_deletion_requests
                   WHERE tenant_id = :t AND status = 'pending' AND scheduled_for <= now()
                   ORDER BY scheduled_for LIMIT :l"""
                ),
                {"t": tenant_id, "l": limit},
            )
        )
        .scalars()
        .all()
    )
    return [str(r) for r in rows]


async def anonymise_user(
    db: AsyncSession, tenant_id: str, *, request_id, actor_id: str = "system"
) -> bool:
    request = (
        (
            await db.execute(
                text(
                    """SELECT id, user_id FROM account_deletion_requests
                       WHERE tenant_id = :t AND id = :i AND status = 'pending' FOR UPDATE"""
                ),
                {"t": tenant_id, "i": request_id},
            )
        )
        .mappings()
        .first()
    )
    if request is None:
        return False
    user_id = str(request["user_id"])
    marker = f"deleted-{uuid.uuid4().hex[:12]}"

    # The person: identifiers replaced, password and sessions gone.
    await db.execute(
        text(
            """UPDATE users SET email = :email, phone = NULL, full_name = 'Deleted account',
                      password_hash = NULL, status = 'deleted', email_verified_at = NULL,
                      phone_verified_at = NULL
               WHERE tenant_id = :t AND id = CAST(:u AS uuid)"""
        ),
        {"email": f"{marker}@deleted.invalid", "t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE tenant_id = :t "
            "AND user_id = CAST(:u AS uuid) AND revoked_at IS NULL"
        ),
        {"t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            "UPDATE device_tokens SET revoked_at = now() WHERE tenant_id = :t "
            "AND user_id = CAST(:u AS uuid)"
        ),
        {"t": tenant_id, "u": user_id},
    )
    # Their address book and cart go entirely; an order's shipping snapshot is a sales record and stays.
    await db.execute(
        text("DELETE FROM addresses WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)"),
        {"t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            "DELETE FROM cart_items WHERE tenant_id = :t AND cart_id IN "
            "(SELECT id FROM carts WHERE tenant_id = :t AND user_id = CAST(:u AS uuid))"
        ),
        {"t": tenant_id, "u": user_id},
    )
    # Contact details on past orders are scrubbed; numbers, VAT and the ledger are untouched.
    await db.execute(
        text(
            """UPDATE orders SET contact_email = NULL, contact_phone = 'deleted',
                      shipping_address = shipping_address
                        || jsonb_build_object('recipient_name', 'Deleted account',
                                              'phone', 'deleted', 'address_line', 'deleted')
               WHERE tenant_id = :t AND user_id = CAST(:u AS uuid)"""
        ),
        {"t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            "UPDATE reviews SET title = NULL, body = NULL WHERE tenant_id = :t "
            "AND user_id = CAST(:u AS uuid)"
        ),
        {"t": tenant_id, "u": user_id},
    )
    await db.execute(
        text(
            """UPDATE account_deletion_requests SET status = 'completed', completed_at = now()
               WHERE tenant_id = :t AND id = :i"""
        ),
        {"t": tenant_id, "i": request_id},
    )
    await db.execute(
        text(
            """INSERT INTO audit_log (tenant_id, actor_kind, actor_id, action, entity, entity_id, data)
               VALUES (:t, 'system', :a, 'account.anonymised', 'user', :u,
                       jsonb_build_object('marker', CAST(:m AS text)))"""
        ),
        {"t": tenant_id, "a": actor_id, "u": user_id, "m": marker},
    )
    return True


async def run_due_deletions(db: AsyncSession, tenant_id: str) -> int:
    done = 0
    for request_id in await due_requests(db, tenant_id):
        done += 1 if await anonymise_user(db, tenant_id, request_id=request_id) else 0
    return done


def utcnow() -> datetime:
    return datetime.now(UTC)
