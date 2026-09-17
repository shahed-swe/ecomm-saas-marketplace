# Build progress

| Phase | Status | Notes |
|---|---|---|
| 0 decisions-adrs | ✅ done | 14 ADRs |
| 1 foundation-infra | ✅ done | skeleton, migration, roles, worker guard, metrics, infra, backups, CI, deploy; 9 api tests |
| 2 saas-tenancy-domains | ✅ done | 2x2 isolation (app+RLS), host resolution, platform tenants, custom domains + TLS gate; 47 api tests |
| 3 identity-rbac-audit | ✅ done | email+OTP auth, refresh families, staff / vendor roles from DB, audit log, platform TOTP; 64 api tests |
| 4 tenant-setup-theme-engine | ✅ done | presets, tokens, dark mode, contrast guard, draft/preview/publish/rollback, logo upload, settings + onboarding, storefront theming; 77 api tests |
| 5 saas-billing-plans | ✅ done | plans + limits, trials, subscription/setup/GMV invoices, dunning → 423 suspension, mark-paid restore; 87 api tests |
| 6 vendor-onboarding-management | ✅ done | signup + invites, KYC to private bucket, state machine + review, commission overrides, encrypted payout method with re-auth + 72h hold; 102 api tests |
| 7 catalog-media-taxonomy | ✅ done | taxonomy + attributes, brands, products/variants, stock ledger, AVIF/WebP pipeline, moderation, CSV import, Q&A, storefront grid + PDP; 163 api tests |
| 8 page-builder | ✅ done | 20-section registry, layouts/menus/custom pages, storefront resolver (≤6 queries), CSS sandbox, vendor store builder, drag-and-drop builder UI + browser E2E; 176 api tests |
| 9 discovery-search-seo-i18n | ✅ done | FTS + Banglish/Bangla expansion + typo correction, facets, RLS-safe fast search (p95 < 200 ms @ 20k/100), wishlist, recently viewed, sitemap/robots/JSON-LD, en/bn web; 184 api tests |
| 10 cart-checkout-promotions-tax | ✅ done | grouped cart + quote, order tree with deterministic locks, coupons/campaigns/free shipping, VAT, COD rules, idempotent placement, expiry job, cart+checkout UI; 213 api tests |
| 11 payments-tenant-gateways | ✅ done | tenant-owned bKash/SSLCommerz accounts (encrypted, health-checked), verify-first settlement, idempotent webhooks by tenant public id, stock consumed on payment, COD receivables, reconciliation sweep, payment return page; 224 api tests |
| 12 fulfilment-courier | ✅ done | Pathao/Steadfast/RedX adapters, rule-based selection, one live parcel per sub-order, idempotent non-regressing events, ops queue, polling + SLA flagging, COD settlement matching, buyer tracking; 255 api tests |
| 13 returns-refunds-credit | ✅ done | return state machine with window + QC rules, reverse pickup, restock on QC pass only, refunds to original rail / store credit / manual finance queue, encrypted masked destinations, credit notes, store credit as tender at checkout; 295 api tests |
| 14 ledger-payouts-tax-docs | ✅ done | append-only balanced ledger with DB-enforced groups, capture/COD/refund/payout postings, vendor balances with reserve + holds, payout batches with maker-checker and bank/bKash exports, Mushak invoices + credit notes, nightly drift reconciliation; 318 api tests |
| 15 trust-safety-messaging | ✅ done | verified reviews with moderation and rollups, disputes that hold payouts and release on resolution, buyer↔vendor chat with Bangla-aware exfiltration filter and staff queue, fact-based vendor scorecards, twice-daily trust sweep, PDP reviews UI; 351 api tests |
