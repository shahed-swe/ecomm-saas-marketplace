"""Dashboards and exports.

Two decisions shape this module:

* **Finished days are rolled up; today is computed live.** A dashboard that says "0 orders today"
  because the nightly job has not run yet is worse than a slightly more expensive query, so today
  always comes from the order tree and yesterday and earlier come from `daily_metrics`.
* **Every number here is the tenant's own.** The same SQL serves a vendor with `vendor_id` bound,
  and RLS stands behind it, so a vendor dashboard can never widen into the tenant's.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_keys import object_key
from app.core.errors import AppError
from app.modules.checkout.pricing import ZERO, money

# One statement produces both the tenant total and the per-vendor slices for a day.
ROLLUP_SQL = """
WITH scope AS (
  SELECT s.id AS sub_order_id, s.vendor_id, s.order_id, s.status, s.items_subtotal, s.discount_total,
         s.shipping_fee, s.vat_total, s.total, s.commission_rate, o.payment_method, o.user_id,
         o.vat_pricing
  FROM sub_orders s
  JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
  WHERE s.tenant_id = :t AND o.placed_at >= :start AND o.placed_at < :end
),
units AS (
  SELECT i.sub_order_id, sum(i.qty) AS qty FROM order_items i
  WHERE i.tenant_id = :t AND i.sub_order_id IN (SELECT sub_order_id FROM scope)
  GROUP BY i.sub_order_id
),
refunded AS (
  SELECT r.vendor_id, sum(f.amount) AS amount
  FROM refunds f JOIN return_requests r ON r.id = f.return_id AND r.tenant_id = f.tenant_id
  WHERE f.tenant_id = :t AND f.status = 'completed'
    AND f.completed_at >= :start AND f.completed_at < :end
  GROUP BY r.vendor_id
),
delivered AS (
  SELECT s.vendor_id, count(*) AS n FROM shipments sh
  JOIN sub_orders s ON s.id = sh.sub_order_id AND s.tenant_id = sh.tenant_id
  WHERE sh.tenant_id = :t AND sh.delivered_at >= :start AND sh.delivered_at < :end
  GROUP BY s.vendor_id
),
new_buyers AS (
  SELECT count(*) AS n FROM users u
  WHERE u.tenant_id = :t AND u.created_at >= :start AND u.created_at < :end
)
SELECT
  g.vendor_id,
  count(DISTINCT g.order_id) AS orders,
  count(*) AS shipments,
  coalesce(sum(u.qty), 0) AS units,
  coalesce(sum(g.total), 0) AS gmv,
  coalesce(sum(g.discount_total), 0) AS discounts,
  coalesce(sum(g.shipping_fee), 0) AS shipping,
  coalesce(sum(g.vat_total), 0) AS vat,
  coalesce(sum(round((g.items_subtotal - g.discount_total
    - CASE WHEN g.vat_pricing = 'inclusive' THEN g.vat_total ELSE 0 END) * g.commission_rate, 2)), 0)
    AS commission,
  count(*) FILTER (WHERE g.status = 'cancelled') AS cancelled,
  count(*) FILTER (WHERE g.status = 'returned') AS returned,
  count(DISTINCT g.order_id) FILTER (WHERE g.payment_method = 'cod') AS cod_orders
