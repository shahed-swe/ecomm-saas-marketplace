# 00 — System Architecture v2 (Multi-Tenant SaaS Marketplace, Bangladesh)

> The reference model every v2 phase builds toward. It supersedes
> `plan/00-architecture.md`. Where a phase contradicts this file, this file wins,
> or you change this file deliberately, in a diff, with a reason.
>
> v1 was **one marketplace**. v2 is **a platform that hosts many marketplaces**:
> each client (a *tenant*) runs its own branded store, single- or multi-vendor, on
> its own domain, with its own payment accounts, couriers, theme, and white-label
> mobile apps. Four things are foundational and cannot be retrofitted:
>
> 1. **Two-level tenancy.** Every query is scoped by `tenant_id`, and inside a
>    tenant, vendor data is scoped by `vendor_id`.
> 2. **The order tree.** One payment, many vendor fulfilments.
> 3. **The ledger.** Per-tenant double-entry accounting.
> 4. **The theme contract.** Presentation is data (versioned JSON), never code
>    per tenant.
>
> Locked product decisions: `00-DECISIONS.md`.

Audience: the solo engineer (with Claude Code) who will own every tenant's money
and brand. Companion to `.claude/CLAUDE.md` (rules) and `02-data-model.md`
(columns).

---

## 1. Actors and trust boundaries

```
 Platform (us)
   └─ Super-admin ─────────► (platform) console: tenants, plans, billing, domains, health
 Tenant (our client = marketplace owner)
   ├─ Tenant staff ────────► (admin) + builder: owner|finance|support|moderator|content
   ├─ Vendor staff ────────► (vendor) web + vendor app: owner|manager|staff
   └─ Buyer / guest ───────► (shop) web + buyer app (white-label)
 Machines
   ├─ Payment providers (tenant's own bKash / SSLCommerz accounts) — verify-first
   └─ Couriers (tenant's own Pathao / Steadfast / RedX accounts) — signed webhooks + polling
```

**Five human boundaries, each with its own dependency, router prefix, and audit
stream.** No boundary is a flag on another boundary's endpoint.

| Boundary | Dependency | Sees |
|---|---|---|
| Super-admin | `require_platform_admin` | tenant metadata, billing, health. **Never** a tenant's buyers' PII by default; a support "break-glass" is time-boxed and audited |
| Tenant staff | `current_tenant_staff(permission)` | everything inside one tenant, filtered by permission |
| Vendor staff | `current_vendor` | one vendor inside one tenant |
| Buyer | `current_buyer` | own orders, addresses, wishlist, tickets |
| Guest | signed guest-order token | one order, by token + phone |

---

## 2. Container view

| Container | Runtime | Owns |
|---|---|---|
| **web** | Next.js standalone (one app, host-routed) | route groups `(shop)`, `(vendor)`, `(admin)`, `(builder)`, `(platform)` |
| **api** | FastAPI async | domain modules + `marketplace/` (ledger, payouts, tax) + `platform/` (tenants, billing, domains) + `theme/` |
| **worker** | ARQ, tenant-aware | media, search indexing, courier sync, payment recon, payout batches, invoices, notifications, campaigns, billing metering |
| **edge** | Caddy with **on-demand TLS** | subdomains + custom domains, auto certificates, gated by an `ask` endpoint |
| **db** | PostgreSQL 16 | shared schema, `tenant_id` and `vendor_id` RLS, append-only ledger |
| **cache** | Redis 7 | tenant resolution cache, published-theme cache, rate limits, queues, locks |
| **object store** | MinIO (S3 API) | buckets: `public-media`, `private-kyc`, `private-docs` (invoices, return photos), `builds` (app artifacts) |
| **secrets** | HashiCorp Vault (transit) | envelope encryption for tenant gateway and courier credentials |
| **mobile CI** | GitHub Actions + Fastlane | white-label builds per tenant |

