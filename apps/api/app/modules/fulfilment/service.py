"""Fulfilment: one shipment per sub-order, courier chosen by rules, status driven by events.

Two invariants worth stating plainly:

* **A parcel's status only ever comes from the courier.** Vendors mark goods ready; they cannot
  declare a parcel delivered, because delivery is what settles COD money.
* **An unknown courier word never becomes a status.** It is stored, the shipment is flagged for
  ops, and nothing downstream moves.

Stock: prepaid stock was consumed when the payment settled (Phase 11). COD stock is consumed when
the courier picks the parcel up — that is when the goods physically leave the vendor. A return to
merchant puts it back with an append-only movement.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Encryptor
from app.core.errors import AppError, NotFound
from app.modules.fulfilment.couriers import (
    PROGRESS,
    TERMINAL,
    Address,
    CourierError,
    CourierEvent,
    build_courier,
)
from app.modules.payments import service as payments

# normalised shipment status -> sub-order status ("" = leave the sub-order alone)
SUB_ORDER_STATUS = {
    "booked": "ready_to_ship",
    "picked_up": "shipped",
    "in_transit": "shipped",
    "out_for_delivery": "shipped",
    "failed_attempt": "shipped",
    "returning": "shipped",
    "delivered": "delivered",
    "partial_delivered": "delivered",
    "returned": "returned",
    "cancelled": "",
}
BOOKABLE = ("confirmed", "processing", "ready_to_ship")


class FulfilmentError(AppError):
    status = 409
    code = "fulfilment_conflict"


@dataclass
class CourierAccount:
    courier: str
    mode: str
    credentials: dict
    pickup_ref: str | None
    vendor_id: str | None
    status: str


def _context(tenant_id: str, courier: str, vendor_id: str | None) -> str:
    return f"courier_account:{tenant_id}:{vendor_id or 'tenant'}:{courier}"


def encrypt_credentials(settings, tenant_id, courier, vendor_id, credentials: dict) -> str:
    return Encryptor(settings.data_encryption_key).encrypt(
        credentials, context=_context(tenant_id, courier, vendor_id)
    )


async def load_account(
    db: AsyncSession, settings, tenant_id: str, courier: str, vendor_id: str | None = None
) -> CourierAccount:
    """A vendor's own account wins when the tenant allowed it; otherwise the tenant's account."""
    row = (
        (
            await db.execute(
                text(
                    """SELECT * FROM courier_accounts
                       WHERE tenant_id = :t AND courier = :c
                         AND (vendor_id = CAST(:v AS uuid) OR vendor_id IS NULL)
                       ORDER BY vendor_id NULLS LAST LIMIT 1"""
                ),
                {"t": tenant_id, "c": courier, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FulfilmentError("This courier is not configured", code="courier_unconfigured")
    if row["status"] == "disabled":
        raise FulfilmentError("This courier is unavailable", code="courier_disabled")
    credentials = Encryptor(settings.data_encryption_key).decrypt(
        row["credentials_ciphertext"],
        context=_context(tenant_id, courier, str(row["vendor_id"]) if row["vendor_id"] else None),
    )
    return CourierAccount(
        courier=row["courier"],
        mode=row["mode"],
        credentials=credentials,
        pickup_ref=row["pickup_ref"],
        vendor_id=str(row["vendor_id"]) if row["vendor_id"] else None,
        status=row["status"],
    )


async def select_courier(
    db: AsyncSession,
    tenant_id: str,
    *,
    district: str,
    zone: str | None,
    weight_grams: int,
    cod_amount: Decimal,
) -> str:
    """First enabled rule that fits wins; rules are ordered by the tenant's own priority."""
    row = (
        await db.execute(
            text(
                """SELECT courier FROM courier_rules
                   WHERE tenant_id = :t AND enabled
                     AND (cardinality(districts) = 0 OR :d = ANY(districts))
                     AND (cardinality(zones) = 0 OR :z = ANY(zones))
                     AND (max_weight_grams IS NULL OR max_weight_grams >= :w)
                     AND (max_cod_amount IS NULL OR max_cod_amount >= :c)
                   ORDER BY priority, created_at LIMIT 1"""
            ),
            {"t": tenant_id, "d": district, "z": zone or "", "w": weight_grams, "c": cod_amount},
        )
    ).first()
    if row:
        return row.courier
    fallback = (
        await db.execute(
            text(
                "SELECT courier FROM courier_accounts WHERE tenant_id = :t AND status <> 'disabled' "
                "ORDER BY (status = 'healthy') DESC, courier LIMIT 1"
            ),
            {"t": tenant_id},
        )
    ).first()
    if fallback is None:
        raise FulfilmentError("No courier is configured for this delivery", code="no_courier")
    return fallback.courier


