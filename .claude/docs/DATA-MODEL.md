# Data Model — Multi-Vendor Marketplace

Everything in the single-vendor model applies (users, addresses, categories,
products, variants, media, carts, coupons, reviews, audit_log). This document
covers what is **added or changed** for a marketplace.

## Vendors

- **vendors** — slug (unique), display_name, legal_name, status
  (`registered|documents_submitted|under_review|changes_requested|approved|
  rejected|suspended|closed`), country, tax_id, support_email, support_phone,
  logo_asset_id, banner_asset_id, bio, return_policy, shipping_policy,
  holiday_mode, connect_account_id, charges_enabled, payouts_enabled,
  payout_schedule, payout_min_amount, reserve_percent, reserve_until,
  commission_override, tier, approved_at, suspended_at, suspension_reason,
  agreement_accepted_at
- **vendor_users** — vendor_id, user_id, role (`owner|manager|staff`),
  invited_by, accepted_at. Unique `(vendor_id, user_id)`
- **vendor_documents** — vendor_id, doc_type (`trade_licence|nid|tin|
  bank_statement|other`), storage_key (private bucket), status
  (`pending|approved|rejected|expired`), expires_at, reviewed_by, reviewed_at,
  rejection_reason
- **vendor_events** — vendor_id, from_status, to_status, reason, actor_id
- **vendor_metrics** — vendor_id, period, on_time_dispatch_rate,
  cancellation_rate, dispute_rate, return_rate, avg_response_minutes,
  rating_avg, score (rolling 90-day projection, recomputed nightly)
- **vendor_invite_codes** — code (unique), created_by (admin), vendor_id
  (nullable, claimed), tier_override, commission_override, used_at

## Changed from single-vendor

- **products** — `+ vendor_id NOT NULL`, unique `(vendor_id, sku)` instead of
  global unique SKU, `+ moderation_status` (`pending|approved|rejected`),
  `+ quality_score`
- **product_variants** — `+ vendor_id` (denormalised for RLS and index locality)
- **coupons** — `+ vendor_id NULL` (null = platform-funded) and
  `+ funded_by (platform|vendor)`; the funding source decides whose ledger the
  discount hits
- **reviews** — `+ vendor_id`, `+ vendor_response`, `+ vendor_responded_at`,
  `+ fraud_score`, `+ verified_purchase` (boolean, enforced by
  `UNIQUE (user_id, order_item_id)`)

## Orders

- **orders** — buyer-facing parent: order_number, user_id, email, currency,
  items_subtotal, discount_total, tax_total, shipping_total, grand_total,
  shipping_address (JSONB), status (**derived**, never set directly), placed_at.
  No `vendor_id` here, ever.
- **sub_orders** — order_id, vendor_id, sub_order_number, status
  (`pending_payment|confirmed|processing|shipped|delivered|cancelled|refunded`),
  items_subtotal, shipping_method, shipping_cost, tax_total, commission_rate,
  commission_amount, rate_source, vendor_net, cancelled_at, cancel_reason.
  Unique `(order_id, vendor_id)`
- **order_items** — sub_order_id NOT NULL, variant_id, title_snapshot,
  sku_snapshot, unit_price, qty, discount_amount, tax_amount, line_total
- **shipments** — sub_order_id (not order_id), carrier, tracking_number, status,
  method (`platform|vendor_courier|pickup`)
- **stock_reservations** — sub_order_id, variant_id, qty, expires_at

## Money (the ledger)

- **ledger_entries** — entry_group_id, vendor_id (nullable for platform-only
  entries), account (`platform_clearing|vendor_payable|platform_revenue|
  payment_fees|reserve|cod_receivable`), direction (`debit|credit`), amount,
  currency, entry_type (`sale|commission|payment_fee|refund|
  commission_reversal|payout|cod_remittance|dispute_hold|dispute_release|
  adjustment`), reference_type, reference_id, description, created_at.
  **Append-only.** No updated_at. No delete path. `CHECK (amount > 0)`
- **vendor_balances** — vendor_id, currency, available, pending, reserved,
  negative_since, last_reconciled_at. A cached projection; the ledger is truth.
- **payout_runs** — vendor_id, period_start, period_end, currency, amount,
  status (`pending|processing|completed|failed`), provider, provider_ref,
  failure_reason, attempts.
  Unique `(vendor_id, period_end)` — this is the idempotency guarantee.
- **commission_rules** — scope (`platform|category|vendor|vendor_category`),
  vendor_id, category_id, type, percent, fixed_fee, min_fee, max_fee,
  effective_from, effective_to
- **disputes** — sub_order_id, opened_by, reason, status
  (`open|under_review|resolved_buyer|resolved_vendor|resolved_partial|
  escalated|closed`), amount_held, resolution, resolved_by, resolved_at,
  sla_due_at
- **dispute_messages** / **dispute_evidence** — immutable, actor-stamped,
  evidence assets in the private bucket

## Payments

- **payments** — order_id, provider (`cod|bkash|nagad|sslcommerz|
  stripe_connect`), method_kind (`cash|wallet|card|aggregator`), status
  (`initiated|pending|verified|failed|refunded`), amount, currency,
  provider_ref, idempotency_key, verified_at, created_at
