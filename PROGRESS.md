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
