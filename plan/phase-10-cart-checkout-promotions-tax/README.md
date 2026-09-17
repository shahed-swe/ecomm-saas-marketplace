# Phase 10 — Cart, checkout, promotions & tax (3 weeks) ✅ COMPLETE

**Mission:** turn a cross-vendor cart into the **order tree** — one payment, many vendor shipments — priced correctly (campaign prices, vendor coupons, per-vendor delivery, VAT) and safe under load: no overselling, no deadlocks, no double orders. Payment capture itself is Phase 11.

**Demo:** a cart with two shops → one quote showing per-vendor delivery and VAT → place → one order, two sub-orders, stock reserved, cart emptied → each vendor sees only its own shipment → an unpaid order expires and returns the stock.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 10.1 | Migration: BD geography (8 divisions, 64 districts with delivery zones), addresses, shipping rates, coupons, campaigns + campaign products, tax rates, carts, orders, sub_orders, order_items, stock_reservations, coupon redemptions, tenant checkout settings, `derive_order_status()` | `migrations/versions/0010_checkout.py` | ✅ |
| 10.2 | Pure pricing engine: campaign price, coupon (percent with cap / fixed) with **largest-remainder proration**, weight-tiered delivery with free-over funded by vendor or tenant, VAT inclusive/exclusive per line, half-up to 0.01 | `checkout/pricing.py` | ✅ |
| 10.3 | Quote service: prices from the database only, per-vendor groups, stock/availability issues, coupon errors surfaced per group, COD availability from tenant rules | `checkout/service.py` | ✅ |
| 10.4 | Placement in one transaction: **deterministic variant lock order** (no deadlocks), availability re-check, campaign cap and coupon limit under locks, order tree + reservations, cart cleared | same | ✅ |
| 10.5 | Idempotency on `(tenant, Idempotency-Key)`; `expected_total` mismatch → 409 `price_changed` | same | ✅ |
| 10.6 | Commission **snapshotted** on each sub-order at placement (vendor → category → tenant → house) | `resolve_commission` | ✅ |
| 10.7 | Reservation expiry job: unpaid prepaid orders cancel, stock/coupons/campaign caps released | `expire_unpaid`, worker cron (every minute) | ✅ |
| 10.8 | Buyer APIs: cart, quote, addresses (BD district list), place, my orders, **guest-safe tracking link** (number + unguessable token, no PII) | `checkout/router.py` | ✅ |
| 10.9 | Vendor APIs: coupons, campaign participation (discount ceiling enforced), own shipments only | same | ✅ |
| 10.10 | Admin APIs: campaigns, shipping rates per zone/vendor, VAT settings and per-category rates, COD rules, order list | same | ✅ |
| 10.11 | Money is never a float: Decimal is serialised as a string for every route | `app/main.py` | ✅ |
| 10.12 | Web: add-to-cart on the PDP, cart page with quantity edits, checkout (saved or new address, district-driven delivery, payment choice, live totals, 409 re-quote), order page | `apps/web/src/app/(shop)/{cart,checkout,orders}` | ✅ |

## Tests (213 api tests pass)
Pricing unit tests (proration sums exactly, percent cap + minimum, fixed never exceeds subtotal, weight tiers, free-over recomputed **after** discount, VAT inclusive vs exclusive, half-up) · grouped cart totals and order tree, vendor sees only its shipment, stock reserved, cart cleared · free-shipping/coupon interaction · campaign price + cap exhaustion · **6 concurrent checkouts with carts in opposite order: exactly one succeeds, reservations never exceed stock, no deadlock** · idempotent placement (same number, no second order) and stale-total 409 · COD rules (max value, unverified phone, immediate confirmation, no payment window) · expiry job releases stock and cancels · guest tracking token (wrong token 404, other tenant 404, no PII) · commission snapshot immune to later rate changes · isolation harness extended (sub-orders, coupons, campaigns).

## Notes
- Payment capture, gateway redirects and the ledger entries for these orders are Phase 11 and Phase 14; sub-orders start as `pending_payment` (prepaid) or `confirmed` (COD).
- Upazila/area are free text with a seeded district list; courier area codes arrive with the courier adapters in Phase 12.
