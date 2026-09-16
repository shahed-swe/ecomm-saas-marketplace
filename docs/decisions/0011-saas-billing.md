# ADR 0011 — SaaS plans and billing
Status: Accepted · 2026-09-16

- `plans(code, name, interval monthly|yearly, price, gmv_fee_rate, setup_fee)`, `plan_limits(plan_id, key, value)` keys: `vendors, products, staff, custom_domains, apps, monthly_orders, custom_css, white_label_apps`.
- `tenant_subscriptions(tenant_id, plan_id, status trial|active|past_due|suspended|cancelled, current_period_start/end, trial_ends_at)`.
- GMV fee metered nightly from tenant ledger: captured + COD delivered − refunds, per period → `usage_records`.
- Invoices: `platform_invoices`, `platform_invoice_lines` (subscription, gmv_fee, setup_fee); paid via platform bKash/SSLCommerz or manual bank marking.
- Dunning: reminders at due −3, 0, +3; `past_due` at +7; `suspended` at +14 (storefront maintenance page, admin read-only, in-flight orders continue); purge only after cancellation + 90 days + export.
- Entitlements enforced by `require_entitlement(key)` and quota checks at write time.
