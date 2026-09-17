# Phase 5 — SaaS billing & plans (2 weeks) ✅ COMPLETE

**Mission:** the platform earns money: plans with limits, trials, subscription + GMV fee + one-time setup invoices, dunning to suspension, and restoration on payment.

**Demo:** new tenant on Starter trial → tries multi-vendor (402) → platform upgrades to Growth → trial ends and converts → month closes with subscription + setup fee + 1.5% GMV fee + VAT → invoice unpaid 14 days → store becomes read-only (423) → invoice marked paid by bKash reference → writes work again.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 5.1 | Migration: `plans` (seeded Starter/Growth/Scale, limits JSON), `tenant_subscriptions`, `usage_records` (idempotent source_ref), `platform_invoices` (+ total check, unique per period), `platform_invoice_lines`, invoice sequence; tenants get SELECT-only RLS policies | `migrations/versions/0005_billing.py` | ✅ |
| 5.2 | Subscription start on tenant creation (plan choice, trial) | `billing/service.py`, `platform/service.py` | ✅ |
| 5.3 | Entitlements: `require_feature` (multi_vendor on create and mode switch), `require_quota` (vendors, staff, custom domains) → 402 | same + vendors/staff/domains routers | ✅ |
| 5.4 | Usage metering API (GMV/orders), idempotent; Phase 14 feeds it from the ledger | `record_usage` | ✅ |
| 5.5 | Invoice generation: trial conversion, subscription (price/interval/override), setup fee once, GMV fee with rate override, VAT, half-up rounding, period roll-forward, idempotent | `generate_invoice` | ✅ |
| 5.6 | Dunning as a pure date function (overdue → past_due @7d → suspended @14d) + status reconciliation; mark-paid idempotent and restores | `dunning_state`, `reconcile_status`, `mark_paid` | ✅ |
| 5.7 | Suspended tenants read-only (423) except auth and billing | `app/core/deps.py` | ✅ |
| 5.8 | Platform endpoints (plans, change subscription, invoices, mark paid, run cycle) and owner billing overview/invoices | `billing/router.py` | ✅ |
| 5.9 | Daily billing cron (02:05 Dhaka) | `workers/settings.py` | ✅ |
| 5.10 | `.claude/CLAUDE.md` rewritten for v2; `settings.json` hooks read file path from stdin; mobile/packages permissions | `.claude/` | ✅ |

## Tests (87 api tests pass)
Dunning timeline; month-end clamping; trial + starter limits; multi-vendor gated on create and switch, lifted by upgrade; custom-domain quota 402; full invoice maths (1500 + 5000 + 1.5% × 133,333.33 = 2000.00, VAT 15% = 1275.00, total 9775.00) with idempotent usage and invoice; setup fee once; suspension → 423 writes, reads OK, invoice visible → mark paid (idempotent) → writes restored; tenant sees only own invoices at RLS level and cannot write billing; invoice route in isolation harness. Redis test DB flushed per run.

## Notes
- Plan prices/limits are seed placeholders: set real numbers in `plans` before selling.
- Online invoice payment via platform bKash/SSLCommerz reuses Phase 11 gateways.
