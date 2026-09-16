# Execution Plan v2 — Multi-Tenant SaaS Marketplace (Bangladesh)

The **build order** for the v2 platform: a SaaS that hosts many branded
marketplaces. Each tenant runs single- or multi-vendor, on a subdomain or a
custom domain, with a section-based theme builder, its own bKash/SSLCommerz
accounts, Pathao/Steadfast/RedX couriers, and white-label buyer and vendor apps.

- **Decisions:** `00-DECISIONS.md` (locked).
- **Architecture:** `00-architecture.md` (why and how).
- **What changed from v1 and why:** `CHANGES-v1-to-v2.md`.

> **Status of this folder:** architecture + phase map (this file) are written.
> Phase folders (`phase-NN-*/` with README, sub-phase docs, `files.md`) are written
> **after this map is approved**, in order.

---

## Build profile

- **Team:** one engineer + Claude Code. Phases are **strictly sequential**; no two
  phases run at once.
- **Scope:** all V1 features in (decision). Estimated **~58 working weeks
  (~13–14 months)** including buffer weeks.
- **Shippable checkpoints:** because v1 scope is large for one person, the map is
  cut so that the platform is *demo-able to a real client* at three points (◆).
  This is not a scope cut; it is a way to start earning and getting feedback
  before month 14.

---

## Phase map