FROM scope g LEFT JOIN units u ON u.sub_order_id = g.sub_order_id
GROUP BY GROUPING SETS ((g.vendor_id), ())
"""


async def rollup_day(db: AsyncSession, tenant_id: str, day: date) -> int:
    """Recompute one day from the order tree. Safe to run again: rows are replaced, never doubled."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC) - timedelta(hours=6)
    end = start + timedelta(days=1)
    rows = (
        (await db.execute(text(ROLLUP_SQL), {"t": tenant_id, "start": start, "end": end}))
        .mappings()
        .all()
    )
    extras = (
        (
            await db.execute(
                text(
                    """SELECT
                         (SELECT count(*) FROM users WHERE tenant_id = :t
                            AND created_at >= :start AND created_at < :end) AS new_buyers,
                         (SELECT coalesce(sum(f.amount), 0) FROM refunds f
                            WHERE f.tenant_id = :t AND f.status = 'completed'
                              AND f.completed_at >= :start AND f.completed_at < :end) AS refunds,
                         (SELECT count(*) FROM shipments sh WHERE sh.tenant_id = :t
                            AND sh.delivered_at >= :start AND sh.delivered_at < :end) AS delivered"""
                ),
                {"t": tenant_id, "start": start, "end": end},
            )
        )
        .mappings()
        .one()
    )
    per_vendor_refunds = {
        str(v): amount
        for v, amount in (
            await db.execute(
                text(
                    """SELECT r.vendor_id, sum(f.amount) FROM refunds f
                       JOIN return_requests r ON r.id = f.return_id AND r.tenant_id = f.tenant_id
                       WHERE f.tenant_id = :t AND f.status = 'completed'
                         AND f.completed_at >= :start AND f.completed_at < :end
                       GROUP BY r.vendor_id"""
                ),
                {"t": tenant_id, "start": start, "end": end},
            )
        ).all()
    }
    per_vendor_delivered = {
        str(v): n
        for v, n in (
            await db.execute(
                text(
                    """SELECT s.vendor_id, count(*) FROM shipments sh
                       JOIN sub_orders s ON s.id = sh.sub_order_id AND s.tenant_id = sh.tenant_id
                       WHERE sh.tenant_id = :t AND sh.delivered_at >= :start AND sh.delivered_at < :end
                       GROUP BY s.vendor_id"""
                ),
                {"t": tenant_id, "start": start, "end": end},
            )
        ).all()
    }
    # One day is recomputed as a whole: clear it, then write it. Idempotent by construction.
    await db.execute(
        text("DELETE FROM daily_metrics WHERE tenant_id = :t AND day = :d"),
        {"t": tenant_id, "d": day},
    )
    written = 0
    for row in rows:
        vendor_id = str(row["vendor_id"]) if row["vendor_id"] else None
        await db.execute(
            text(
                """INSERT INTO daily_metrics (tenant_id, day, vendor_id, orders, shipments, units, gmv,
                       discounts, shipping, vat, commission, refunds, cancelled, returned, delivered,
                       cod_orders, new_buyers)
                   VALUES (:t, :d, :v, :o, :sh, :u, :gmv, :disc, :ship, :vat, :comm, :ref, :canc,
                           :ret, :del, :cod, :nb)"""
            ),
            {
                "t": tenant_id,
                "d": day,
                "v": vendor_id,
                "o": row["orders"],
                "sh": row["shipments"],
                "u": row["units"],
                "gmv": row["gmv"],
                "disc": row["discounts"],
                "ship": row["shipping"],
                "vat": row["vat"],
                "comm": row["commission"],
                "ref": per_vendor_refunds.get(
                    vendor_id, extras["refunds"] if vendor_id is None else 0
                ),
                "canc": row["cancelled"],
                "ret": row["returned"],
                "del": per_vendor_delivered.get(
                    vendor_id, extras["delivered"] if vendor_id is None else 0
                ),
                "cod": row["cod_orders"],
                "nb": extras["new_buyers"] if vendor_id is None else 0,
            },
        )
        written += 1
    return written


async def rollup_range(db: AsyncSession, tenant_id: str, *, days: int = 2) -> int:
    """Yesterday and today by default: today because it changed, yesterday because it just closed."""
    today = _today()
    return sum(
        [await rollup_day(db, tenant_id, today - timedelta(days=offset)) for offset in range(days)]
    )


def _today() -> date:
    return (datetime.now(UTC) + timedelta(hours=6)).date()


