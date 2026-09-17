"""Vendor state machine (ADR 0010). Every transition writes an append-only vendor_events row and
an audit entry. Invalid transitions are 409, never silently ignored."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.errors import Conflict, NotFound
from app.core.security import Principal

# to_status -> (allowed from statuses, who may perform: vendor|staff)
TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "documents_submitted": ({"registered", "changes_requested"}, "vendor"),
    "under_review": ({"documents_submitted"}, "system"),
    "changes_requested": ({"under_review"}, "staff"),
    "approved": ({"under_review", "suspended"}, "staff"),
    "rejected": ({"under_review", "documents_submitted"}, "staff"),
    "suspended": ({"approved"}, "staff"),
    "closed": ({"approved", "suspended", "rejected", "registered", "changes_requested"}, "staff"),
}

TIMESTAMP_COLUMNS = {"approved": "approved_at", "suspended": "suspended_at"}


async def transition(
    db: AsyncSession,
    *,
    tenant_id: str,
    vendor_id,
    to: str,
    actor: Principal | None,
    performer: str,
    reason: str | None = None,
    request=None,
) -> str:
    if to not in TRANSITIONS:
        raise Conflict("Unknown status")
    allowed_from, who = TRANSITIONS[to]
    if who != performer:
        raise Conflict("This change is not allowed")
    row = (
        await db.execute(
            text("SELECT status FROM vendors WHERE id = :v AND tenant_id = :t FOR UPDATE"),
            {"v": str(vendor_id), "t": tenant_id},
        )
    ).first()
    if row is None:
        raise NotFound("Not found")
    current = row.status
    if current not in allowed_from:
        raise Conflict(f"Cannot move a vendor from {current} to {to}")
    ts = TIMESTAMP_COLUMNS.get(to)
    extra = f", {ts} = now()" if ts else ""
    await db.execute(
        text(
            f"UPDATE vendors SET status = :to, status_reason = :r, updated_at = now(){extra} "  # noqa: S608
            "WHERE id = :v AND tenant_id = :t"
        ),
        {"to": to, "r": reason, "v": str(vendor_id), "t": tenant_id},
    )
    await db.execute(
        text("""INSERT INTO vendor_events (tenant_id, vendor_id, from_status, to_status, reason, actor_kind, actor_id)
                VALUES (:t, :v, :f, :to, :r, :ak, :aid)"""),
        {
            "t": tenant_id,
            "v": str(vendor_id),
            "f": current,
            "to": to,
            "r": reason,
            "ak": actor.kind if actor else "system",
            "aid": actor.sub if actor else "system",
        },
    )
    await audit.record(
        db,
        tenant_id=tenant_id,
        actor=actor,
        action=f"vendor.{to}",
        entity="vendor",
        entity_id=vendor_id,
        data={"from": current, "reason": reason},
        request=request,
    )
    return current
