"""Quote and placement for the order tree (architecture §6).

Placement is one transaction:
  1. lock variants in deterministic (tenant, id) order  -> no deadlocks between overlapping carts
  2. re-price from the database (client totals are never trusted); mismatch with `expected_total` -> 409
  3. check availability = on_hand - reserved; campaign caps and coupon limits under row locks
  4. write orders -> sub_orders -> order_items, reserve stock, redeem coupons
Idempotent on (tenant, Idempotency-Key).
"""

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.modules.catalog.products import VISIBLE
from app.modules.checkout.pricing import (
    ZERO,
    Coupon,
    Group,
    Line,
    ShippingRate,
    apply_coupon,
    apply_vat,
    group_total,
    money,
    shipping_fee,
)


class CheckoutError(AppError):
    status = 409
    code = "checkout_conflict"


@dataclass
class Quote:
    groups: list[Group]
    pricing: str
    zone: str | None
    issues: list[dict]
    payment_methods: list[str]

    @property
    def items_subtotal(self) -> Decimal:
        return sum((g.subtotal for g in self.groups), ZERO)

    @property
    def discount_total(self) -> Decimal:
        return sum((g.discount for g in self.groups), ZERO)

    @property
    def shipping_total(self) -> Decimal:
        return sum((g.shipping_fee - g.shipping_waived for g in self.groups), ZERO)

    @property
    def vat_total(self) -> Decimal:
        return sum((g.vat for g in self.groups), ZERO)

    @property
    def grand_total(self) -> Decimal:
        return sum((group_total(g, self.pricing) for g in self.groups), ZERO)

    def as_dict(self) -> dict:
        return {
            "groups": [
                {
                    "vendor_id": g.vendor_id,
                    "items": [
                        {
                            "variant_id": ln.variant_id,
                            "product_id": ln.product_id,
                            "title": ln.title,
                            "sku": ln.sku,
                            "options": ln.options,
                            "list_price": ln.list_price,
                            "unit_price": ln.unit_price,
                            "qty": ln.qty,
                            "discount": ln.discount,
                            "vat_rate": ln.vat_rate,
                            "vat_amount": ln.vat_amount,
                            "line_total": ln.total,
                        }
                        for ln in g.lines
                    ],
                    "subtotal": g.subtotal,
                    "discount": g.discount,
                    "coupon": g.coupon.code if g.coupon else None,
                    "coupon_error": g.coupon_error,
                    "shipping_fee": g.shipping_fee,
                    "shipping_waived": g.shipping_waived,
                    "vat": g.vat,
                    "total": group_total(g, self.pricing),
                }
                for g in self.groups
            ],
            "items_subtotal": self.items_subtotal,
            "discount_total": self.discount_total,
            "shipping_total": self.shipping_total,
            "vat_total": self.vat_total,
            "grand_total": self.grand_total,
            "vat_pricing": self.pricing,
            "zone": self.zone,
            "issues": self.issues,
            "payment_methods": self.payment_methods,
        }


