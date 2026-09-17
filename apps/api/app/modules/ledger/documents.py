"""Tax documents: a Mushak-6.3-style invoice per shipment and a credit note per refund (ADR 0005).

The seller on the document is the vendor (or the tenant's house vendor in single-vendor mode),
because that is who made the supply. Numbers are per tenant and gapless — a tax document series
with holes is a problem with the NBR, so the counter lives in `tenant_settings` and moves inside
the same transaction as the document row.

PDFs go to the private bucket: a tax invoice names a buyer and an address, and is never public.
"""

import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_keys import object_key
from app.core.errors import NotFound
from app.modules.checkout.pricing import money

TITLES = {"invoice": "Tax Invoice (Mushak 6.3)", "credit_note": "Credit Note (Mushak 6.8)"}


async def _document_data(db: AsyncSession, tenant_id: str, sub_order_id) -> dict:
    head = (
        (
            await db.execute(
                text(
                    """SELECT s.number AS shipment_number, s.items_subtotal, s.discount_total,
                              s.shipping_fee, s.vat_total, s.total, s.vendor_id,
                              o.number AS order_number, o.placed_at, o.vat_pricing, o.shipping_address,
                              o.contact_phone, v.display_name AS vendor_name, t.name AS tenant_name,
                              ts.vat_bin
                       FROM sub_orders s
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       JOIN vendors v ON v.id = s.vendor_id AND v.tenant_id = s.tenant_id
                       JOIN tenants t ON t.id = s.tenant_id
                       JOIN tenant_settings ts ON ts.tenant_id = s.tenant_id
                       WHERE s.tenant_id = :t AND s.id = :s"""
                ),
                {"t": tenant_id, "s": sub_order_id},
            )
        )
        .mappings()
        .first()
    )
    if head is None:
        raise NotFound("Not found")
    items = (
        (
            await db.execute(
                text(
                    """SELECT title_snapshot, sku_snapshot, qty, unit_price, discount_amount,
                              vat_rate, vat_amount, line_total
                       FROM order_items WHERE tenant_id = :t AND sub_order_id = :s
                       ORDER BY title_snapshot"""
                ),
                {"t": tenant_id, "s": sub_order_id},
            )
        )
        .mappings()
        .all()
    )
    return {"head": dict(head), "items": [dict(i) for i in items]}


def render_pdf(
    kind: str, number: str, data: dict, *, amount: Decimal, vat_amount: Decimal
) -> bytes:
    head, items = data["head"], data["items"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=f"{TITLES[kind]} {number}",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small")
    small.fontSize = 8.5
    small.leading = 11
    address = head["shipping_address"] or {}
    story = [
        Paragraph(f"<b>{TITLES[kind]}</b>", styles["Title"]),
        Paragraph(f"{head['tenant_name']} — marketplace", small),
        Spacer(1, 6),
        Table(
            [
                [
                    Paragraph(
                        f"<b>Seller</b><br/>{head['vendor_name']}<br/>"
                        f"BIN: {head['vat_bin'] or 'not registered'}",
                        small,
                    ),
                    Paragraph(
                        f"<b>Buyer</b><br/>{address.get('recipient_name', '')}<br/>"
                        f"{address.get('address_line', '')}, {address.get('upazila', '')}<br/>"
                        f"{address.get('district_code', '')}<br/>{head['contact_phone']}",
                        small,
                    ),
                    Paragraph(
                        f"<b>{'Invoice' if kind == 'invoice' else 'Credit note'} no.</b> {number}<br/>"
                        f"<b>Date</b> {head['placed_at']:%d %b %Y}<br/>"
                        f"<b>Order</b> {head['order_number']}<br/>"
                        f"<b>Shipment</b> {head['shipment_number']}",
                        small,
                    ),
                ]
            ],
            colWidths=[60 * mm, 60 * mm, 54 * mm],
            style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]),
        ),
        Spacer(1, 10),
    ]
    rows = [["Description", "SKU", "Qty", "Unit", "Discount", "VAT", "Total"]]
    for item in items:
        rows.append(
            [
                Paragraph(item["title_snapshot"], small),
                item["sku_snapshot"],
                str(item["qty"]),
                f"{money(item['unit_price']):,.2f}",
                f"{money(item['discount_amount']):,.2f}",
                f"{money(item['vat_amount']):,.2f}",
                f"{money(item['line_total']):,.2f}",
            ]
        )
    table = Table(rows, colWidths=[56 * mm, 26 * mm, 12 * mm, 22 * mm, 22 * mm, 20 * mm, 22 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8))
    totals = [
        ["Taxable value", f"{money(amount - vat_amount):,.2f}"],
        [f"VAT ({head['vat_pricing']})", f"{money(vat_amount):,.2f}"],
        ["Delivery", f"{money(head['shipping_fee']):,.2f}" if kind == "invoice" else "0.00"],
        ["Total", f"{money(amount):,.2f}"],
    ]
    totals_table = Table(totals, colWidths=[40 * mm, 30 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
            ]
        )
    )
    story += [
        totals_table,
        Spacer(1, 10),
        Paragraph(
            "Amounts in BDT. This document is generated electronically and is valid without a signature.",
            small,
        ),
    ]
    doc.build(story)
    return buf.getvalue()


async def issue(
    db: AsyncSession,
    storage,
    tenant_id: str,
    *,
    sub_order_id,
    kind: str = "invoice",
    amount: Decimal | None = None,
    vat_amount: Decimal | None = None,
) -> dict:
    """Idempotent: one invoice and at most one credit note per shipment, numbered without gaps."""
    existing = (
        (
            await db.execute(
                text(
                    """SELECT number, taxable_amount, vat_amount, document_key FROM tax_invoices
                       WHERE tenant_id = :t AND sub_order_id = :s AND kind = :k"""
                ),
                {"t": tenant_id, "s": sub_order_id, "k": kind},
            )
        )
        .mappings()
        .first()
    )
    if existing:
        return dict(existing)
    data = await _document_data(db, tenant_id, sub_order_id)
    head = data["head"]
    amount = money(amount if amount is not None else head["total"])
    vat_amount = money(vat_amount if vat_amount is not None else head["vat_total"])
    n = (
        await db.execute(
            text(
                """UPDATE tenant_settings SET next_invoice_number = next_invoice_number + 1
                   WHERE tenant_id = :t RETURNING next_invoice_number - 1"""
            ),
            {"t": tenant_id},
        )
    ).scalar()
    prefix = "INV" if kind == "invoice" else "CRN"
    number = f"{prefix}-{head['placed_at']:%Y}-{n:06d}"
    pdf = render_pdf(kind, number, data, amount=amount, vat_amount=vat_amount)
    key = object_key(tenant_id, "tax", kind, f"{number}.pdf")
    await storage.put(key, pdf, "application/pdf")
    await db.execute(
        text(
            """INSERT INTO tax_invoices (tenant_id, vendor_id, number, sub_order_id, kind,
                   taxable_amount, vat_amount, seller_bin, document_key)
               VALUES (:t, :v, :n, :s, :k, :a, :vat, :bin, :key)"""
        ),
        {
            "t": tenant_id,
            "v": head["vendor_id"],
            "n": number,
            "s": sub_order_id,
            "k": kind,
            "a": money(amount - vat_amount),
            "vat": vat_amount,
            "bin": head["vat_bin"],
            "key": key,
        },
    )
    return {
        "number": number,
        "taxable_amount": money(amount - vat_amount),
        "vat_amount": vat_amount,
        "document_key": key,
    }
