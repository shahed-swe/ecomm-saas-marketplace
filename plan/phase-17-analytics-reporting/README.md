# Phase 17 — Analytics & reporting (2 weeks) ✅ COMPLETE

**Mission:** give each audience exactly its own numbers — the tenant its shop, a vendor its own slice and nobody else's, the platform its business — fast enough to open every morning and exportable when someone wants a spreadsheet.

**Demo:** two sales this morning already show on the dashboard (no waiting for a nightly job); a vendor opens the same report and sees only its own GMV with no league table of competitors; a refund moves net sales down; an orders CSV lands in the private bucket behind a short-lived signed URL; the platform console shows MRR, GMV fees and which tenants have had no orders this week.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 17.1 | Migration: `daily_metrics` (tenant total **and** per-vendor rows, two partial unique indexes because a NULL cannot sit in a primary key), `report_exports`; RLS | `migrations/versions/0017_reporting.py` | ✅ |
| 17.2 | One rollup statement producing the tenant total and every vendor slice for a day, with refunds, deliveries and new buyers folded in; a day is cleared and rewritten, so re-running never doubles it | `reporting/service.rollup_day` | ✅ |
| 17.3 | Series reader: finished days from the rollup, **today computed live**, so a dashboard is never behind the shop | `service.series` | ✅ |
| 17.4 | Summary maths: net of refunds, AOV, return rate, COD share, and a previous-period comparison | `service.summarise`, router | ✅ |
| 17.5 | Top products and top sellers, both bound to the caller's scope | `service.top_products`, `top_vendors` | ✅ |
| 17.6 | CSV exports (orders, payouts, products, ledger) written to the **private** bucket and served by signed URL — an orders export carries names, phones and addresses | `service.export_csv` | ✅ |
| 17.7 | Vendor dashboard and vendor export, bound to `vendor_id` in SQL with RLS behind it | `reporting/router.py` | ✅ |
| 17.8 | Platform console: MRR from live subscriptions, trial/past-due/suspended counts, GMV in the window, platform billed vs collected vs outstanding, and per-tenant health with an at-risk flag | `service.platform_overview` | ✅ |
| 17.9 | Nightly rollup of the last three days per tenant (yesterday closed, and a window for late-arriving facts) | `workers/settings.rollup_metrics` | ✅ |
| 17.10 | Web: staff dashboard with period switch, comparison cards, daily bars, top lists and one-click orders export | `apps/web/src/app/(admin)/admin/reports` | ✅ |

## Tests (383 api tests pass)
Today's two sales appear immediately with units, commission, COD share, delivered count and AOV, and the top-product list is right · running the rollup three times leaves exactly one tenant-total row and one vendor row for the day · a vendor sees 3 units to the other's 1, no `top_vendors` key at all, and only its own product in the top list · a completed refund shows in `refunds` and comes off `net_sales` · an export writes a tenant-scoped private object, returns a signed URL, contains exactly the two orders with the district column, and can be re-signed later from the export list · a vendor's export contains only that vendor's rows · the platform overview reports MRR, a 30-day GMV window and per-tenant health without any customer-level data, and is a 404 to a tenant's staff · the default window is the last thirty days and a backwards range is read the way it was obviously meant.

## Notes
- **Yesterday is history; today is live.** Rolled-up days are cheap to read and never change; today is recomputed on each dashboard load, which is the one query worth paying for.
- Exports are private by construction. There is no public URL for a CSV of customers, and the signed link expires in minutes.
- The platform view reads aggregates only (`daily_metrics`, subscriptions, invoices) — it never opens a tenant's orders, and RLS would stop it if it tried without the platform role.
- Cohorts, funnels and attribution are deliberately out of scope: the tenant's own GA4 and Meta (Phase 16) already answer those, and duplicating them here would mean a second set of numbers to reconcile.
