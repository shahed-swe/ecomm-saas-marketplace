---
name: marketplace-checkout-split
description: Build multi-vendor checkout — splitting one cart into per-vendor sub-orders, per-vendor shipping and ETAs, single-payment capture with Stripe Connect or PayPal Marketplace, partial fulfilment and partial cancellation, and per-vendor refunds. Use this skill for any cart, checkout, order, shipping, or refund work in this marketplace, including "why does the cart show two shipping fees".
---

# Multi-Vendor Checkout & Order Splitting

## Order shape

```
orders (buyer-facing, one payment)
  └── sub_orders (one per vendor: own status, shipping, commission, tracking)
        └── order_items
```
The buyer sees one order number and one charge. Each vendor sees only their
sub-order. `orders` carries no `vendor_id`; `order_items` carries no vendor
either — it belongs to a sub-order, which belongs to a vendor. Keep that
invariant and the tenancy queries stay simple.

## Quote

`POST /checkout/quote` returns groups, not a flat list:

```json
{
  "groups": [{
    "vendor": {"id": "...", "name": "...", "ships_from": "Dhaka"},
    "items": [...],
    "subtotal": 249000,
    "shipping_options": [{"id":"std","label":"Standard","cost":6000,"eta_days":[2,4]}],
    "selected_shipping": "std",
    "estimated_delivery": "2026-09-22"
  }],
  "discount_total": 0, "tax_total": 12450, "grand_total": 267450,
  "currency": "BDT", "quote_hash": "..."
}
```
Shipping is per vendor because fulfilment is per vendor. Showing one blended
shipping fee is the single most common multi-vendor UX mistake — it makes the
cart impossible to reconcile when one group is later cancelled.

## Creating the order

One transaction:
1. Re-quote server-side; compare `quote_hash`; 409 with the new quote if it moved.
2. `SELECT ... FOR UPDATE` every variant across every vendor, in a **deterministic
   order (sort by variant id)** — otherwise two concurrent multi-vendor checkouts
   deadlock. This is the subtle bug that only appears under load.
3. Create `orders`, then one `sub_order` per vendor with its snapshotted
   commission rate and resolved shipping.
4. Create reservations with a TTL.
5. Create one PaymentIntent with `transfer_group` set to the order id (destination
   charges: one `application_fee_amount` computed as the sum of per-sub-order
   commissions; separate charges and transfers: one charge, then N transfers on
   capture).

## Capture and split

On `payment_intent.succeeded`: transition every sub-order to `paid`, convert
reservations to inventory movements, write the ledger entries (sale, commission,
fees) per sub-order, and notify each vendor about only their own items.

## Partial states are the norm

Vendor A ships, vendor B cancels. Therefore:
- Parent order status is **derived**, never set directly:
  all delivered → `delivered`; any shipped → `partially_shipped`; all
  cancelled → `cancelled`; mixed → `partially_fulfilled`.
- Cancelling a sub-order refunds exactly that sub-order's items plus its shipping,
  reverses that vendor's commission, and leaves the others untouched.
- The buyer's order page shows one order with per-vendor tracking sections. Do not
  present three separate orders — the buyer paid once.

## Refunds
Always scoped to a sub-order (or specific items within it). Refund amount =
item amounts + proportional item-level discount + that group's shipping if the
whole group is refunded. The commission reversal is proportional. Where the
platform retains its fee on a buyer-fault return, that must be a distinct,
disclosed ledger entry — never a silently skipped reversal.

## Frontend rules
Group the cart by vendor with a header card (logo, name, rating, ships-from).
Per-group shipping selector and subtotal; one grand total. Stock or price changes
are flagged inline on the affected line, and the affected group is disabled
rather than the whole checkout blocked.

## Tests that must exist
Three-vendor cart → one order, three sub-orders, totals reconcile · concurrent
checkouts contending on a shared variant, locked in id order, no deadlock · one
vendor cancels post-payment → correct partial refund and single commission
reversal · webhook replay → no duplicate sub-order transitions.
