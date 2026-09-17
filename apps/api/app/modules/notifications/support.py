"""Support tickets: one thread per question, with the first-response clock running from creation.

Internal notes live on the same thread as the customer's messages but are never returned to the
customer — one story for staff, one for the person waiting.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound
from app.modules.trust import filters

OPEN_STATUSES = ("open", "pending_customer", "pending_staff")


class TicketError(AppError):
    status = 409
    code = "ticket_conflict"


async def create_ticket(
    db: AsyncSession,
    tenant_id: str,
    *,
    subject: str,
    body: str,
    category: str,
    user_id: str | None = None,
    vendor_id: str | None = None,
    order_number: str | None = None,
) -> dict:
    order_id = None
    if order_number:
        order_id = (
            await db.execute(
                text(
                    """SELECT id FROM orders WHERE tenant_id = :t AND number = :n
                       AND (CAST(:u AS uuid) IS NULL OR user_id = CAST(:u AS uuid))"""
                ),
                {"t": tenant_id, "n": order_number, "u": user_id},
            )
        ).scalar()
        if order_id is None:
            raise NotFound("Order not found")
    number = (
        await db.execute(
            text(
                """UPDATE tenant_settings SET next_ticket_number = next_ticket_number + 1
                   WHERE tenant_id = :t RETURNING 'TKT-' || (next_ticket_number - 1)"""
            ),
            {"t": tenant_id},
        )
    ).scalar()
    ticket_id = (
        await db.execute(
            text(
                """INSERT INTO support_tickets (tenant_id, number, user_id, vendor_id, order_id,
                       subject, category)
                   VALUES (:t, :n, :u, :v, :o, :s, :c) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "n": number,
                "u": user_id,
                "v": vendor_id,
                "o": order_id,
                "s": subject,
                "c": category,
            },
        )
    ).scalar()
    await add_message(
        db,
        tenant_id,
        ticket_id,
        author_kind="vendor" if vendor_id else "customer",
        author_id=vendor_id or user_id,
        body=body,
        initial=True,
    )
    return {"id": str(ticket_id), "number": number, "status": "open"}


async def add_message(
    db: AsyncSession,
    tenant_id: str,
    ticket_id,
    *,
    author_kind: str,
    author_id: str | None,
    body: str,
    internal: bool = False,
    initial: bool = False,
) -> dict:
    ticket = (
        (
            await db.execute(
                text(
                    "SELECT status, first_response_at FROM support_tickets WHERE tenant_id = :t AND id = :i"
                ),
                {"t": tenant_id, "i": ticket_id},
            )
        )
        .mappings()
        .first()
    )
    if ticket is None:
        raise NotFound("Not found")
    if ticket["status"] == "closed":
        raise TicketError("This ticket is closed", code="closed")
    cleaned = body if author_kind == "staff" else filters.redact(body)[0]
    await db.execute(
        text(
            """INSERT INTO ticket_messages (tenant_id, ticket_id, author_kind, author_id, body, internal)
               VALUES (:t, :i, :k, :a, :b, :int)"""
        ),
        {
            "t": tenant_id,
            "i": ticket_id,
            "k": author_kind,
            "a": author_id,
            "b": cleaned,
            "int": internal,
        },
    )
    if internal or initial:
        # The message a ticket is born with does not move it: a new ticket is simply open.
        return {"status": ticket["status"], "internal": internal}
    status = "pending_customer" if author_kind == "staff" else "pending_staff"
    await db.execute(
        text(
            """UPDATE support_tickets SET status = :s, updated_at = now(),
                      first_response_at = CASE WHEN :staff AND first_response_at IS NULL
                                               THEN now() ELSE first_response_at END
               WHERE tenant_id = :t AND id = :i"""
        ),
        {"s": status, "staff": author_kind == "staff", "t": tenant_id, "i": ticket_id},
    )
    return {"status": status, "internal": False}


async def set_status(
    db: AsyncSession, tenant_id: str, ticket_id, *, status: str, assignee_id: str | None = None
) -> dict:
    res = await db.execute(
        text(
            """UPDATE support_tickets SET status = :s, updated_at = now(),
                      assignee_id = COALESCE(CAST(:a AS uuid), assignee_id),
                      resolved_at = CASE WHEN :s IN ('resolved','closed') THEN now() ELSE NULL END
               WHERE tenant_id = :t AND id = :i"""
        ),
        {"s": status, "a": assignee_id, "t": tenant_id, "i": ticket_id},
    )
    if res.rowcount == 0:
        raise NotFound("Not found")
    return {"id": str(ticket_id), "status": status}


async def thread(db: AsyncSession, tenant_id: str, ticket_id, *, include_internal: bool) -> dict:
    ticket = (
        (
            await db.execute(
                text(
                    """SELECT t.*, o.number AS order_number FROM support_tickets t
                       LEFT JOIN orders o ON o.id = t.order_id AND o.tenant_id = t.tenant_id
                       WHERE t.tenant_id = :t AND t.id = :i"""
                ),
                {"t": tenant_id, "i": ticket_id},
            )
        )
        .mappings()
        .first()
    )
    if ticket is None:
        raise NotFound("Not found")
    messages = (
        (
            await db.execute(
                text(
                    """SELECT author_kind, body, internal, created_at FROM ticket_messages
                       WHERE tenant_id = :t AND ticket_id = :i AND (:inc OR NOT internal)
                       ORDER BY created_at"""
                ),
                {"t": tenant_id, "i": ticket_id, "inc": include_internal},
            )
        )
        .mappings()
        .all()
    )
    return {
        **{k: v for k, v in ticket.items() if k != "tenant_id"},
        "messages": [dict(m) for m in messages],
    }