**Hosting: a local Bangladesh data centre** (decision). Consequences we design for:
- **No managed cloud services.** Postgres, Redis, MinIO, and Vault are self-run, so
  backups, point-in-time recovery, and monitoring are Phase 1 work, not launch work.
- **A CDN in front of the DC** for static assets and ISR HTML (a provider with a
  Dhaka point of presence, or Cloudflare). Origin stays in BD, so data stays in BD.
- **An off-site backup copy** at a second BD location. One DC is one fire away
  from zero.
- Minimum footprint at launch: 2 app nodes, 1 Postgres primary + 1 streaming
  replica, 1 Redis, 1 MinIO (erasure-coded or replicated), 1 monitoring node.

---

## 3. Tenancy architecture (the most important section)

### 3.1 Two layers, one pattern

```
Host header ─► TenantResolver ─► tenant_id ─┐
JWT (tid, vid, role) ────────────────────────┼─► deps ─► TenantScopedRepo(tenant_id)
                                              │            └─► VendorScopedRepo(tenant_id, vendor_id)
                                              └─► SET LOCAL app.tenant_id, app.vendor_id ─► PG RLS
```

1. **Tenant is resolved from the host, not from the client.** `domains(host →
   tenant_id)` is cached in Redis. The JWT carries `tid`, and **a token whose `tid`
   differs from the host's tenant is rejected** (401), so a buyer token from store
   A replayed on store B is dead.
2. **Every tenant-owned table carries `tenant_id NOT NULL`.** Vendor-owned tables
   carry both `tenant_id` and `vendor_id`. Every unique constraint leads with
   `tenant_id`: `(tenant_id, slug)`, `(tenant_id, vendor_id, sku)`.
3. **Repositories cannot be built without scope.** `TenantScopedRepository`
   requires `tenant_id`; `VendorScopedRepository` requires both.
4. **RLS is the second layer.** Policy on tenant tables:
   `tenant_id = current_setting('app.tenant_id')::uuid`. Vendor tables add
   `AND (current_setting('app.vendor_id', true) IS NULL OR vendor_id = current_setting('app.vendor_id')::uuid)`.
   The app role has **no BYPASSRLS**. Platform jobs use a separate DB role, only
   inside `app/platform/`.
5. **Workers are tenant-aware.** Every job payload carries `tenant_id` (and
   `vendor_id` where relevant). The job wrapper sets the RLS variables before
   the handler runs, and a job without `tenant_id` fails at enqueue.
6. **Everything else is prefixed too:** Redis keys `t:{tenant_id}:…`, object keys
   `t/{tenant_id}/…`, search documents filtered by tenant, ISR cache tags
   `t:{tenant_id}:…`, log fields `tenant_id`, `vendor_id`.

### 3.2 Isolation is proven on a grid, not a line

The mandatory harness is a **2×2**: tenant A vs tenant B, and vendor A1 vs vendor
A2 inside tenant A. For every scoped endpoint:
- cross-tenant access → **404**
- cross-vendor access inside a tenant → **404**
- the same with the app filter removed → RLS returns **zero rows**

Markers used by `02-scenarios.md`: **ISO-T** (tenant) and **ISO-V** (vendor).

### 3.3 Where leaks actually happen in SaaS

Cache keys without the tenant prefix. ISR pages cached by path instead of host.
Search facets. Sitemaps. Password-reset and OTP lookups by phone or email with no
tenant filter. Webhooks from a courier or gateway that trust a payload field
instead of the credential that signed it. Export files in a shared bucket path.
Push topics shared across white-label apps. Analytics pixels firing another
tenant's ID. **Each has a named test in Phase 2 or the phase that introduces it.**

### 3.4 Users are per tenant

A buyer account belongs to one tenant: `users(tenant_id, phone)` and
`users(tenant_id, email)` are unique **per tenant**. The same phone number in two
stores is two accounts, which is what a white-label brand expects. Platform
super-admins live in a separate `platform_users` table.

