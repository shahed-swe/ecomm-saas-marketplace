# plan-v2 — Product Decisions (locked 2026-09-16)

Source: Q&A with Dev before rewriting the multi-vendor plan. These override `plan/` wherever they conflict.

## Business model
- **SaaS, multi-client.** One platform, many marketplace owners (clients).
- Tenancy: **shared DB + `tenant_id` + RLS**, layered above `vendor_id` (platform → tenant → vendor).
- Store mode: **both** — a tenant can run single-vendor or multi-vendor (same codebase; toggle).
- Domains: **free subdomain + custom domain** with auto SSL.
- SaaS billing: **subscription (monthly/yearly plans) + GMV % fee + one-time setup fee**.
- Hosting: **local Bangladesh data center**.

## Theme & UI customization
- Tenant owner: **full section-based builder**; vendor: **limited** (store logo, banner, color, a few sections).
- Editable: **Homepage, Header/Footer/Menu, custom pages (About/FAQ/Policy), Product & Category page layout**.
- Section-based (ready section library, drag-drop reorder, per-section settings); no free-form layout.
- Extras: **draft → preview → publish + version rollback**, **dark mode**, **custom CSS box** (sanitised, scoped).
- Mobile: **color/logo + homepage sections** from API (server-driven); other screens fixed.

## Mobile
- **White-label apps per tenant** (buyer + vendor) built from one Flutter codebase via CI; published to tenant's store accounts.
- **Rider app removed** (courier-only delivery).

## Payments & money
- Money goes to the **tenant's own merchant accounts** (tenant enters bKash / SSLCommerz credentials).
- V1 methods: **COD, bKash, SSLCommerz**. Nagad and Stripe dropped.
- Vendor payouts: **bank (BEFTN/NPSB) + bKash, calculated by system, manually approved** by tenant admin, reference recorded.
- Double-entry ledger stays (per tenant).

## Delivery
- **Courier API only**: Pathao, Steadfast, RedX. COD collected by courier → courier settlement reconciliation.

## Auth
- Buyer: **email + password, phone OTP, guest checkout**.

## Orders & promotions
- **Full return/RMA flow**: request (reason + photo) → approve → courier reverse pickup → QC → refund (bKash/bank/store credit; COD buyers via bKash/bank).
- Promotions: **vendor coupons, flash sales/campaigns, free-shipping rules**.

## V1 scope (all in)
- Buyer extras: wishlist, product Q&A, recently viewed, brands, BD address (division/district/upazila).
- Vendor/admin: bulk CSV upload, admin staff roles (finance/support/moderator), audit log viewer.
- Marketing & support: GA4/Meta Pixel, push campaigns, abandoned cart, support tickets.
- VAT/Mushak invoice + TDS on payouts; Bangla web (en/bn) + Banglish search.
- Platform fixes from audit: admin-managed category taxonomy + attributes, unified ledger account names & enums, consistent ADR numbering, force-update/remote config, account deletion, staging + backups from Phase 1.

## Team & timeline
- **Solo dev + Claude Code.** All features in V1 (accepted long timeline, est. 12–14+ months).