async def build_quote(
    db: AsyncSession,
    tenant_id: str,
    items: list[tuple[str, int]],
    *,
    district: str | None,
    coupons: dict[str, str],
    user_id: str | None,
    payment_method: str | None = None,
    lock: bool = False,
) -> Quote:
    if not items:
        raise AppError("Your cart is empty", status=422, code="empty_cart")
    variant_ids = sorted({v for v, _ in items})  # deterministic lock order
    qty = {}
    for v, q in items:
        qty[v] = qty.get(v, 0) + q
    now = datetime.now(UTC)
    rows = (
        (
            await db.execute(
                text(
                    f"""SELECT pv.id, pv.product_id, pv.vendor_id, p.category_id, p.title_en, pv.sku, pv.options, pv.price,
                   pv.stock_on_hand - pv.stock_reserved AS available, coalesce(pv.weight_grams, p.weight_grams, 500) AS weight,
                   coalesce(tr.rate, s.default_vat_rate) AS vat_rate, s.vat_pricing,
                   cp.id AS campaign_product_id, cp.campaign_price, cp.stock_cap - cp.sold_count AS campaign_left
            FROM product_variants pv
            JOIN products p ON p.id = pv.product_id AND p.tenant_id = pv.tenant_id
            JOIN vendors v ON v.id = pv.vendor_id AND v.tenant_id = pv.tenant_id
            JOIN tenant_settings s ON s.tenant_id = pv.tenant_id
            LEFT JOIN tax_rates tr ON tr.tenant_id = p.tenant_id AND tr.category_id = p.category_id
            LEFT JOIN LATERAL (
                SELECT c.id, c.campaign_price, c.stock_cap, c.sold_count FROM campaign_products c
                JOIN campaigns cm ON cm.id = c.campaign_id AND cm.tenant_id = c.tenant_id
                WHERE c.tenant_id = pv.tenant_id AND c.variant_id = pv.id AND cm.status = 'scheduled'
                  AND cm.starts_at <= :now AND cm.ends_at > :now AND c.sold_count < c.stock_cap
                ORDER BY c.campaign_price LIMIT 1) cp ON true
            WHERE pv.tenant_id = :t AND pv.id = ANY(CAST(:ids AS uuid[])) AND pv.is_active AND {VISIBLE}
            ORDER BY pv.id {"FOR UPDATE OF pv" if lock else ""}"""
                ),  # noqa: S608
                {"t": tenant_id, "ids": variant_ids, "now": now},
            )
        )
        .mappings()
        .all()
    )
    found = {str(r["id"]): r for r in rows}
    issues: list[dict] = []
    groups: dict[str, Group] = {}
    pricing = rows[0]["vat_pricing"] if rows else "inclusive"
    for vid in variant_ids:
        r = found.get(vid)
        if r is None:
            issues.append(
                {
                    "variant_id": vid,
                    "code": "unavailable",
                    "message": "This item is no longer available",
                }
            )
            continue
        want = qty[vid]
        if r["available"] < want:
            issues.append(
                {
                    "variant_id": vid,
                    "code": "insufficient_stock",
                    "available": max(0, r["available"]),
                    "message": f"Only {max(0, r['available'])} left",
                }
            )
            continue
        use_campaign = r["campaign_product_id"] is not None and r["campaign_left"] >= want
        ln = Line(
            vid,
            str(r["product_id"]),
            str(r["vendor_id"]),
            str(r["category_id"]),
            r["title_en"],
            r["sku"],
            r["options"],
            r["price"],
            r["campaign_price"] if use_campaign else r["price"],
            want,
            r["weight"],
            Decimal(r["vat_rate"]),
            str(r["campaign_product_id"]) if use_campaign else None,
        )
        groups.setdefault(ln.vendor_id, Group(ln.vendor_id, [])).lines.append(ln)

    zone = None
    if district:
        zone = (
            await db.execute(
                text("SELECT zone FROM geo_districts WHERE code = :d"), {"d": district}
            )
        ).scalar()
        if zone is None:
            raise AppError("Unknown district", status=422, code="invalid_district")
    rates = {}
    if zone and groups:
        for r in (
            (
                await db.execute(
                    text(
                        """SELECT DISTINCT ON (coalesce(vendor_id, tenant_id)) vendor_id, base_fee, base_weight_grams, per_extra_kg,
                      free_over, free_funded_by
               FROM shipping_rates WHERE tenant_id = :t AND zone = :z
                 AND (vendor_id IS NULL OR vendor_id = ANY(CAST(:v AS uuid[])))"""
                    ),
                    {"t": tenant_id, "z": zone, "v": list(groups)},
                )
            )
            .mappings()
            .all()
        ):
            rates[str(r["vendor_id"]) if r["vendor_id"] else "*"] = ShippingRate(
                r["base_fee"],
                r["base_weight_grams"],
                r["per_extra_kg"],
                r["free_over"],
                r["free_funded_by"],
            )

    wanted = {vid: code.strip() for vid, code in coupons.items() if code and code.strip()}
    coupon_rows = {}
    if wanted:
        rs = (
            (
                await db.execute(
                    text(
                        f"""SELECT c.*, (SELECT count(*) FROM coupon_redemptions r WHERE r.tenant_id = c.tenant_id
                               AND r.coupon_id = c.id AND r.user_id = CAST(:u AS uuid)) AS mine
                FROM coupons c WHERE c.tenant_id = :t AND c.code = ANY(CAST(:codes AS citext[]))
                ORDER BY c.id {"FOR UPDATE OF c" if lock else ""}"""
                    ),  # noqa: S608
                    {"t": tenant_id, "codes": list(wanted.values()), "u": user_id},
                )
            )
            .mappings()
            .all()
        )
        coupon_rows = {r["code"].lower(): r for r in rs}
    for g in groups.values():
        code = wanted.get(g.vendor_id)
        if code:
            c = coupon_rows.get(code.lower())
            err = None
            if c is None or str(c["vendor_id"]) != g.vendor_id:
                err = "Coupon not valid for this shop"
            elif (
                not c["is_active"] or c["starts_at"] > now or (c["ends_at"] and c["ends_at"] <= now)
            ):
                err = "Coupon has expired"
            elif c["usage_limit"] is not None and c["used_count"] >= c["usage_limit"]:
                err = "Coupon has been fully used"
            elif user_id and c["mine"] >= c["per_buyer_limit"]:
                err = "You have already used this coupon"
            if err is None:
                try:
                    apply_coupon(
                        g,
                        Coupon(
                            str(c["id"]),
                            c["code"],
                            c["kind"],
                            c["value"],
                            c["min_subtotal"],
                            c["max_discount"],
                        ),
                    )
                except ValueError as exc:
                    err = str(exc)
            g.coupon_error = err
        shipping_fee(g, rates.get(g.vendor_id) or rates.get("*"))
        apply_vat(g, pricing)

    methods = []
    if groups:
        s = (
            (
                await db.execute(
                    text(
                        "SELECT cod_enabled, cod_max_order, cod_blocked_districts FROM tenant_settings WHERE tenant_id = :t"
                    ),
                    {"t": tenant_id},
                )
            )
            .mappings()
            .one()
        )
        q = Quote(list(groups.values()), pricing, zone, issues, [])
        if (
            s["cod_enabled"]
            and q.grand_total <= s["cod_max_order"]
            and (district not in s["cod_blocked_districts"])
        ):
            methods.append("cod")
        methods += ["bkash", "sslcommerz"]  # availability refined by tenant credentials in Phase 11
        q.payment_methods = methods
        return q
    return Quote([], pricing, zone, issues, methods)