| # | Phase | Weeks | Ships (demo at exit) |
|---|---|---|---|
| 0 | **decisions-adrs** | 1 | Signed ADRs: tenancy, chart of accounts, VAT/TDS, payouts, courier, returns, promotions funding, billing, hosting |
| 1 | **foundation-infra** | 2 | Monorepo, Docker, BD-DC topology, **staging**, CI, PITR backups + restore drill, observability, OpenAPI codegen (TS + Dart) |
| 2 | **saas-tenancy-domains** | 3 | Tenant A vs B and vendor A1 vs A2 → **404** at app **and** RLS; subdomain + custom domain with auto-TLS; `(platform)` console skeleton |
| 3 | **identity-rbac-audit** | 2 | Email + phone OTP + guest login per tenant; tenant staff roles; vendor staff roles; audit log + viewer |
| 4 | **tenant-setup-theme-engine** | 2 | Onboarding wizard; store mode single/multi (house vendor); **theme presets, tokens, logo/colors/fonts, dark mode** live on storefront shell |
| 5 | **saas-billing-plans** | 2 | Plans + entitlements, subscription invoices, GMV fee metering stub, setup fee, dunning → suspend |
| 6 | **vendor-onboarding-management** | 3 | Signup → KYC private bucket → approval; store settings; staff; **bank/bKash payout method with re-auth + hold**; invite codes, commission overrides |
| 7 | **catalog-media-taxonomy** | 3 | Admin categories + attributes, brands, products/variants, image pipeline, moderation, **bulk CSV import**, product Q&A |
| 8 | **page-builder** | 4 | Drag-drop sections for home, header/footer/menus, custom pages, PDP/category layouts; vendor limited store builder; draft → preview → publish → rollback; custom CSS sandbox |
| — | ◆ **Checkpoint A** | — | *A client can set up a branded catalogue site on their own domain* |
| 9 | **discovery-search-seo-i18n** | 3 | Tenant-scoped search < 200 ms p95 with Banglish; facets; store pages; wishlist, recently viewed; sitemaps + JSON-LD; en/bn web |
| 10 | **cart-checkout-promotions-tax** | 3 | Grouped cart, BD address book, guest checkout, order tree + deterministic locks, reservation TTL; **vendor coupons, flash sales, free-shipping rules**; VAT in quote |
| 11 | **payments-tenant-gateways** | 3 | Tenant credentials in Vault; **COD (risk controls), bKash, SSLCommerz**; verify-first; idempotent callbacks; per-tenant recon |
| 12 | **fulfilment-courier** | 3 | Vendor fulfils sub-order → **Pathao/Steadfast/RedX** booking + label → tracking normalised → COD settlement reconciled to ledger |
| 13 | **returns-refunds-credit** | 2 | Return request → approve → reverse pickup → QC → refund to bKash/bank/**store credit**; credit notes |
| 14 | **ledger-payouts-tax-docs** | 4 | Unified chart of accounts; balances + reserves; **payout batch → bank file / bKash list → manual approve → mark paid**; Mushak-style invoices; TDS |
| — | ◆ **Checkpoint B** | — | *A client can run a real marketplace on web end to end: sell, ship, return, pay vendors* |
| 15 | **trust-safety-messaging** | 3 | Disputes with holds, verified reviews, vendor scoring, suspension, buyer↔vendor chat with exfiltration filter |
| 16 | **notifications-marketing-support** | 3 | One notification service (push/SMS/email/in-app); push campaigns; abandoned cart; GA4 + Meta Pixel/CAPI per tenant; **support tickets** |
| 17 | **analytics-reporting** | 2 | Tenant, vendor (isolated), and platform (MRR, GMV fee, tenant health) dashboards; exports |
| 18 | **mobile-core-buyer-app** | 4 | `packages/core`, runtime theme + server-driven home, buyer app full journey, force update, account deletion, crash reporting |
| 19 | **vendor-app-whitelabel-pipeline** | 3 | Vendor app; **per-tenant CI build → sign → upload** to tenant store accounts |
| — | ◆ **Checkpoint C** | — | *A client gets branded iOS/Android apps* |
| 20 | **hardening-launch** | 3 | Security review, 2×2 isolation audit, multi-tenant load test, DR drill, runbooks, store submissions, go/no-go |
| — | **buffer** | 5 | Distributed: 1 week after phases 2, 8, 11, 14, 19 |

**Total:** 58 build weeks + 5 buffer weeks ≈ 63 weeks. That is ~13–14 months at a
realistic solo pace, including buffers.

---

## Gate discipline

Each phase's exit criteria are the next phase's gate-in. The existential gates:

- **2 → 3:** tenant **and** vendor isolation proven on the 2×2 grid, at app and
  RLS layers. **Retrofitting tenancy is a rewrite.**
- **4 → 8:** the token and CSS-variable contract is frozen before any section is
  built; no component may contain a hardcoded color.
- **10 → 11:** order tree + VAT + promotion proration correct before real money.
- **11 → 12:** payment verification bulletproof before a courier collects COD.
- **12 → 14:** payouts need courier-confirmed delivery + COD settlement.
- **14 → 15:** disputes reverse ledger entries; the ledger must balance first.
- **17 → 18:** the API is stable before the apps; apps inherit, never re-implement.

A red gate is a stop.

---

## Definition of Done (every task)

- [ ] Migration reversible; **RLS policy (tenant, and vendor where applicable) migrated with its table**
- [ ] Scope enforced in the **repository**; unique constraints lead with `tenant_id`
- [ ] ISO-T and ISO-V tests written **failing first**
- [ ] Redis keys, object keys, ISR tags, and search docs carry the tenant prefix
- [ ] Ledger groups balance where money moves (LDG)
- [ ] Endpoint in OpenAPI; TS + Dart clients regenerated
- [ ] No hardcoded color, string, or currency format in UI (tokens + i18n)
- [ ] Payment/courier callbacks verified server-side and idempotent
- [ ] Audit log entry for every staff/vendor mutation
- [ ] Performance budget (architecture §14) still met

---

## Execution protocol

1. Read `00-DECISIONS.md` and `00-architecture.md` once.
2. Open the lowest-numbered unfinished phase folder; check gate-in.
3. Work its sub-phases in the order its README lists.
4. Run exit criteria + demo script. All green → next phase.
5. `/execute-phase <n>` automates 2–4 (after the `.claude` bundle is updated for v2).