async def series(
    db: AsyncSession,
    tenant_id: str,
    *,
    start: date,
    end: date,
    vendor_id: str | None = None,
) -> list[dict]:
    """Rolled-up days, plus today computed live so a dashboard is never behind the shop."""
    rows = (
        (
            await db.execute(
                text(
                    """SELECT day, orders, shipments, units, gmv, discounts, shipping, vat, commission,
                              refunds, cancelled, returned, delivered, cod_orders, new_buyers
                       FROM daily_metrics
                       WHERE tenant_id = :t AND day BETWEEN :s AND :e
                         AND vendor_id IS NOT DISTINCT FROM CAST(:v AS uuid)
                       ORDER BY day"""
                ),
                {"t": tenant_id, "s": start, "e": end, "v": vendor_id},
            )
        )
        .mappings()
        .all()
    )
    out = [dict(r) for r in rows]
    today = _today()
    if start <= today <= end:
        await rollup_day(db, tenant_id, today)
        fresh = (
            (
                await db.execute(
                    text(
                        """SELECT day, orders, shipments, units, gmv, discounts, shipping, vat, commission,
                                  refunds, cancelled, returned, delivered, cod_orders, new_buyers
                           FROM daily_metrics
                           WHERE tenant_id = :t AND day = :d
                             AND vendor_id IS NOT DISTINCT FROM CAST(:v AS uuid)"""
                    ),
                    {"t": tenant_id, "d": today, "v": vendor_id},
                )
            )
            .mappings()
            .first()
        )
        out = [r for r in out if r["day"] != today] + ([dict(fresh)] if fresh else [])
    return sorted(out, key=lambda r: r["day"])


def summarise(rows: list[dict]) -> dict:
    totals = {
        k: sum(Decimal(str(r[k] or 0)) for r in rows)
        for k in ("gmv", "discounts", "shipping", "vat", "commission", "refunds")
    }
    counts = {
        k: sum(int(r[k] or 0) for r in rows)
        for k in (
            "orders",
            "shipments",
            "units",
            "cancelled",
            "returned",
            "delivered",
            "cod_orders",
            "new_buyers",
        )
    }
    gmv = money(totals["gmv"])
    net = money(gmv - totals["refunds"])
    return {
        **{k: money(v) for k, v in totals.items()},
        **counts,
        "net_sales": net,
        "average_order_value": money(gmv / counts["orders"]) if counts["orders"] else ZERO,
        "return_rate": (
            round(counts["returned"] / counts["shipments"], 4) if counts["shipments"] else None
        ),
        "cod_share": (
            round(counts["cod_orders"] / counts["orders"], 4) if counts["orders"] else None
        ),
    }