### 3.5 Store mode: single or multi, no branch

`tenants.store_mode ∈ {single, multi}`. **Single mode is not a different code
path:** it auto-creates one *house vendor* (`vendors.is_house = true`) and hides
vendor-facing UI. Every order still has sub-orders and the ledger still runs.
Switching single → multi is a settings change plus enabling vendor signup. There
is no `if multi_vendor` inside a query, ever.

### 3.6 Noisy neighbours and limits

Plan entitlements (`plan_limits`: vendors, products, staff, custom domains, apps,
monthly orders) are checked in a dependency. Rate limits are per tenant *and* per
IP. Heavy jobs (bulk CSV import, campaign push) run on a per-tenant concurrency
cap so one tenant's 50k-row import cannot starve another tenant's checkout.

---

## 4. Domains and edge

- **Free subdomain:** `{slug}.{platform-domain}` via a wildcard certificate.
- **Custom domain:** the tenant adds `shop.example.com` → we show a CNAME (or A)
  record plus a TXT verification token → a worker verifies DNS → status
  `pending → verified → active`.
- **TLS:** Caddy on-demand TLS, whose `ask` endpoint returns 200 only for
  `active` domains, so nobody can make us mint certificates for arbitrary hosts.
- **One primary host per tenant.** The others 301 to it (SEO). Admin and vendor
  dashboards live at `/admin` and `/vendor` on the primary host, plus an
  optional `admin.{primary}`.
- **Deep links:** `/.well-known/apple-app-site-association` and `assetlinks.json`
  are generated **per host** from the tenant's app bundle IDs.

---

## 5. The theme and page-builder architecture (presentation as data)

### 5.1 The model

```
theme_presets (platform-owned, versioned)      e.g. "Minimal", "Bazaar", "Fashion"
   │ tenant picks one
tenant_theme ── draft_version_id ─┐
             └─ published_version_id ─► theme_versions (immutable JSON snapshot)
                                          ├─ tokens      colors, dark palette, fonts, radius, spacing scale, button style
                                          ├─ brand       logo, logo_dark, favicon, app_icon asset ids
                                          ├─ layouts     header, footer, menus
                                          ├─ templates   home | category | product | search | store | cart
                                          │              each = ordered [section{id,type,settings,blocks?}]
                                          ├─ pages       custom pages (about, faq, policy…) = ordered sections
                                          └─ custom_css  sanitised, scoped (see 5.5)
vendor_store_theme (limited) ─► accent color (within tenant palette), banner, logo,
                                ordered subset of `vendor_allowed` sections
```

**Why snapshots:** publish = write a new immutable `theme_versions` row, then flip
`published_version_id` in one transaction. **Rollback = flip the pointer back.**
Preview = render `draft_version_id` behind a signed, short-lived preview token,
with no caching.

### 5.2 One section registry, three consumers

Section types are declared once, in `packages/theme-schema/sections/*.json` (JSON
Schema). From that single source we generate:
- **Pydantic models:** the API validates every save, so an invalid section can
  never be stored.
- **TypeScript types plus the builder's settings forms:** the admin editor renders
  the form from the schema, so a new section needs no hand-built form.
- **Dart models:** the Flutter home screen renders the same section list.

A section declares `pages` (where it may appear), `vendor_allowed`, `max_per_page`,
`mobile_supported`, and its data source (e.g. `product_carousel.source =
category|collection|campaign|manual`). **Sections never contain queries.** They
reference IDs; the renderer fetches through the normal scoped API with a
per-page query budget.

**V1 section library (~20):** announcement bar, hero slider, image banner,
banner grid, category grid, product carousel, product grid, flash-sale countdown,
campaign strip, brand strip, top vendors, featured vendor, rich text, image+text,
FAQ accordion, testimonials (tenant-entered and labelled), newsletter/app
download, trust badges, video embed (allow-listed hosts), spacer/divider. PDP and
category templates expose **blocks** as positional slots: gallery style, info
order, specs, Q&A, reviews, related products, grid columns, filter position.

