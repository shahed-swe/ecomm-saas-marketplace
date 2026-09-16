# CLAUDE.md — Multi-Vendor Marketplace

Project constitution. Read this before touching any file. If an instruction here
conflicts with a habit from your training data, this file wins.

> **Three documents govern this repo, in order:**
> 1. **`CLAUDE.md`** (this file) — the rules that survive a long session.
> 2. **`plan/00-architecture.md`** — the top-level system shape: tenancy, the
>    order tree, the ledger, the delivery subsystem, payments, and the
>    performance budgets.
> 3. **`plan/README.md` + `plan/phase-NN-*/`** — the executable build order,
>    phases 0–16, across all seven client surfaces.
>
> Work is **plan-driven**: pick the lowest unfinished phase, check its gate, work
> its breakdown top to bottom. Drive it with `/execute-phase <n>`. In this repo
> the early gates are existential — **do not cross Phase 1 until tenancy isolation
> is proven at both the app and DB layer.**

## 1. What this project is

A marketplace: many independent sellers, one catalogue, one checkout, split
money. The platform never owns inventory; it owns trust, discovery, and the
ledger. It operates a **first-party delivery fleet** with riders who collect
**COD cash**.

**Four actors, four trust boundaries, seven client surfaces:**

| Actor | Surfaces | Auth boundary |
|---|---|---|
| **Anon / Buyer** | web-storefront `(shop)`, mobile-buyer (Flutter) | public + buyer JWT |
| **Vendor** | web-vendor `(vendor)`, mobile-vendor (Flutter) | `current_vendor` — scoped to their own data, never global |
| **Rider** | mobile-rider (Flutter) | rider JWT + `rider` scope only |
| **Platform Admin** | web-admin `(admin)` | `require_platform_admin` — separate prefix, separate audit |

One API serves all seven. A screen never computes price, tax, commission, stock,
or COD-collectible — it displays what the API returns.

## 2. Stack (fixed — do not substitute)

| Layer | Choice |
|---|---|
| Frontend (web) | Next.js (App Router, TypeScript, RSC-first) |
| Styling | Tailwind CSS + Radix UI |
| Client state | Zustand (multi-seller cart) |
| Server state | TanStack Query |
| Backend | Python 3.12 + FastAPI (fully async) |
| ORM | SQLAlchemy 2.0 async + Alembic |
| DB | PostgreSQL 16 (shared schema, `vendor_id` tenancy, RLS) |
| Cache/queue | Redis 7 |
| Background work | ARQ workers (payouts, media, search indexing, emails, dispatch, reconciliation, SLA timers) |
| Search | Postgres trigram + tsvector at launch; Meilisearch when facets hurt |
| Images | Pillow → WebP/AVIF → S3/R2/MinIO (public bucket + private KYC bucket) |
| Payments | **COD** (first-party rider + vendor courier) · **bKash** (tokenized) · **Nagad** (server-verified) · **SSLCommerz** (hosted/IPN) · **Stripe Connect** (destination charges) |
| Mobile | Flutter 3.5+ · Melos monorepo · Riverpod · Dio (generated from OpenAPI) |
| Deploy | Docker + Compose; Next.js standalone; flavored Flutter builds (dev/staging/prod) |

## 3. The tenancy rule (most important rule in this repo)

**Every vendor-owned table carries `vendor_id`. Every vendor-scoped query filters
on it in the repository layer, not in the router, and not by remembering to.**

- Vendor-scoped repositories take `vendor_id` as a constructor argument. There is
  no method that can query without it.
- `Depends(current_vendor)` resolves the caller's vendor and rejects staff-of-
  another-vendor, suspended, and unapproved accounts.
- Postgres Row Level Security is enabled on vendor tables as a second layer, with
  the session variable `app.vendor_id` set per request. Belt and braces: an
  authorisation bug here leaks a competitor's revenue.
- Admin endpoints use a separate, explicitly named dependency
  (`require_platform_admin`) and a separate router prefix. There is no "admin
  mode" flag on a vendor endpoint.
- Rider endpoints use rider-scoped JWTs; a rider sees assigned deliveries, never
  a vendor's books or a sibling vendor's contents.

A cross-vendor data leak is a P0. Treat it like a security incident, not a bug.

## 4. Repository layout