async def top_products(
    db: AsyncSession, tenant_id: str, *, start: date, end: date, vendor_id=None, limit: int = 10
) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT i.product_id, max(i.title_snapshot) AS title, sum(i.qty) AS units,
                              sum(i.line_total) AS revenue
                       FROM order_items i
                       JOIN sub_orders s ON s.id = i.sub_order_id AND s.tenant_id = i.tenant_id
                       JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
                       WHERE i.tenant_id = :t AND o.placed_at::date BETWEEN :s AND :e
                         AND s.status <> 'cancelled'
                         AND (CAST(:v AS uuid) IS NULL OR i.vendor_id = CAST(:v AS uuid))
                       GROUP BY i.product_id ORDER BY revenue DESC LIMIT :l"""
                ),
                {"t": tenant_id, "s": start, "e": end, "v": vendor_id, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def top_vendors(
    db: AsyncSession, tenant_id: str, *, start: date, end: date, limit: int = 10
) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """SELECT m.vendor_id, v.display_name, sum(m.gmv) AS gmv, sum(m.orders) AS orders,
                              sum(m.commission) AS commission
                       FROM daily_metrics m
                       JOIN vendors v ON v.id = m.vendor_id AND v.tenant_id = m.tenant_id
                       WHERE m.tenant_id = :t AND m.day BETWEEN :s AND :e AND m.vendor_id IS NOT NULL
                       GROUP BY m.vendor_id, v.display_name ORDER BY gmv DESC LIMIT :l"""
                ),
                {"t": tenant_id, "s": start, "e": end, "l": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# -------------------------------------------------------------------------------------- exports
EXPORTS = {
    "orders": (
        """SELECT o.number, o.placed_at, o.payment_method, s.number AS shipment, v.display_name AS vendor,
                  s.status, s.items_subtotal, s.discount_total, s.shipping_fee, s.vat_total, s.total,
                  o.shipping_address->>'district_code' AS district
           FROM sub_orders s
           JOIN orders o ON o.id = s.order_id AND o.tenant_id = s.tenant_id
           JOIN vendors v ON v.id = s.vendor_id AND v.tenant_id = s.tenant_id
           WHERE s.tenant_id = :t AND o.placed_at::date BETWEEN :s AND :e
             AND (CAST(:v AS uuid) IS NULL OR s.vendor_id = CAST(:v AS uuid))
           ORDER BY o.placed_at""",
        "orders",
    ),
    "payouts": (
        """SELECT b.period_end, v.display_name AS vendor, l.gross, l.tds, l.net, l.method,
                  l.account_last4, l.status, l.reference, l.paid_at
           FROM payout_lines l
           JOIN payout_batches b ON b.id = l.batch_id AND b.tenant_id = l.tenant_id
           JOIN vendors v ON v.id = l.vendor_id AND v.tenant_id = l.tenant_id
           WHERE l.tenant_id = :t AND l.period_end BETWEEN :s AND :e
             AND (CAST(:v AS uuid) IS NULL OR l.vendor_id = CAST(:v AS uuid))
           ORDER BY b.period_end DESC""",
        "payouts",
    ),
    "products": (
        """SELECT p.slug, p.title_en, v.display_name AS vendor, c.name_en AS category, p.status,
                  p.min_price, p.max_price, p.rating_avg, p.rating_count,
                  (SELECT sum(stock_on_hand) FROM product_variants pv
                    WHERE pv.tenant_id = p.tenant_id AND pv.product_id = p.id) AS stock
           FROM products p
           JOIN vendors v ON v.id = p.vendor_id AND v.tenant_id = p.tenant_id
           JOIN categories c ON c.id = p.category_id AND c.tenant_id = p.tenant_id
           WHERE p.tenant_id = :t AND (CAST(:v AS uuid) IS NULL OR p.vendor_id = CAST(:v AS uuid))
           ORDER BY p.title_en""",
        "products",
    ),
    "ledger": (
        """SELECT created_at, account, direction, amount, entry_type, ref_type, memo
           FROM ledger_entries
           WHERE tenant_id = :t AND created_at::date BETWEEN :s AND :e
             AND (CAST(:v AS uuid) IS NULL OR vendor_id = CAST(:v AS uuid))
           ORDER BY created_at""",
        "ledger",
    ),
}


class ExportError(AppError):
    status = 422
    code = "bad_export"


async def export_csv(
    db: AsyncSession,
    storage,
    tenant_id: str,
    *,
    kind: str,
    start: date,
    end: date,
    vendor_id: str | None,
    actor_id: str,
) -> dict:
    """Exports go to the private bucket: an orders CSV has names, phone numbers and addresses in it."""
    if kind not in EXPORTS:
        raise ExportError("Unknown export")
    sql, _ = EXPORTS[kind]
    rows = (
        (await db.execute(text(sql), {"t": tenant_id, "s": start, "e": end, "v": vendor_id}))
        .mappings()
        .all()
    )
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in dict(row).items()})
    key = object_key(
        tenant_id, "exports", kind, f"{start}_{end}_{datetime.now(UTC):%Y%m%d%H%M%S}.csv"
    )
    await storage.put(key, buf.getvalue().encode(), "text/csv")
    export_id = (
        await db.execute(
            text(
                """INSERT INTO report_exports (tenant_id, vendor_id, kind, params, status, rows,
                       object_key, requested_by)
                   VALUES (:t, :v, :k, CAST(:p AS jsonb), 'ready', :n, :key, :a) RETURNING id"""
            ),
            {
                "t": tenant_id,
                "v": vendor_id,
                "k": kind,
                "p": f'{{"start": "{start}", "end": "{end}"}}',
                "n": len(rows),
                "key": key,
                "a": actor_id,
            },
        )
    ).scalar()
    return {"id": str(export_id), "kind": kind, "rows": len(rows), "object_key": key}


# ------------------------------------------------------------------------------ platform view
async def platform_overview(db: AsyncSession, *, days: int = 30) -> dict:
    """The platform's own business: recurring revenue, GMV fees, and which tenants are in trouble.

    Runs on the platform connection (it spans tenants by design) and never reads a tenant's
    customer data — only the aggregates the tenant already agreed to be billed on.
    """
    mrr = (
        (
            await db.execute(
                text(
                    """SELECT coalesce(sum(
                           CASE WHEN s.interval = 'yearly'
                                THEN coalesce(s.price_override, p.yearly_price) / 12
                                ELSE coalesce(s.price_override, p.monthly_price) END), 0) AS mrr,
                          count(*) FILTER (WHERE s.status = 'active') AS paying,
                          count(*) FILTER (WHERE s.status = 'trial') AS trialing,
                          count(*) FILTER (WHERE s.status = 'past_due') AS past_due,
                          count(*) FILTER (WHERE s.status = 'suspended') AS suspended
                       FROM tenant_subscriptions s JOIN plans p ON p.code = s.plan_code
                       WHERE s.status IN ('active','past_due')"""
                )
            )
        )
        .mappings()
        .one()
    )
    counts = (
        (
            await db.execute(
                text(
                    """SELECT count(*) FILTER (WHERE status = 'trial') AS trialing,
                              count(*) FILTER (WHERE status = 'past_due') AS past_due,
                              count(*) FILTER (WHERE status = 'suspended') AS suspended
                       FROM tenant_subscriptions"""
                )
            )
        )
        .mappings()
        .one()
    )
    gmv = (
        (
            await db.execute(
                text(
                    """SELECT coalesce(sum(quantity), 0) AS gmv FROM usage_records
                       WHERE metric = 'gmv' AND period_date > current_date - make_interval(days => :d)"""
                ),
                {"d": days},
            )
        )
        .mappings()
        .one()
    )
    fees = (
        (
            await db.execute(
                text(
                    """SELECT coalesce(sum(total), 0) AS billed,
                              coalesce(sum(total) FILTER (WHERE status = 'paid'), 0) AS collected,
                              coalesce(sum(total) FILTER (WHERE status = 'open'), 0) AS outstanding
                       FROM platform_invoices
                       WHERE created_at > now() - make_interval(days => :d)"""
                ),
                {"d": days},
            )
        )
        .mappings()
        .one()
    )
    health = (
        (
            await db.execute(
                text(
                    """SELECT t.id, t.slug, t.name, t.status, s.plan_code, s.status AS subscription,
                              (SELECT coalesce(sum(m.gmv), 0) FROM daily_metrics m
                                WHERE m.tenant_id = t.id AND m.vendor_id IS NULL
                                  AND m.day > current_date - 30) AS gmv_30d,
                              (SELECT coalesce(sum(m.orders), 0) FROM daily_metrics m
                                WHERE m.tenant_id = t.id AND m.vendor_id IS NULL
                                  AND m.day > current_date - 7) AS orders_7d,
                              (SELECT count(*) FROM vendors v WHERE v.tenant_id = t.id
                                 AND v.status = 'approved') AS vendors
                       FROM tenants t
                       LEFT JOIN tenant_subscriptions s ON s.tenant_id = t.id
                       WHERE t.status NOT IN ('purged')
                       ORDER BY gmv_30d DESC LIMIT 100"""
                )
            )
        )
        .mappings()
        .all()
    )
    return {
        "mrr": money(mrr["mrr"]),
        "paying_tenants": mrr["paying"],
        "trialing": counts["trialing"],
        "past_due": counts["past_due"],
        "suspended": counts["suspended"],
        "gmv_window_days": days,
        "gmv": money(gmv["gmv"]),
        "platform_billed": money(fees["billed"]),
        "platform_collected": money(fees["collected"]),
        "platform_outstanding": money(fees["outstanding"]),
        "tenants": [
            {**dict(t), "gmv_30d": money(t["gmv_30d"]), "at_risk": t["orders_7d"] == 0}
            for t in health
        ],
    }