### 5.3 Rendering path (fast by construction)

```
request(host, path) ─► resolve tenant (Redis) ─► load published theme (Redis, ~10 KB)
  ─► <style>:root{--color-primary:…} [data-theme=dark]{…}</style>   (no rebuild, no FOUC)
  ─► template.sections.map(type → <SectionComponent settings>)      (RSC, streamed)
  ─► ISR, cache tag t:{tid}:theme + per-data tags
publish ─► revalidateTag(t:{tid}:theme) ─► CDN purge by tag/host
```

- **Tokens are CSS variables.** Tailwind maps to `var(--…)`, and no hex value
  appears in components. Dark mode is a second token set.
- **Budgets:** theme payload < 20 KB; ≤ 25 sections per page; home page ≤ 6 data
  queries; publish → live < 60 s; builder save < 300 ms p95.
- **Contrast guard:** the builder blocks publishing a palette whose text/background
  pairs fail WCAG AA.
- **Fonts:** a curated list (Latin + Bangla pairs, self-hosted), so no arbitrary
  font URLs are allowed.

### 5.4 Builder UX (admin `(builder)`)

Left panel: templates and pages → section list, drag to reorder (dnd-kit),
add/hide/duplicate/delete. Right panel: schema-driven settings form. Centre: an
iframe preview (desktop/tablet/mobile widths, light/dark) that receives draft
updates by `postMessage`, so no reload is needed. Top bar: autosave draft ·
preview link · publish · version history (who, when, diff summary) · rollback.
Permission `theme.publish` is separate from `theme.edit`.

### 5.5 Custom CSS (a controlled risk)

- Parsed with a real CSS parser (lightningcss) on save; **rejects** `@import`,
  `expression`, `behavior`, `javascript:`, `url()` outside the tenant's own media
  host, `@font-face` from foreign hosts, and `position: fixed` overlays covering
  more than a threshold area.
- **Every selector is prefixed** with `[data-tenant-css]` so it cannot style the
  admin, builder, or checkout payment step. Checkout and payment pages **ignore**
  custom CSS entirely (anti-phishing).
- Size cap 50 KB; served as its own hashed stylesheet; the CSP forbids inline
  script, so CSS is the ceiling (no custom JS in V1).

### 5.6 Mobile (white-label)

On launch, the app fetches `GET /app/bootstrap` (tokens, brand, home sections,
remote config, min version) → caches it → renders. Colors and logo apply
app-wide; **home sections are server-driven**; other screens use fixed layouts
that consume tokens. The icon, splash, and app name are baked in at build time
(see §11).

---

## 6. Order tree (unchanged core, v2 additions)

```
orders (tenant_id, buyer_id | guest_token, derived status, totals, VAT totals)
  └─ sub_orders (tenant_id, vendor_id, status, courier, shipping fee, commission snapshot, VAT snapshot)
       └─ order_items (snapshots incl. VAT rate, discount allocation)
       └─ shipments (courier, consignment_id, label, tracking events, COD amount)
       └─ return_requests ─► return_items ─► reverse_shipment ─► qc_result ─► refund
```

- The parent status is **derived**; partial states are normal.
- **Deterministic lock order** across variants: `(tenant_id, variant_id)` ascending.
- **Stock reservation TTL:** 15 min for prepaid pending payment. COD reserves
  until courier handover or cancel (max 72 h, then an ops review queue).
- **Order expiry:** unpaid prepaid orders cancel at TTL, and reservations release
  in the same transaction.
- **Guest checkout:** phone OTP-verified guest, `guest_token` signed link for
  tracking and returns; converts to an account by claiming the phone.
