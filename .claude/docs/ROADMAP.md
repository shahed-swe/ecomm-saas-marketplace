# Roadmap — Multi-Vendor Marketplace (≈28 weeks, small team)

Narrative summary. The **executable** version — phase folders with per-surface
sub-phase files — lives in `plan/`. A marketplace is ~1.6× the single-vendor
build; the extra time is **tenancy, money, and trust — not features.**

The platform is **one API with seven client surfaces**: web storefront, web
vendor dashboard, web admin panel, and three Flutter apps (**buyer**, **vendor**,
**rider**) — plus a first-party **delivery system**, a **COD + payment-gateway**
stack (COD, bKash, Nagad, SSLCommerz, Stripe Connect), and a double-entry
**ledger/payout** engine. Architecture: `plan/00-architecture.md`.

## Phase 0 — Decisions (2–3 days) → `plan/phase-00-decisions/`
Freeze the schema-shaping calls before any code: commission model + rate, gateway
fees, payout schedule + hold, refund policy, shipping/delivery model, KYC
requirements, enabled gateways. Each is an ADR. Deciding these late is the most
expensive mistake in the project.

## Phase 1 — Foundation + tenancy (weeks 1–3) → `plan/phase-01-foundation-tenancy/`
Foundation + auth, **plus** the tenancy architecture: `vendors`/`vendor_users`,
`current_vendor`, `VendorScopedRepository`, RLS + `app.vendor_id`, three web route
groups, `require_platform_admin`, the Flutter foundation, and the parametrised
cross-tenant isolation harness.
**Demo:** vendor A gets a 404 for vendor B's resource at the app **and** DB layer.
**Do not proceed until this is solid.** Retrofitting tenancy is a rewrite.

## Phase 2 — Vendor onboarding (weeks 4–6) → `plan/phase-02-vendor-onboarding/`
Signup, KYC to the **private** bucket, admin review queue with duplicate
detection, approval state machine, Stripe Connect onboarding, and cold-start
operator tools (invite codes, commission overrides, zero-commission tier).
**Demo:** a seller signs up, gets approved, and Connect onboarding completes.

## Phase 3 — Vendor management (weeks 6–7) → `plan/phase-03-vendor-management/`
Vendor dashboard: storefront settings, staff roles (`owner|manager|staff`), payout
bank details with owner re-auth. All vendor-scoped.
**Demo:** a vendor configures their store and manages staff from the dashboard.

## Phase 4 — Catalog & media (weeks 8–9) → `plan/phase-04-catalog-media/`
Vendor-scoped product/variant CRUD, the image pipeline, storefront listing + PDP
with the vendor card, moderation queue for first listings.
**Demo:** a vendor publishes a product with photos; a buyer sees it on the storefront.

## Phase 5 — Discovery & search (week 10) → `plan/phase-05-discovery-search/`
Multi-vendor tsvector + trigram search, facets (store, ships-from), vendor-quality
ranking, store pages, vendor cards.
**Demo:** search 10k products / 50 vendors under 200 ms p95.

## Phase 6 — Cart & split checkout (weeks 11–12) → `plan/phase-06-cart-split-checkout/`
Grouped cart, `orders → sub_orders → order_items`, deterministic variant-id lock
ordering, commission snapshot at capture writing a balanced ledger group.
**Demo:** a 3-vendor cart → 3 sub-orders; one cancels, only its ledger moves.

## Phase 7 — Payments: COD + gateways (weeks 12–14) → `plan/phase-07-payments/`
COD (per sub-order, settles at delivery), bKash, Nagad, SSLCommerz, and **Stripe
Connect destination charges** — split at capture, verified server-side, idempotent,
reconciled.
**Demo:** pay via each method; split at capture; a duplicate callback is a no-op.

## Phase 8 — Order fulfilment (weeks 14–15) → `plan/phase-08-order-fulfilment/`
Sub-order fulfilment state machine, partial fulfilment/cancel/refund affecting only
one vendor's ledger, derived parent status.
**Demo:** a vendor fulfils a sub-order; partial states render correctly for the buyer.

## Phase 9 — Delivery system (weeks 16–18) → `plan/phase-09-delivery-system/`
Dispatch engine, the Flutter **rider app** (PoD, COD cash), admin dispatch console,
live tracking, COD cash reconciliation.
**Demo:** dispatch → rider app delivers with PoD → COD cash reconciles.

## Phase 10 — Ledger & payouts (weeks 18–20) → `plan/phase-10-ledger-payouts/`
Double-entry ledger, balances with holds/reserves, payout runs idempotent on
`(vendor_id, period_end)`, nightly reconciliation, vendor earnings dashboard.
**Demo:** run a payout twice → one transfer, balanced books, clean reconciliation.

## Phase 11 — Buyer mobile app (weeks 20–22) → `plan/phase-11-mobile-buyer-app/`
Full Flutter buyer app: browse, split cart, pay (COD + gateways), track, push,
offline.

## Phase 12 — Vendor mobile app (weeks 22–23) → `plan/phase-12-mobile-vendor-app/`
Flutter vendor app: orders, stock, chat, earnings on the go — scoped to own vendor.

## Phase 13 — Trust & safety (weeks 23–24) → `plan/phase-13-trust-safety/`
Disputes with SLA timers and evidence (reversing ledger entries), review integrity,
vendor scoring, suspension/appeal, messaging with exfiltration filters.
**Demo:** open a dispute, hold funds, resolve partially, correct ledger reversals.

## Phase 14 — Notifications & messaging (weeks 24–25) → `plan/phase-14-notifications-messaging/`
Push (buyer/vendor/rider), SMS + email, in-app chat.

## Phase 15 — Analytics & reporting (weeks 25–26) → `plan/phase-15-analytics-reporting/`
Vendor and admin dashboards with correct, **isolated** numbers; CSV exports.

## Phase 16 — Hardening & launch (weeks 27–28) → `plan/phase-16-hardening-launch/`
Security (cross-tenant focus), performance (every §6 budget), load test with
realistic vendor distribution, e2e, legal, backups + rehearsed restore, CI/CD,
app-store submissions, `/ship`.
**Demo:** green go/no-go with a cross-tenant load test + app-store submissions.

## Cold-start reality
Ten vendors × thirty products looks empty. Build the operator tools in week one:
invite codes, per-vendor commission overrides, manual homepage curation, a
zero-commission launch tier. They live in Phases 2 and 13, not the backlog.

## Deliberately deferred
Multi-warehouse, subscriptions, B2B, auctions, vendor ads/sponsored placement,
cross-border tax automation, Kubernetes.

## Known hard parts, ranked
1. Cross-tenant isolation (a leak is existential)
2. Ledger correctness and payout idempotency
3. Payment verification across five providers + COD cash reconciliation
4. Deterministic lock ordering in split checkout (deadlocks under load)
5. Partial fulfilment and refund arithmetic
6. Delivery dispatch + rider COD cash drift
7. Faceted search performance at vendor scale
8. Keeping three Flutter apps on one shared core