async def _shipment_context(db: AsyncSession, tenant_id: str, sub_order_id, vendor_id=None):
    row = (
        (
            await db.execute(
                text(
                    """SELECT s.id, s.number, s.status, s.vendor_id, s.order_id, s.total, s.weight_grams,
                              o.number AS order_number, o.payment_method, o.shipping_address,
                              o.contact_phone, d.zone
                       FROM sub_orders s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       LEFT JOIN geo_districts d ON d.code = o.shipping_address->>'district_code'
                       WHERE s.tenant_id = :t AND s.id = :s
                         AND (CAST(:v AS uuid) IS NULL OR s.vendor_id = CAST(:v AS uuid))"""
                ),
                {"t": tenant_id, "s": sub_order_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    return row


async def mark_ready(db: AsyncSession, tenant_id: str, sub_order_id, vendor_id: str) -> str:
    """Vendor says the goods are packed. Everything after this belongs to the courier."""
    sub = await _shipment_context(db, tenant_id, sub_order_id, vendor_id)
    if sub["status"] not in ("confirmed", "processing"):
        raise FulfilmentError("This shipment is not awaiting packing", code="bad_state")
    await db.execute(
        text(
            "UPDATE sub_orders SET status = 'processing', updated_at = now() "
            "WHERE tenant_id = :t AND id = :s AND status = 'confirmed'"
        ),
        {"t": tenant_id, "s": sub_order_id},
    )
    return "processing"


async def book_shipment(
    db: AsyncSession,
    settings,
    tenant_id: str,
    *,
    sub_order_id,
    vendor_id: str | None,
    courier: str | None = None,
    note: str | None = None,
    overrides: dict | None = None,
) -> dict:
    sub = await _shipment_context(db, tenant_id, sub_order_id, vendor_id)
    if sub["status"] not in BOOKABLE:
        raise FulfilmentError(
            "This shipment cannot be booked in its current state", code="bad_state"
        )
    existing = (
        await db.execute(
            text(
                "SELECT id, courier, consignment_id FROM shipments WHERE tenant_id = :t AND sub_order_id = :s "
                "AND status <> 'cancelled'"
            ),
            {"t": tenant_id, "s": sub_order_id},
        )
    ).first()
    if existing:
        raise FulfilmentError("This shipment is already booked", code="already_booked")

    address = sub["shipping_address"]
    cod_amount = Decimal(str(sub["total"])) if sub["payment_method"] == "cod" else Decimal("0")
    courier = courier or await select_courier(
        db,
        tenant_id,
        district=address.get("district_code", ""),
        zone=sub["zone"],
        weight_grams=sub["weight_grams"],
        cod_amount=cod_amount,
    )
    account = await load_account(db, settings, tenant_id, courier, str(sub["vendor_id"]))
    adapter = build_courier(courier, account.mode, overrides)
    area_code = (
        await db.execute(
            text(
                """SELECT area_code FROM geo_courier_areas
                   WHERE courier = :c AND district_code = :d AND lower(area_name) = lower(:a)"""
            ),
            {"c": courier, "d": address.get("district_code", ""), "a": address.get("area") or ""},
        )
    ).scalar()
    to = Address(
        name=address.get("recipient_name", ""),
        phone=address.get("phone") or sub["contact_phone"],
        district=address.get("district_code", ""),
        upazila=address.get("upazila"),
        area=area_code or address.get("area"),
        line=address.get("address_line", ""),
    )
    booking = await adapter.book(
        credentials=account.credentials,
        pickup_ref=account.pickup_ref,
        to=to,
        reference=sub["number"],
        weight_grams=sub["weight_grams"],
        cod_amount=cod_amount,
        note=note,
    )
    shipment_id = (
        await db.execute(
            text(
                """INSERT INTO shipments (tenant_id, vendor_id, order_id, sub_order_id, courier, consignment_id,
                       tracking_code, tracking_url, cod_amount, delivery_fee, weight_grams, status, last_event_at)
                   VALUES (:t, :v, :o, :s, :c, :cid, :tc, :tu, :cod, :fee, :w, 'booked', now())
                   RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": sub["vendor_id"],
                "o": sub["order_id"],
                "s": sub_order_id,
                "c": courier,
                "cid": booking.consignment_id,
                "tc": booking.tracking_code,
                "tu": booking.tracking_url,
                "cod": cod_amount,
                "fee": booking.delivery_fee,
                "w": sub["weight_grams"],
            },
        )
    ).scalar()
    await db.execute(
        text(
            "UPDATE sub_orders SET status = 'ready_to_ship', updated_at = now() "
            "WHERE tenant_id = :t AND id = :s"
        ),
        {"t": tenant_id, "s": sub_order_id},
    )
    if cod_amount > 0:
        await db.execute(
            text(
                "UPDATE cod_receivables SET shipment_id = :sh, courier = :c "
                "WHERE tenant_id = :t AND sub_order_id = :s"
            ),
            {"sh": shipment_id, "c": courier, "t": tenant_id, "s": sub_order_id},
        )
    return {
        "id": str(shipment_id),
        "courier": courier,
        "consignment_id": booking.consignment_id,
        "tracking_code": booking.tracking_code,
        "tracking_url": booking.tracking_url,
        "status": "booked",
        "cod_amount": cod_amount,
    }


async def cancel_shipment(
    db: AsyncSession, settings, tenant_id: str, shipment_id, *, vendor_id=None, overrides=None
) -> dict:
    row = (
        (
            await db.execute(
                text(
                    """SELECT * FROM shipments WHERE tenant_id = :t AND id = :i
                       AND (CAST(:v AS uuid) IS NULL OR vendor_id = CAST(:v AS uuid))"""
                ),
                {"t": tenant_id, "i": shipment_id, "v": vendor_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound("Not found")
    if row["status"] in TERMINAL:
        raise FulfilmentError("This parcel can no longer be cancelled", code="bad_state")
    account = await load_account(db, settings, tenant_id, row["courier"], str(row["vendor_id"]))
    adapter = build_courier(row["courier"], account.mode, overrides)
    try:
        await adapter.cancel(credentials=account.credentials, consignment_id=row["consignment_id"])
    except CourierError as exc:
        raise FulfilmentError(str(exc), code="courier_refused") from exc
    await db.execute(
        text(
            "UPDATE shipments SET status = 'cancelled', cancelled_at = now(), last_event_at = now() "
            "WHERE tenant_id = :t AND id = :i"
        ),
        {"t": tenant_id, "i": shipment_id},
    )
    await db.execute(
        text(
            "UPDATE sub_orders SET status = 'processing', updated_at = now() "
            "WHERE tenant_id = :t AND id = :s AND status = 'ready_to_ship'"
        ),
        {"t": tenant_id, "s": row["sub_order_id"]},
    )
    return {"id": str(shipment_id), "status": "cancelled"}


# ------------------------------------------------------------------------------------- events
async def apply_event(
    db: AsyncSession, tenant_id: str, *, courier: str, event: CourierEvent
) -> dict:
    """Store the event once, then move the shipment forward only if it really is forward."""
    shipment = (
        (
            await db.execute(
                text(
                    """SELECT s.*, o.payment_method, o.number AS order_number FROM shipments s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.courier = :c AND s.consignment_id = :cid
                       FOR UPDATE OF s"""
                ),
                {"t": tenant_id, "c": courier, "cid": event.consignment_id},
            )
        )
        .mappings()
        .first()
    )
    stored = await db.execute(
        text(
            """INSERT INTO shipment_events (tenant_id, shipment_id, courier, event_id, raw_status, status,
                   payload, occurred_at)
               VALUES (:t, :s, :c, :e, :raw, :st, CAST(:p AS jsonb), :at)
               ON CONFLICT (tenant_id, courier, event_id) DO NOTHING RETURNING id"""
        ),
        {
            "t": tenant_id,
            "s": shipment["id"] if shipment else None,
            "c": courier,
            "e": event.event_id,
            "raw": event.raw_status or "",
            "st": event.status,
            "p": _json(event.payload),
            "at": event.occurred_at,
        },
    )
    if stored.first() is None:
        return {"status": "duplicate"}
    if shipment is None:
        return {"status": "unknown_consignment"}
    if event.status is None:
        await db.execute(
            text(
                """UPDATE shipments SET needs_attention = true, attention_reason = :r,
                          status_detail = :d, last_event_at = now()
                   WHERE tenant_id = :t AND id = :i"""
            ),
            {
                "r": f"unmapped courier status: {event.raw_status}"[:200],
                "d": event.raw_status,
                "t": tenant_id,
                "i": shipment["id"],
            },
        )
        return {"status": "needs_attention", "shipment_id": str(shipment["id"])}

    current, new = shipment["status"], event.status
    if current in TERMINAL or PROGRESS[new] <= PROGRESS[current]:
        await db.execute(
            text("UPDATE shipments SET last_event_at = now() WHERE tenant_id = :t AND id = :i"),
            {"t": tenant_id, "i": shipment["id"]},
        )
        return {"status": "stale", "shipment_id": str(shipment["id"]), "current": current}

    stamps = {
        "picked_up": "picked_up_at",
        "delivered": "delivered_at",
        "partial_delivered": "delivered_at",
        "returned": "returned_at",
        "cancelled": "cancelled_at",
    }
    sets = ["status = :st", "status_detail = :d", "last_event_at = now()"]
    if new in stamps:
        sets.append(f"{stamps[new]} = coalesce(:at, now())")
    if new == "failed_attempt":
        sets.append("failed_attempts = failed_attempts + 1")
    await db.execute(
        text(
            "UPDATE shipments SET " + ", ".join(sets) + " WHERE tenant_id = :t AND id = :i"  # noqa: S608 - fixed column names
        ),
        {
            "st": new,
            "d": event.raw_status,
            "at": event.occurred_at,
            "t": tenant_id,
            "i": shipment["id"],
        },
    )
    sub_status = SUB_ORDER_STATUS.get(new, "")
    if sub_status:
        await db.execute(
            text(
                "UPDATE sub_orders SET status = :s, updated_at = now() "
                "WHERE tenant_id = :t AND id = :i AND status <> :s"
            ),
            {"s": sub_status, "t": tenant_id, "i": shipment["sub_order_id"]},
        )
    if PROGRESS[new] >= PROGRESS["picked_up"] and shipment["payment_method"] == "cod":
        # COD goods leave the shelf once the courier has them. Couriers skip statuses, so this
        # fires on the first event at or past pick-up; consuming reservations is idempotent.
        await payments.consume_reservations(
            db, tenant_id, shipment["order_id"], ref=shipment["order_number"]
        )
    if new in ("delivered", "partial_delivered") and shipment["payment_method"] == "cod":
        await payments.collect_cod(db, tenant_id, shipment["sub_order_id"], courier=courier)
    if new == "returned":
        await _restock(db, tenant_id, shipment)
    return {"status": new, "shipment_id": str(shipment["id"])}


async def _restock(db: AsyncSession, tenant_id: str, shipment) -> None:
    """A parcel that came back to the vendor is stock again — with a movement, never silently."""
    rows = (
        await db.execute(
            text(
                """SELECT i.variant_id, i.vendor_id, sum(i.qty) AS qty FROM order_items i
                   WHERE i.tenant_id = :t AND i.sub_order_id = :s
                   GROUP BY i.variant_id, i.vendor_id ORDER BY i.variant_id"""
            ),
            {"t": tenant_id, "s": shipment["sub_order_id"]},
        )
    ).all()
    consumed = (
        await db.execute(
            text(
                "SELECT count(*) FROM stock_reservations WHERE tenant_id = :t AND sub_order_id = :s "
                "AND consumed_at IS NOT NULL"
            ),
            {"t": tenant_id, "s": shipment["sub_order_id"]},
        )
    ).scalar()
    if not consumed:
        return  # the goods never left: nothing to put back
    for variant_id, vendor_id, qty in rows:
        balance = (
            await db.execute(
                text(
                    "UPDATE product_variants SET stock_on_hand = stock_on_hand + :q, updated_at = now() "
                    "WHERE tenant_id = :t AND id = :v RETURNING stock_on_hand"
                ),
                {"q": int(qty), "t": tenant_id, "v": variant_id},
            )
        ).scalar()
        await db.execute(
            text(
                """INSERT INTO inventory_movements (tenant_id, vendor_id, variant_id, delta, balance_after,
                       reason, ref, actor_id)
                   VALUES (:t, :vend, :v, :d, :b, 'return', :ref, 'courier')"""
            ),
            {
                "t": tenant_id,
                "vend": vendor_id,
                "v": variant_id,
                "d": int(qty),
                "b": balance,
                "ref": shipment["order_number"],
            },
        )
    await db.execute(
        text(
            "UPDATE cod_receivables SET status = 'cancelled' WHERE tenant_id = :t AND sub_order_id = :s "
            "AND status = 'due'"
        ),
        {"t": tenant_id, "s": shipment["sub_order_id"]},
    )


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload, default=str)


async def poll_open_shipments(
    db: AsyncSession,
    settings,
    tenant_id: str,
    *,
    stale_minutes: int = 30,
    limit: int = 100,
    overrides: dict | None = None,
) -> dict:
    """Webhooks get lost. Every non-terminal parcel is asked directly, on a schedule."""
    rows = (
        (
            await db.execute(
                text(
                    """SELECT id, courier, consignment_id, vendor_id FROM shipments
                       WHERE tenant_id = :t AND status NOT IN ('delivered','returned','cancelled')
                         AND (last_event_at IS NULL OR last_event_at < now() - make_interval(mins => :m))
                       ORDER BY last_event_at NULLS FIRST LIMIT :l"""
                ),
                {"t": tenant_id, "m": stale_minutes, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    out = {"polled": 0, "advanced": 0, "errors": 0}
    accounts: dict[str, CourierAccount] = {}
    for row in rows:
        try:
            key = f"{row['courier']}:{row['vendor_id']}"
            if key not in accounts:
                accounts[key] = await load_account(
                    db, settings, tenant_id, row["courier"], str(row["vendor_id"])
                )
            account = accounts[key]
            adapter = build_courier(row["courier"], account.mode, overrides)
            event = await adapter.track(
                credentials=account.credentials, consignment_id=row["consignment_id"]
            )
            result = await apply_event(db, tenant_id, courier=row["courier"], event=event)
        except Exception:
            out["errors"] += 1
            continue
        out["polled"] += 1
        if result["status"] not in ("duplicate", "stale", "unknown_consignment"):
            out["advanced"] += 1
    return out


async def flag_stuck_shipments(db: AsyncSession, tenant_id: str, *, sla_hours: int = 72) -> int:
    """Nothing has happened to this parcel for far too long: a human needs to chase the courier."""
    res = await db.execute(
        text(
            """UPDATE shipments SET needs_attention = true,
                      attention_reason = 'no courier update within SLA'
               WHERE tenant_id = :t AND status NOT IN ('delivered','returned','cancelled')
                 AND NOT needs_attention
                 AND coalesce(last_event_at, booked_at) < now() - make_interval(hours => :h)"""
        ),
        {"t": tenant_id, "h": sla_hours},
    )
    return res.rowcount


# -------------------------------------------------------------------------------- settlements
async def import_settlement(
    db: AsyncSession,
    tenant_id: str,
    *,
    courier: str,
    statement_ref: str,
    lines: list[dict],
    actor_id: str,
    period_start=None,
    period_end=None,
) -> dict:
    """Match a courier's COD statement to our shipments. Nothing is overwritten to make it fit."""
    settlement_id = (
        await db.execute(
            text(
                """INSERT INTO courier_settlements (tenant_id, courier, statement_ref, period_start,
                       period_end, imported_by)
                   VALUES (:t, :c, :r, :ps, :pe, :a)
                   ON CONFLICT (tenant_id, courier, statement_ref) DO NOTHING RETURNING id"""
            ),
            {
                "t": tenant_id,
                "c": courier,
                "r": statement_ref,
                "ps": period_start,
                "pe": period_end,
                "a": actor_id,
            },
        )
    ).scalar()
    if settlement_id is None:
        raise FulfilmentError("This statement was already imported", code="already_imported")
    totals = {
        "matched": 0,
        "unmatched": 0,
        "mismatch": 0,
        "amount": Decimal("0"),
        "fee": Decimal("0"),
    }
    seen: set[str] = set()
    for line in lines:
        consignment = str(line["consignment_id"]).strip()
        amount = Decimal(str(line["amount"]))
        fee = Decimal(str(line.get("fee") or 0))
        if consignment in seen:
            await _line(db, tenant_id, settlement_id, consignment, amount, fee, None, "duplicate")
            continue
        seen.add(consignment)
        totals["amount"] += amount
        totals["fee"] += fee
        shipment = (
            (
                await db.execute(
                    text(
                        """SELECT id, sub_order_id, cod_amount, status FROM shipments
                           WHERE tenant_id = :t AND courier = :c AND consignment_id = :cid"""
                    ),
                    {"t": tenant_id, "c": courier, "cid": consignment},
                )
            )
            .mappings()
            .first()
        )
        if shipment is None:
            totals["unmatched"] += 1
            await _line(db, tenant_id, settlement_id, consignment, amount, fee, None, "unmatched")
            continue
        if Decimal(str(shipment["cod_amount"])) != amount:
            totals["mismatch"] += 1
            await _line(
                db,
                tenant_id,
                settlement_id,
                consignment,
                amount,
                fee,
                shipment["id"],
                "mismatch",
                note=f"expected {shipment['cod_amount']}, statement says {amount}",
            )
            await db.execute(
                text(
                    "UPDATE shipments SET needs_attention = true, attention_reason = 'COD settlement mismatch' "
                    "WHERE tenant_id = :t AND id = :i"
                ),
                {"t": tenant_id, "i": shipment["id"]},
            )
            continue
        totals["matched"] += 1
        await _line(
            db, tenant_id, settlement_id, consignment, amount, fee, shipment["id"], "matched"
        )
        from app.modules.ledger import service as ledger

        await ledger.post_cod_settlement(
            db,
            tenant_id,
            shipment_id=shipment["id"],
            courier=courier,
            amount=amount,
            fee=fee,
        )
        await db.execute(
            text(
                """UPDATE cod_receivables SET status = 'settled', settled_at = now(), settlement_ref = :r
                   WHERE tenant_id = :t AND sub_order_id = :s AND status IN ('collected','due')"""
            ),
            {"r": statement_ref, "t": tenant_id, "s": shipment["sub_order_id"]},
        )
    await db.execute(
        text(
            """UPDATE courier_settlements SET total_amount = :a, total_fee = :f, matched_count = :m,
                      unmatched_count = :u, mismatch_count = :x
               WHERE tenant_id = :t AND id = :i"""
        ),
        {
            "a": totals["amount"],
            "f": totals["fee"],
            "m": totals["matched"],
            "u": totals["unmatched"],
            "x": totals["mismatch"],
            "t": tenant_id,
            "i": settlement_id,
        },
    )
    return {
        "id": str(settlement_id),
        "courier": courier,
        "statement_ref": statement_ref,
        "matched": totals["matched"],
        "unmatched": totals["unmatched"],
        "mismatch": totals["mismatch"],
        "total_amount": totals["amount"],
        "total_fee": totals["fee"],
    }


async def _line(
    db, tenant_id, settlement_id, consignment, amount, fee, shipment_id, status, note=None
):
    await db.execute(
        text(
            """INSERT INTO courier_settlement_lines (tenant_id, settlement_id, consignment_id, amount, fee,
                   shipment_id, status, note)
               VALUES (:t, :s, :c, :a, :f, :sh, :st, :n)"""
        ),
        {
            "t": tenant_id,
            "s": settlement_id,
            "c": consignment,
            "a": amount,
            "f": fee,
            "sh": shipment_id,
            "st": status,
            "n": note,
        },
    )


def parse_statement_csv(raw: bytes) -> list[dict]:
    """Couriers all ship a different CSV; these are the columns every one of them has."""
    import csv
    import io

    text_ = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text_))
    aliases = {
        "consignment_id": ("consignment_id", "consignment", "tracking_id", "tracking_code", "cid"),
        "amount": ("amount", "cod_amount", "collected_amount", "cash_collected"),
        "fee": ("fee", "delivery_fee", "charge", "courier_fee"),
    }
    lines = []
    for row in reader:
        lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        out = {}
        for field_, names in aliases.items():
            for name in names:
                if lower.get(name):
                    out[field_] = lower[name]
                    break
        if out.get("consignment_id") and out.get("amount"):
            lines.append(out)
    if not lines:
        raise FulfilmentError(
            "No consignment rows found in this statement", status=422, code="bad_statement"
        )
    return lines


def utcnow() -> datetime:
    return datetime.now(UTC)