- **Address book:** division → district → upazila/thana → area (seeded BD
  geography table), plus free-text line and landmark. The courier zone mapping
  lives on the geography table, so no courier guesses from free text.

---

## 7. Money architecture

### 7.1 Where money physically sits

**Buyer money goes to the tenant's own merchant accounts** (decision). The
platform never holds tenant GMV. So there are **two separate books**:

| Book | Owner | Tracks |
|---|---|---|
| **Tenant ledger** (`ledger_entries`, per tenant) | tenant | buyer payments, vendor payables, tenant commission, gateway fees, courier fees, COD receivables, refunds, reserves, VAT/TDS, payouts |
| **Platform billing** (`platform_invoices`) | us | subscription, GMV % fee, setup fee — billed *to* the tenant |

### 7.2 One chart of accounts (fixes the v1 name drift)

Every entry: `(tenant_id, group_id, account, vendor_id?, direction debit|credit,
amount > 0, entry_type, ref_type, ref_id)`. **Σdebits = Σcredits per group.**

| Account | Normal | Meaning |
|---|---|---|
| `gateway_clearing:{provider}` | debit | money the gateway owes the tenant |
| `courier_cod_receivable:{courier}` | debit | COD cash held by courier |
| `tenant_bank` | debit | settled cash |
| `vendor_payable` (per vendor) | credit | owed to vendor |
| `tenant_commission_revenue` | credit | tenant's take |
| `gateway_fee_expense` / `courier_fee_expense` | debit | costs |
| `shipping_fee_revenue` | credit | shipping charged to buyer when tenant-billed |
| `promo_expense_tenant` / `promo_contra_vendor` | debit | discounts by funder |
| `buyer_refund_payable` | credit | refunds owed |
| `store_credit_liability` (per buyer) | credit | wallet balance |
| `vat_output_payable` / `tds_payable` | credit | tax owed to NBR |
| `reserve_hold` (per vendor) | credit | return-window and dispute holds |

`entry_type` enum (single source): `capture, cod_delivery, cod_settlement,
commission, gateway_fee, courier_fee, shipping_fee, discount, refund,
return_reversal, reserve_hold, reserve_release, dispute_hold, dispute_release,
payout, vat, tds, store_credit_issue, store_credit_redeem, adjustment`.

### 7.3 Invariants (carried from v1, still non-negotiable)

1. Append-only; mistakes get reversing groups.
2. Every group balances, tested on every money path.
3. Commission **and VAT** are snapshotted on the sub-order at capture (prepaid) or
   at courier-confirmed delivery (COD).
4. Payable = Σ entries − holds − return-window reserve − disputes. **One reserve
   rule** per tenant (percent *or* window, configured, never both stacked).
5. Payout batches are idempotent on `(tenant_id, vendor_id, period_end)`.
6. Nightly reconciliation, per tenant, per provider, per courier: ledger ↔
   provider/courier statement ↔ projection. Drift raises an alert and is never
   overwritten.

### 7.4 Promotions in the ledger

- **Vendor coupon:** the discount reduces `vendor_payable`. Commission is computed
  on the **post-discount** price.
- **Flash sale / campaign:** the vendor's campaign price *is* the price (a snapshot),
  with no ledger discount line. An optional tenant subsidy posts to
  `promo_expense_tenant`.
- **Free-shipping rule:** is per vendor group, or per tenant with cost absorbed
  by the funder named on the rule. It posts to the funder's expense.
- **Proration:** allocated to order items by line value, largest-remainder
  rounding, and stored on `order_items.discount_amount`.

### 7.5 Tax (Bangladesh)

- **VAT:** tenant-level config (registered or not, BIN, inclusive or exclusive
  pricing, default rate). Category and product overrides are supported. VAT is
  computed server-side in the quote and snapshotted per line.
- **Invoices:** a Mushak-6.3-style tax invoice PDF per sub-order (seller = vendor,
  or tenant in single mode), numbered per tenant, stored in `private-docs`.
  Credit notes are issued on refund.
