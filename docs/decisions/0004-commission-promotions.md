# ADR 0004 — Commission and promotions funding
Status: Accepted · 2026-09-16

## Commission
- Resolution order: vendor override → category rate → tenant default. Stored as `commission_rate`, `rate_source`, `commission_amount` on `sub_orders` at capture/COD delivery. Never recomputed.
- Base: **item price after vendor-funded discounts, excluding VAT and shipping.**
- Tenant can set a zero-commission launch tier via vendor override with `expires_at`.

## Promotions (V1)
| Type | Funded by | Ledger |
|---|---|---|
| Vendor coupon | vendor | reduces `vendor_payable`; commission on discounted price |
| Flash sale / campaign price | vendor (price itself) | no discount line; snapshot price |
| Campaign tenant subsidy (optional) | tenant | debit `promo_expense_tenant` |
| Free-shipping rule | named funder (vendor or tenant) | debit funder's expense / reduce `vendor_payable` |

- Proration across items by line value, largest-remainder rounding to 0.01.
- One vendor coupon per vendor group per order; campaigns and coupons stack only if the campaign allows.
- Platform (tenant) coupons are deferred (not selected for V1); schema keeps `funded_by`.

## Schema impact
`commission_rules(tenant_id, scope tenant|category|vendor, scope_id, rate, starts_at, expires_at)`; `coupons(tenant_id, vendor_id, code, kind percent|fixed, value, min_subtotal, max_discount, usage_limit, per_buyer_limit, starts_at, ends_at)`; `campaigns`, `campaign_products(price, stock_cap)`; `shipping_rules(tenant_id, vendor_id null, free_over, funded_by)`.