- **payment_events** — payment_id, provider, event_id, event_type, payload
  (JSONB), processed_at. Unique `(provider, event_id)` — duplicate callback is a
  no-op
- **refunds** — payment_id, sub_order_id, amount, reason, status
  (`pending|processing|completed|failed`), provider_ref, created_at
- **cod_receivables** — sub_order_id, vendor_id, amount, status
  (`due|collected|remitted|cancelled`), delivery_id, collected_at, remitted_at

## Delivery & Dispatch

- **delivery_zones** — name, area (postcode set / polygon JSONB),
  base_fee, per_km_fee, sla_minutes, cod_enabled, cod_ceiling, is_active
- **deliveries** — sub_order_id, vendor_id, rider_id, zone_id, status
  (`pending|assigned|accepted|picked_up|in_transit|arrived|delivered|
  failed_attempt|returned`), assigned_at, accepted_at, picked_up_at,
  delivered_at, attempt_count, cod_amount (0 for prepaid), sla_deadline,
  proof_type (`otp|photo|signature`), proof_verified
- **rider_profiles** — user_id (rider scope), zone_ids (JSONB), rating,
  active_load, cod_cash_on_hand, cod_cash_ceiling, status
  (`available|on_delivery|offline|suspended`)
- **rider_locations** — rider_id, lat, lng, heading, speed, recorded_at.
  Ephemeral (Redis stream primary, table for audit only; never stored at full
  resolution long-term)
- **rider_cash_ledger** — rider_id, delivery_id, direction (`collect|remit`),
  amount, balance_after, created_at. Remittance settles against the platform
  ledger via balanced entry groups.
- **proof_of_deliveries** — delivery_id, type (`otp|photo|signature`),
  storage_key (private bucket), otp_hash, verified, created_at

## Communication & Notifications

- **conversations** / **messages** — buyer ↔ vendor, scoped to a sub-order where
  applicable, with flags for contact-detail exfiltration and attachment scanning
- **device_tokens** — user_id, platform (`fcm|apns`), token, app
  (`buyer|vendor|rider`), created_at, last_used_at
- **notification_logs** — user_id, channel (`push|sms|email|in_app`),
  template_id, reference_type, reference_id, status (`sent|delivered|failed`),
  sent_at

## Indexes specific to the marketplace

```sql
-- Vendors
CREATE UNIQUE INDEX ON vendors (slug);
CREATE UNIQUE INDEX ON vendor_users (vendor_id, user_id);
CREATE INDEX ON vendor_users (user_id);

-- Products (vendor-scoped)
CREATE INDEX ON products (vendor_id, published_at DESC) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX ON products (vendor_id, sku);

-- Orders
CREATE INDEX ON sub_orders (vendor_id, created_at DESC);
CREATE INDEX ON sub_orders (order_id);

-- Money
CREATE INDEX ON ledger_entries (vendor_id, created_at DESC);
CREATE INDEX ON ledger_entries (entry_group_id);
CREATE UNIQUE INDEX ON payout_runs (vendor_id, period_end);

-- Payments
CREATE UNIQUE INDEX ON payment_events (provider, event_id);
CREATE INDEX ON payments (order_id);
CREATE INDEX ON cod_receivables (vendor_id, status);

-- Delivery
CREATE INDEX ON deliveries (rider_id, status);
CREATE INDEX ON deliveries (sub_order_id);
CREATE INDEX ON deliveries (zone_id, status);
CREATE INDEX ON rider_cash_ledger (rider_id, created_at DESC);

-- Trust
CREATE INDEX ON disputes (status, sla_due_at) WHERE status <> 'resolved_buyer'
  AND status <> 'resolved_vendor' AND status <> 'closed';
CREATE UNIQUE INDEX ON reviews (user_id, order_item_id);

-- Notifications
CREATE INDEX ON device_tokens (user_id, app);
CREATE INDEX ON notification_logs (user_id, sent_at DESC);
```

## Row Level Security

Enable on `products`, `product_variants`, `sub_orders`, `shipments`,
`ledger_entries`, `payout_runs`, `vendor_documents`, `conversations`,
`cod_receivables`, `deliveries`, `disputes`, `reviews` (vendor-scoped reads),
with the `vendor_isolation` policy on `current_setting('app.vendor_id')`. The
admin path uses a DB role that bypasses RLS — separate role, not a cleared
variable. Rider endpoints use a `rider_isolation` policy on
`current_setting('app.rider_id')` for `deliveries` and `rider_cash_ledger`.

## Invariants to assert in tests

1. Every `entry_group_id` sums to zero.
2. `orders.grand_total` = Σ sub_order totals + order-level discounts/tax.
3. No `order_items` row without a `sub_order_id`.
4. No `products` row without a `vendor_id`.
5. `vendor_balances.available` = ledger sum, verified nightly.
6. `rider_cash_ledger` collect entries = Σ `cod_receivables` with
   `status=collected` for that rider.
7. `payment_events (provider, event_id)` is globally unique — no duplicate
   callback processing.
8. `payout_runs (vendor_id, period_end)` is unique — re-running produces zero
   new transfers.
9. `deliveries.proof_verified = true` before any `delivered` status.
10. No cross-vendor data in any analytics aggregate, CSV export, or error string.