- **TDS/AIT on vendor payouts:** a configurable rate per vendor type, withheld at
  payout into `tds_payable`, with a certificate export.
- Rates and thresholds are **configuration, not code**. The tenant's accountant
  confirms them; the plan never hardcodes a statutory rate.

---

## 8. Payments (tenant-owned accounts)

One `PaymentGateway` interface; credentials resolved per tenant from Vault.

| Method | Flow | Paid when | Ledger |
|---|---|---|---|
| **COD** | order → courier handover → delivered → courier settles | courier marks delivered **and** COD collected | `cod_delivery` at delivery, `cod_settlement` when courier payout matches |
| **bKash** (tokenized) | create → buyer approves → execute → **query** | verified execute | `capture` |
| **SSLCommerz** | session → hosted page → IPN → **validation API** | validated IPN | `capture` |

Rules:
1. **The server verifies; the callback informs.** Always re-query before paid.
2. **Callbacks are routed by tenant**
   (`/webhooks/{provider}/{tenant_public_id}`) but **authenticated by that
   tenant's credential**. The path picks the tenant; the signature or verify
   call proves it.
3. **Idempotency:** `payment_events (tenant_id, provider, event_id)` UNIQUE;
   `Idempotency-Key` on create and capture.
4. **Credential health check** on save (sandbox or live ping), plus a "test
   payment" button in admin. A tenant with broken credentials sees the method
   auto-hidden at checkout, not a failed payment.
5. **COD risk controls** per tenant: value cap, area allow-list, OTP-verified
   phone, buyer refusal-rate score, optional **partial advance** (e.g. shipping
   fee prepaid via bKash), and a blocklist.