async def resolve_commission(
    db, tenant_id: str, vendor_id: str, category_id: str
) -> tuple[Decimal, str]:
    row = (
        (
            await db.execute(
                text(
                    """SELECT v.is_house,
                  (SELECT rate FROM commission_rules WHERE tenant_id = :t AND scope = 'vendor' AND scope_id = :v
                     AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now()) ORDER BY starts_at DESC LIMIT 1) AS vr,
                  (SELECT rate FROM commission_rules WHERE tenant_id = :t AND scope = 'category' AND scope_id = :c
                     AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now()) ORDER BY starts_at DESC LIMIT 1) AS cr,
                  s.default_commission_rate AS tr
           FROM vendors v JOIN tenant_settings s ON s.tenant_id = v.tenant_id
           WHERE v.id = :v AND v.tenant_id = :t"""
                ),
                {"t": tenant_id, "v": vendor_id, "c": category_id},
            )
        )
        .mappings()
        .one()
    )
    if row["is_house"]:
        return Decimal("0"), "house"
    if row["vr"] is not None:
        return row["vr"], "vendor"
    if row["cr"] is not None:
        return row["cr"], "category"
    return row["tr"], "tenant"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def place_order(
    db: AsyncSession,
    tenant_id: str,
    *,
    user_id: str,
    items: list[tuple[str, int]],
    address: dict,
    contact_phone: str,
    contact_email: str | None,
    payment_method: str,
    coupons: dict[str, str],
    expected_total: Decimal,
    idempotency_key: str,
) -> tuple[dict, str | None]:
    existing = (
        await db.execute(
            text("SELECT id, number FROM orders WHERE tenant_id = :t AND idempotency_key = :k"),
            {"t": tenant_id, "k": idempotency_key},
        )
    ).first()
    if existing:
        return {"id": str(existing.id), "number": existing.number, "replayed": True}, None

    quote = await build_quote(
        db,
        tenant_id,
        items,
        district=address["district_code"],
        coupons=coupons,
        user_id=user_id,
        payment_method=payment_method,
        lock=True,
    )
    if quote.issues:
        raise CheckoutError("Some items changed. Review your cart.", code="cart_changed")
    bad_coupon = next((g.coupon_error for g in quote.groups if g.coupon_error), None)
    if bad_coupon:
        raise CheckoutError(bad_coupon, code="coupon_invalid")
    if payment_method not in quote.payment_methods:
        raise CheckoutError(
            "This payment method is not available for this order", code="payment_unavailable"
        )
    if money(expected_total) != quote.grand_total:
        raise CheckoutError(
            f"The total changed to ৳{quote.grand_total}. Please review.", code="price_changed"
        )

    settings = (
        (
            await db.execute(
                text(
                    """UPDATE tenant_settings SET next_order_number = next_order_number + 1 WHERE tenant_id = :t
           RETURNING next_order_number - 1 AS n, order_prefix, reservation_minutes"""
                ),
                {"t": tenant_id},
            )
        )
        .mappings()
        .one()
    )
    number = f"{settings['order_prefix']}-{settings['n']}"
    tracking_token = secrets.token_urlsafe(18)
    prepaid = payment_method != "cod"
    due = (
        datetime.now(UTC) + timedelta(minutes=settings["reservation_minutes"]) if prepaid else None
    )
    order_id = (
        await db.execute(
            text(
                """INSERT INTO orders (tenant_id, number, user_id, contact_phone, contact_email, payment_method, items_subtotal,
               discount_total, shipping_total, vat_total, grand_total, vat_pricing, shipping_address, idempotency_key,
               tracking_token_hash, payment_due_at)
           VALUES (:t, :n, :u, :ph, :em, :pm, :sub, :disc, :ship, :vat, :tot, :vp, CAST(:addr AS jsonb), :k, :th, :due)
           RETURNING id"""
            ),
            {
                "t": tenant_id,
                "n": number,
                "u": user_id,
                "ph": contact_phone,
                "em": contact_email,
                "pm": payment_method,
                "sub": quote.items_subtotal,
                "disc": quote.discount_total,
                "ship": quote.shipping_total,
                "vat": quote.vat_total,
                "tot": quote.grand_total,
                "vp": quote.pricing,
                "addr": json.dumps(address),
                "k": idempotency_key,
                "th": _hash(tracking_token),
                "due": due,
            },
        )
    ).scalar()

    for i, g in enumerate(sorted(quote.groups, key=lambda g: g.vendor_id), start=1):
        rate, source = await resolve_commission(db, tenant_id, g.vendor_id, g.lines[0].category_id)
        sub_id = (
            await db.execute(
                text(
                    """INSERT INTO sub_orders (tenant_id, order_id, vendor_id, number, status, items_subtotal, discount_total,
                   shipping_fee, shipping_waived, shipping_waiver_funded_by, vat_total, total, weight_grams, coupon_id,
                   commission_rate, commission_source)
               VALUES (:t, :o, :v, :n, :s, :sub, :disc, :fee, :waived, :wf, :vat, :tot, :w, :c, :cr, :cs) RETURNING id"""
                ),
                {
                    "t": tenant_id,
                    "o": order_id,
                    "v": g.vendor_id,
                    "n": f"{number}-{i}",
                    "s": "pending_payment" if prepaid else "confirmed",
                    "sub": g.subtotal,
                    "disc": g.discount,
                    "fee": g.shipping_fee,
                    "waived": g.shipping_waived,
                    "wf": g.shipping_waiver_funded_by,
                    "vat": g.vat,
                    "tot": group_total(g, quote.pricing),
                    "w": g.weight,
                    "c": g.coupon.id if g.coupon else None,
                    "cr": rate,
                    "cs": source,
                },
            )
        ).scalar()
        for ln in g.lines:
            await db.execute(
                text(
                    """INSERT INTO order_items (tenant_id, sub_order_id, vendor_id, product_id, variant_id, title_snapshot,
                       sku_snapshot, options_snapshot, list_price, unit_price, qty, discount_amount, vat_rate, vat_amount,
                       line_total, campaign_product_id)
                   VALUES (:t, :s, :v, :p, :var, :title, :sku, CAST(:opt AS jsonb), :lp, :up, :q, :d, :vr, :va, :lt, :cp)"""
                ),
                {
                    "t": tenant_id,
                    "s": sub_id,
                    "v": g.vendor_id,
                    "p": ln.product_id,
                    "var": ln.variant_id,
                    "title": ln.title,
                    "sku": ln.sku,
                    "opt": json.dumps(ln.options),
                    "lp": ln.list_price,
                    "up": ln.unit_price,
                    "q": ln.qty,
                    "d": ln.discount,
                    "vr": ln.vat_rate,
                    "va": ln.vat_amount,
                    "lt": ln.total,
                    "cp": ln.campaign_product_id,
                },
            )
            await db.execute(
                text(
                    """UPDATE product_variants SET stock_reserved = stock_reserved + :q, updated_at = now()
                   WHERE id = :v AND tenant_id = :t"""
                ),
                {"q": ln.qty, "v": ln.variant_id, "t": tenant_id},
            )
            await db.execute(
                text(
                    """INSERT INTO stock_reservations (tenant_id, vendor_id, order_id, sub_order_id, variant_id, qty, expires_at)
                   VALUES (:t, :v, :o, :s, :var, :q, :e)"""
                ),
                {
                    "t": tenant_id,
                    "v": g.vendor_id,
                    "o": order_id,
                    "s": sub_id,
                    "var": ln.variant_id,
                    "q": ln.qty,
                    "e": due,
                },
            )
            if ln.campaign_product_id:
                res = await db.execute(
                    text(
                        """UPDATE campaign_products SET sold_count = sold_count + :q
                       WHERE id = :c AND tenant_id = :t AND sold_count + :q <= stock_cap"""
                    ),
                    {"q": ln.qty, "c": ln.campaign_product_id, "t": tenant_id},
                )
                if res.rowcount != 1:
                    raise CheckoutError(
                        "A sale item just sold out. Review your cart.", code="cart_changed"
                    )
        if g.coupon:
            res = await db.execute(
                text(
                    """UPDATE coupons SET used_count = used_count + 1
                   WHERE id = :c AND tenant_id = :t AND (usage_limit IS NULL OR used_count < usage_limit)"""
                ),
                {"c": g.coupon.id, "t": tenant_id},
            )
            if res.rowcount != 1:
                raise CheckoutError("Coupon has been fully used", code="coupon_invalid")
            await db.execute(
                text(
                    """INSERT INTO coupon_redemptions (tenant_id, coupon_id, user_id, order_id, amount)
                   VALUES (:t, :c, :u, :o, :a)"""
                ),
                {"t": tenant_id, "c": g.coupon.id, "u": user_id, "o": order_id, "a": g.discount},
            )
    if not prepaid:
        # COD owes money from placement: one receivable per shipment (ADR 0006).
        from app.modules.payments import service as payments

        await payments.register_cod(db, tenant_id, order_id, amount=quote.grand_total)
    await db.execute(
        text(
            "DELETE FROM cart_items WHERE tenant_id = :t AND cart_id IN "
            "(SELECT id FROM carts WHERE tenant_id = :t AND user_id = :u)"
        ),
        {"t": tenant_id, "u": user_id},
    )
    return {"id": str(order_id), "number": number, "replayed": False}, tracking_token


