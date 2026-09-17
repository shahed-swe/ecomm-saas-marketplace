# Phase 2 — SaaS tenancy & domains (3 weeks) ✅ COMPLETE

**Mission:** the thing a SaaS marketplace cannot retrofit — two-level tenancy proven at the app **and** DB layer, host-based tenant resolution, subdomains + custom domains with a TLS gate.

**Demo:** tenant A's vendor A1 requests B1's storefront → 404; requests sibling A2's → 404; A's token on B's host → 401; the same query with the application filter removed returns zero rows from Postgres RLS.

## Gate-in
- [x] Phase 1 exit green (roles, baseline helpers `app_current_tenant/vendor`)

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 2.1 | Migration: `tenants`, `domains`, `vendors` (house vendor unique), `tenant_settings`, `vendor_storefronts`; RLS ENABLE+FORCE on all; composite FK `(tenant_id, vendor_id)` | `migrations/versions/0002_tenancy.py` | ✅ |
| 2.2 | SECURITY DEFINER `resolve_tenant_by_host()` and `tls_host_allowed()` — the only pre-tenant lookups | same | ✅ |
| 2.3 | Host → tenant resolver with Redis cache + invalidation; unknown/cancelled → 404 | `app/core/tenancy.py` | ✅ |
| 2.4 | Token claim contract (`kind, tid, vid, roles`); `tid` bound to host | `app/core/security.py`, `app/core/deps.py` | ✅ |
| 2.5 | Boundary deps: `current_tenant`, `tenant_db` (tx-local RLS vars), `current_vendor`, `require_tenant_staff`, `require_platform_admin` (404 to others) | `app/core/deps.py` | ✅ |
| 2.6 | `TenantScopedRepository` / `VendorScopedRepository` (cannot be built unscoped) | `app/core/repository.py` | ✅ |
| 2.7 | Tenant-prefixed cache/object/ISR keys | `app/core/cache_keys.py` | ✅ |
| 2.8 | Platform API: create tenant (subdomain + house vendor + settings), list, patch status/mode (multi→single guarded) | `app/modules/platform/*` | ✅ |
| 2.9 | Custom domains: add (generic conflict), DNS verify (TXT + CNAME/A), set primary, Caddy `ask`, periodic recheck job | `app/modules/domains/*`, `app/workers/settings.py` | ✅ |
| 2.10 | Vendor/admin sample surfaces used by the harness (storefront, vendors) | `app/modules/vendors/*` | ✅ |
| 2.11 | Web: server-side API fetch forwarding host; storefront shows tenant | `apps/web/src/lib/api.ts` | ✅ |

## Test plan (47 api tests pass)
- **ISO-T** cross-tenant 404 on every scoped route · **ISO-V** cross-vendor 404 · own resource 200 · token replay on other host 401
- **Coverage guard:** `test_every_scoped_route_is_covered` fails the build if a vendor/admin route with an id is not in the harness
- **RLS:** no context → 0 rows on all 5 tables; unfiltered SQL under A never returns B; vendor context hides sibling; cross-tenant INSERT/UPDATE rejected by WITH CHECK
- **Mutation check:** removing the vendor filter + vendor RLS variable makes ISO-V fail (harness verified to catch leaks)
- Platform: reserved/duplicate slug 409, platform surface 404 for tenant tokens, cancelled tenant disappears, single-mode house vendor, multi→single blocked
- Domains: pending not served, wrong TXT stays pending, verified → served + TLS allowed, generic conflict text, DNS moved → inactive + TLS denied

## Exit criteria
- [x] 2×2 isolation proven at app and DB layers
- [x] App DB role cannot bypass RLS; platform role used only in `app/modules/platform` and platform jobs
- [x] TLS `ask` returns 200 only for active domains of live tenants
- [x] Error messages never reveal another tenant (domain/slug conflicts generic)

## Notes for later phases
- Every new tenant table: `tenant_id NOT NULL`, RLS via `_tenant_rls`/`_vendor_rls` pattern, entry in `SCOPED_ROUTES`.
- Phase 3 replaces role strings in `require_tenant_staff` with permission bundles and adds real login.