6. **Refunds:** bKash refund API; SSLCommerz refund API; COD buyers → bKash/bank
   manual refund task or store credit (buyer's choice). Every refund is a
   reversing group plus a credit note.
7. **Chargebacks and disputes** from providers enter the dispute module as
   `dispute_hold`.
8. **Platform billing** (tenant pays us) uses **our** bKash/SSLCommerz account,
   through the same interface with a platform credential scope.

---

## 9. Delivery (courier-only)

```
sub_order ready_to_ship ─► CourierAdapter.book(parcel) ─► consignment_id + label PDF
   ─► webhook/poll status ─► normalised: booked → picked_up → in_transit → out_for_delivery
        → delivered | partial_delivered | failed_attempt | returning | returned | cancelled
   ─► COD: courier collects ─► courier settlement statement ─► match per consignment ─► ledger
```

- **Adapters:** Pathao, Steadfast, RedX behind one `CourierAdapter` (quote,
  book, cancel, track, label, parse_webhook, fetch_settlements). Credentials are
  per tenant (or per vendor, if the tenant allows vendors their own courier
  accounts).
- **Courier selection:** a tenant rule by destination (district/area), weight,
  COD amount, and courier health, with manual override by vendor or admin.
- **Status normalisation** is one table per courier mapped to our enum. Unknown
  statuses go to an ops queue, never silently dropped.
- **Polling fallback:** webhook-first where supported; a poller every 15–30 min
  for non-terminal shipments; a stuck-shipment alert after an SLA.
- **COD settlement reconciliation:** import the courier payout statement (API or
  CSV) → match consignments → post `cod_settlement` → flag short-pays, missing
  parcels, and fee mismatches.
- **Multi-vendor orders:** one shipment per sub-order (vendors ship from
  different places). The buyer sees one timeline per vendor, and the cart shows
  the per-vendor delivery fee up front.
- **Reverse logistics:** return pickup is booked through the same adapter.

---

## 10. Vendor payouts (bank + bKash, manual approval)

1. The weekly or configured job builds a **payout batch** per tenant: payable per
   vendor after reserves, holds, disputes, and TDS; below-minimum vendors roll
   over.
2. The tenant `finance` role reviews and **approves** (maker-checker: the
   preparer cannot approve, when the tenant has ≥ 2 finance users).
3. Export: a **bank file** (BEFTN/NPSB CSV in the format the tenant's bank
   accepts; templates per bank) and a **bKash disbursement list**.
4. Finance marks each line paid with the bank or bKash reference → a `payout`
   ledger group posts. Idempotent; re-marking is a no-op.
5. **Payout-destination change** = owner re-auth + OTP + 72 h hold + SMS/email to
   the old and new contact. It is still the most-attacked flow.

---

## 11. Client surfaces

| Surface | Tech | Users |
|---|---|---|
| web `(shop)` | Next.js RSC + ISR, host-routed | buyers, guests |
| web `(vendor)` | Next.js | vendor staff |
| web `(admin)` + `(builder)` | Next.js | tenant staff |
| web `(platform)` | Next.js, separate auth | super-admin |
| **buyer app** | Flutter, white-label per tenant | buyers |
| **vendor app** | Flutter, white-label per tenant (or one generic vendor app, a per-plan option) | vendor staff |
| api + workers | FastAPI + ARQ | all |

**Contract rule (unchanged):** clients display what the API returns; they never
compute price, VAT, commission, stock, or COD amount.

### White-label build pipeline

```
tenant_apps (tenant_id, platform ios|android, bundle_id, app_name, icon_asset, splash_asset,
             store_account_ref, firebase_app_id, version, status)
 ─► CI job (per tenant, per platform): fetch config → generate flavor (name, ids, icons via
    flutter_launcher_icons, splash, google-services/GoogleService-Info) → build → sign
    (tenant keystore / App Store Connect API key from Vault) → upload to tenant's store account
    (internal track / TestFlight) → record build
```

- **One codebase, zero per-tenant code.** A tenant difference that needs code is a
  feature flag or a section, not a fork.
- **Push:** one Firebase project on our side with one app registration per tenant
  app; APNs keys from the tenant's Apple account; topics prefixed `t_{tenant}`.
- **Store policy:** Apple guideline 4.2.6 rejects template-spam apps built from the
  same code. Mitigations: the tenant's own developer account, real brand and
  content, distinct metadata. **This is a launch risk to state to clients up
  front.**
- **Required by stores:** in-app account deletion, privacy labels, force
  update (`min_supported_version` from bootstrap), crash reporting
  (Sentry/Crashlytics) from the first build.

---

## 12. SaaS platform (`app/platform/`)

- **Tenants lifecycle:** `trial → active → past_due → suspended → cancelled
  → purged`. Suspended = storefront shows a maintenance page, admin read-only,
  and **in-flight orders still fulfil and settle**. Purge happens after a retention
  window, with a data export first.
- **Plans and entitlements:** `plans`, `plan_limits`, `tenant_subscriptions`,
  checked by `require_entitlement("custom_domain")`.
- **Billing:** subscription invoices (monthly/yearly), a **GMV fee** metered
  nightly from the tenant ledger (captured minus refunded, per plan %), a one-time
  setup fee, and dunning (reminders → grace → past_due → suspend). Invoices carry
  VAT if the platform is registered.
- **Tenant onboarding wizard:** store name → mode (single/multi) → preset theme
  → logo/colors → subdomain → payment credentials → courier credentials → first
  product → go live checklist.
- **Support access:** super-admin impersonation is time-boxed, requires a reason,
  and is written to the tenant's audit log, which the tenant can see.

---

## 13. Cross-cutting

- **Auth:** email+password (argon2id), **phone OTP** (SMS gateway; rate-limited,
  per tenant+phone+IP, 6 digits, 5-min TTL, hashed at rest), guest OTP. JWT 15 min
  plus a rotating refresh cookie, with the `tid` claim bound to the host.
- **RBAC:** permission strings (`orders.read`, `payouts.approve`, `theme.publish`
  …); roles are named bundles; tenant owner is immutable.
- **Audit log:** every staff, vendor, and super-admin mutation, with a viewer in
  admin (filter by actor/entity/date), 2-year retention, and exportable.
- **i18n:** en + bn on web and apps; Bangla numerals optional per tenant; `৳`
  formatting in one package per platform.
- **Search:** Postgres tsvector (simple config) + pg_trgm, plus a **Banglish
  transliteration and synonym table** per tenant. Meilisearch is the scaling step.
- **SEO:** per-host sitemaps, robots, canonical, JSON-LD Product/Offer/Store/
  Breadcrumb, OG images, hreflang en/bn.
- **Marketing:** per-tenant GA4 ID, Meta Pixel + Conversions API (server-side,
  deduplicated by event_id), GTM optional, cookie consent banner.
- **Observability:** OpenTelemetry traces with `tenant_id`/`vendor_id`,
  Prometheus + Grafana, Loki logs, Sentry. Per-tenant dashboards for support.
- **Backups:** WAL archiving + base backups (pgBackRest) → point-in-time recovery,
  RPO ≤ 5 min, RTO ≤ 2 h. MinIO replicated off-site. **A restore drill every
  month, starting Phase 1.**
- **Environments:** local → staging (same topology, anonymised seed) →
  production. Staging exists from Phase 1.
- **Privacy:** buyer data export and deletion (anonymise, keep ledger), PII
  encryption for phone/NID at rest, KYC documents private.

---

## 14. Performance budgets

| Surface | Target |
|---|---|
| Storefront page (cached) edge TTFB | < 80 ms p95 in Dhaka (< 50 ms with a local PoP) |
| Storefront uncached SSR | < 400 ms p95 |
| Tenant resolution + theme load | < 5 ms p95 (Redis) |
| Search @ 20k products / 100 vendors / tenant | < 200 ms p95 |
| Grouped-cart quote (incl. VAT, promos, courier fee) | < 300 ms p95 |
| Split checkout write | < 400 ms p95 |
| Vendor/admin dashboard read | < 150 ms p95 |
| Builder save / preview update | < 300 ms / < 150 ms |
| Theme publish → live | < 60 s |
| Payment callback handler | < 500 ms p95 |
| Mobile home (server-driven) | LCP < 1.5 s on mid Android, cached bootstrap |
| Isolation | 100% scoped endpoints pass ISO-T and ISO-V |

---

## 15. Failure modes

| Failure | Response |
|---|---|
| Tenant filter forgotten | RLS returns 0 rows; ISO-T test fails in CI |
| Token replayed on another tenant's host | `tid` ≠ host tenant → 401 |
| Theme JSON invalid or malicious | schema validation on save; CSS sanitiser; checkout ignores custom CSS |
| Bad publish breaks storefront | one-click rollback (pointer flip), publish error-rate alarm |
| Tenant's gateway credentials revoked | health check hides method; admin alert |
| Courier webhook missed | poller catches up; stuck-shipment alert |
| Courier short-pays COD | settlement mismatch queue; ledger not overwritten |
| Payout file uploaded twice at bank | batch lines carry unique reference; marked-paid idempotent; bank-side dedupe advised |
| One tenant floods jobs | per-tenant concurrency caps + queue fairness |
| DC outage | replica promote runbook; off-site backups; status page |
| Custom domain DNS moved away | verifier marks `inactive`; Caddy `ask` stops issuing |
| App store rejects white-label app | tenant-account submission, metadata checklist, fallback PWA |

---

## 16. What we still deliberately defer

Custom JavaScript in themes · third-party theme marketplace · multi-currency ·
multi-warehouse · subscriptions for buyers · auctions · B2B pricing · vendor ads ·
Nagad and international cards via Stripe · own rider fleet · Kubernetes. Each is
a later project on a platform that is already correct.