async def release_order(
    db: AsyncSession, tenant_id: str, order_id, reason: str, *, statuses=("pending_payment",)
) -> int:
    """Cancel open sub-orders of an order and release their reservations, coupons and campaign stock."""
    subs = (
        await db.execute(
            text(
                """UPDATE sub_orders SET status = 'cancelled', cancelled_at = now(), cancel_reason = :r, updated_at = now()
           WHERE tenant_id = :t AND order_id = :o AND status = ANY(CAST(:st AS text[]))
           RETURNING id, coupon_id"""
            ),
            {"t": tenant_id, "o": order_id, "r": reason, "st": list(statuses)},
        )
    ).all()
    if not subs:
        return 0
    ids = [str(s.id) for s in subs]
    res = (
        await db.execute(
            text(
                """UPDATE stock_reservations SET released_at = now()
           WHERE tenant_id = :t AND sub_order_id = ANY(CAST(:s AS uuid[])) AND released_at IS NULL AND consumed_at IS NULL
           RETURNING variant_id, qty"""
            ),
            {"t": tenant_id, "s": ids},
        )
    ).all()
    for variant_id, q in sorted(res, key=lambda r: str(r[0])):
        await db.execute(
            text(
                "UPDATE product_variants SET stock_reserved = stock_reserved - :q WHERE id = :v AND tenant_id = :t"
            ),
            {"q": q, "v": variant_id, "t": tenant_id},
        )
    await db.execute(
        text(
            """UPDATE campaign_products c SET sold_count = c.sold_count - i.qty FROM order_items i
           WHERE i.tenant_id = :t AND i.sub_order_id = ANY(CAST(:s AS uuid[])) AND c.id = i.campaign_product_id
             AND c.tenant_id = i.tenant_id"""
        ),
        {"t": tenant_id, "s": ids},
    )
    from app.modules.payments import service as payments

    await payments.cancel_cod_receivables(db, tenant_id, ids)
    for s in subs:
        if s.coupon_id:
            await db.execute(
                text(
                    "UPDATE coupons SET used_count = used_count - 1 WHERE id = :c AND tenant_id = :t"
                ),
                {"c": s.coupon_id, "t": tenant_id},
            )
            await db.execute(
                text(
                    "DELETE FROM coupon_redemptions WHERE tenant_id = :t AND coupon_id = :c AND order_id = :o"
                ),
                {"t": tenant_id, "c": s.coupon_id, "o": order_id},
            )
    return len(subs)


async def expire_unpaid(db: AsyncSession, now: datetime | None = None) -> int:
    """Platform job: unpaid prepaid orders past their payment window are cancelled and stock released."""
    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            text(
                """SELECT DISTINCT o.tenant_id, o.id FROM orders o JOIN sub_orders s ON s.order_id = o.id AND s.tenant_id = o.tenant_id
           WHERE o.payment_due_at IS NOT NULL AND o.payment_due_at < :now AND s.status = 'pending_payment'
           ORDER BY o.id LIMIT 500"""
            ),
            {"now": now},
        )
    ).all()
    n = 0
    for tenant_id, order_id in rows:
        n += await release_order(db, str(tenant_id), order_id, "payment_timeout")
    return n


__all__ = ["uuid"]