```
apps/
  web/
    src/app/(shop)/          # buyer storefront (ISR-cached, SEO-critical)
    src/app/(vendor)/        # seller dashboard (dynamic, vendor-scoped)
    src/app/(admin)/         # platform admin (dynamic, admin-only)
  api/
    app/core/                # config, security, deps, tenancy, rls
    app/modules/<domain>/    # router/schemas/models/service/repository
    app/marketplace/         # commission, ledger, payouts, balances, reconciliation
    app/media/               # image pipeline (public bucket + private KYC bucket)
    app/workers/             # ARQ: media, payout, dispatch, reconcile, notify, SLA
    app/tests/               # unit, integration, isolation, ledger, concurrency
mobile/
  packages/core/             # shared: OpenAPI Dio client, auth+refresh, design system,
                             # money/format, offline queue, push, i18n (en, bn)
  apps/buyer/                # storefront, split cart, checkout (COD + gateways), tracking
  apps/vendor/               # orders, stock edits, chat, earnings — scoped to own vendor
  apps/rider/                # deliveries, navigation, PoD, COD cash reconcile
docs/decisions/              # ADRs frozen in Phase 0 (commission, fees, payout, etc.)
infra/                       # docker-compose, Caddyfile, postgres init, backups, observability
```

## 5. Backend rules

Everything in the single-vendor constitution applies (vertical slices, async
throughout, `Numeric(12,2)` money, keyset pagination, Pydantic v2, idempotency,
`AppError`, audit log). Plus:

1. **Order splitting.** One buyer checkout creates one `orders` row and N
   `sub_orders`, one per vendor. Fulfilment, shipping, cancellation, and refunds
   operate on the sub-order. The parent order only aggregates; its status is
   **derived**, never stored.
2. **The ledger is append-only.** `ledger_entries` are never updated or deleted.
   A mistake is corrected with a reversing entry. Vendor balance is a SUM, or a
   cached projection reconciled nightly with an alert on drift.
3. **Commission is computed once, at capture, and stored on the sub-order.**
   Changing the commission rate never rewrites history.
4. **Payouts are idempotent, batched, and reconciled.** Every transfer records
   the provider reference. A payout run is idempotent on `(vendor_id,
   period_end)` — re-running produces zero new transfers.
5. **Vendor state gates everything.** `pending|approved|suspended|rejected|
   closed` is checked in a dependency — suspended vendors' products leave search
   within one revalidation cycle, and their open orders still get paid out.
6. **Never trust vendor input on money.** Vendors set prices and shipping rates,
   within admin-configured bounds, validated server-side.
7. **Payment verification is server-side.** The server verifies, the callback
   informs. Never trust a client redirect or an unverified callback — always
   re-query the provider's verify/validation API (bKash query, Nagad verify,
   SSLCommerz `validationserverAPI`, Stripe webhook signature over raw body)
   before marking paid.
8. **COD is a payment method with delivery-time settlement**, not "no payment."
   It reserves stock, creates the order, and books revenue only at remittance.
   COD risk controls: order-value cap, area allow-list, phone verification,
   abuse/return-rate scoring per buyer.
9. **Delivery dispatch** assigns riders by zone, load, rating, and COD-cash
   ceiling. A rider carrying too much uncollected cash gets no more COD.
   `delivered` requires valid proof-of-delivery (OTP or photo+signature). COD
   cash is tracked through `rider_cash_ledger` and settled via balanced ledger
   groups at remittance.

## 6. Frontend rules

Everything in the single-vendor constitution, plus:

1. The cart groups by vendor. Each group has its own shipping method, cost, and
   ETA. The totals panel shows per-vendor subtotals and one grand total.
2. A vendor cannot see another vendor's numbers anywhere — including in
   aggregate charts, leaderboard ranks (show "you are #7", not the full list with
   revenue), and error messages.
3. The vendor dashboard is a separate route group with its own layout, nav, and
   auth boundary. Do not reuse the admin layout with conditional rendering.
4. PDP shows the vendor card: name, logo, rating, response time, ships-from,
   return policy. Trust signals are product-page-critical in a marketplace.

## 7. Mobile rules (Flutter — buyer, vendor, rider)

Three apps, one shared package, one API. Do not fork three codebases.

1. **The app displays; the API decides.** Never compute price, tax, stock,
   commission, or a COD-collectible on device — render what the API returns.
2. **Tenancy is the API's job, but you must not undermine it.** The vendor app is
   scoped to `current_vendor`; it must never request or cache another vendor's
   data. An on-device cross-tenant call is a bug.
3. **State:** Riverpod, decided once in `packages/core`.
4. **Auth:** access token in memory, refresh in secure storage; rider builds
   carry only the `rider` scope; vendor builds authenticate as vendor staff.
5. **Money-moving calls carry an `Idempotency-Key`** (checkout, COD confirm) and
   survive a retry — never double-charge, never lose a captured PoD.
6. **Offline-first where it matters:** buyer cart and rider PoD/COD actions queue
   locally with a durable op id and sync with conflict resolution.
7. **Every screen has loading / empty / error states.** Deep links route push
   into the right screen and the right app.

## 8. Security baseline

Single-vendor baseline, plus:
- KYC documents go to a **private** bucket, served only via short-lived signed
  URLs to platform admins. Never through the CDN. Never in a public media table.
- Vendor staff accounts have roles within the vendor (`owner|manager|staff`) and
  cannot change payout bank details without owner re-authentication.
- Payout destination changes trigger a hold and an out-of-band notification —
  this is the single most-attacked flow in any marketplace.
- Vendor-to-buyer messaging is filtered for contact-detail exfiltration and rate
  limited.
- PoD assets (photos, signatures) go to the **private** bucket — the same
  security boundary as KYC.
- COD-cash ceiling per rider prevents over-accumulation of physical cash.

## 9. Testing

Single-vendor rules, plus a mandatory class of tests: **cross-tenant isolation.**
For every vendor-scoped endpoint, a test where vendor A requests vendor B's
resource by id and must receive 404 (not 403 — do not confirm existence).

Also required: multi-vendor cart checkout producing correct sub-orders, partial
refund affecting only one vendor's ledger, a payout run executed twice producing
one transfer, a payment callback replayed producing a no-op, and COD cash
reconciliation matching `rider_cash_ledger` to the platform ledger.

## 10. Definition of done

Single-vendor checklist, plus:
- [ ] Migration reversible + RLS policy migrated with its table
- [ ] `vendor_id` filter in the repository, not the caller
- [ ] Cross-tenant isolation test written and failing before the fix
- [ ] Ledger entries balance (debits = credits) where money moves
- [ ] Endpoint in OpenAPI; typed clients (web + 3 Flutter apps) regenerated
- [ ] No cross-vendor data in any aggregate, export, or error string
- [ ] Payment/COD paths verified server-side + idempotent callback proven
- [ ] Performance budget (architecture §6) still met
- [ ] Admin audit entry for any action taken on a vendor

## 11. Working style for Claude

Read before you write. Small diffs. State assumptions rather than stalling.
Say when you disagree. Never invent a library version. When a change touches
money or tenancy, say explicitly what you checked.

## 12. Performance budgets (the "super-fast" contract)

Speed is designed with numbers. Full table in `plan/00-architecture.md §6`. Hold
these on every change:

| Surface | Target |
|---|---|
| Storefront PDP / listing, edge-cached | TTFB < 50 ms p95 |
| Multi-vendor search | < 200 ms p95 @ 10k products / 50 vendors |
| Vendor dashboard read | < 150 ms p95 |
| Split checkout write | < 400 ms p95 |
| Grouped-cart quote | < 300 ms p95 |
| Payout run | idempotent, batched, no double transfer |
| Mobile PDP | LCP < 1.5 s / CLS < 0.05 / INP < 200 ms |
| Cross-tenant isolation | 100% of scoped endpoints have a passing 404 test |

**Query budget:** storefront PDP ≤ 3 queries, vendor dashboard widget ≤ 2,
grouped-cart quote ≤ 1 read round-trip per variant. RLS adds a predicate, not a
scan — every vendor table has a `vendor_id`-leading index. A change that regresses
a budget row is not done.

## 13. Plan-driven execution

The build is sequenced in `plan/`, phases 0–16 across 17 phase folders. The
protocol:

1. Open `plan/README.md`; find the lowest phase that isn't complete.
2. Read that `plan/phase-NN-<slug>/README.md`. Do not start until its **Gate-in**
   is green.
3. Work the **Work breakdown** top to bottom — honour the phase budget, the
   tenancy invariants (§3), and the money invariants (architecture §5).
4. Run the phase **Exit criteria** + **Demo script**. All green = phase done.
5. `/execute-phase <n>` automates 1–4. `/plan` reasons about or revises the plan.
   `/verify-gate <n>` checks gate-in/gate-out without executing.

Phase 0 (decisions) and Phase 1 (tenancy) are gates you do not cross early.
Retrofitting tenancy is a rewrite; a schema decision deferred to week 9 is the
most expensive mistake in the project.

## 14. Commands

`/bootstrap` `/feature` `/endpoint` `/page` `/migrate` `/test` `/review`
`/perf-audit` `/ship` `/vendor-module` `/payout-run` `/plan` `/execute-phase`
`/dispatch` `/mobile` `/verify-gate`
— see `.claude/commands/`.
