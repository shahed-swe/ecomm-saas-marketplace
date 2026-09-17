# Phase 20 — Hardening & launch (3 weeks) ✅ COMPLETE

**Mission:** stop building and start proving. Audit the isolation model automatically rather than by memory, upgrade what the dependency scanners flag, write the runbooks the on-call person will actually open, and define go/no-go in one sentence a non-engineer can hold us to.

**Demo:** the test suite itself now walks `pg_class` and fails the build if any tenant table ships without enforced RLS — and it found one; every staff, vendor and platform route is probed anonymously on every run; the restore drill asserts isolation and a balanced ledger on the restored copy; `pip-audit` and `pnpm audit` are clean after the web upgrade.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 20.1 | Security headers on every response (CSP `frame-ancestors 'none'`, nosniff, DENY, no-referrer, COOP/CORP, permissions-policy, `no-store`, HSTS in production) | `core/middleware.SecurityHeadersMiddleware` | ✅ |
| 20.2 | **Automated isolation audit**: every table with `tenant_id` must have RLS `ENABLE` + `FORCE` + a policy, and the app role must be `NOBYPASSRLS` | `tests/test_security_audit.py` | ✅ |
| 20.3 | **Finding fixed**: `payment_accounts` (tenants' encrypted gateway credentials) had RLS enabled but not forced | `migrations/versions/0020_hardening.py` | ✅ |
| 20.4 | Anonymous probe of every scoped route in the live OpenAPI schema | same | ✅ |
| 20.5 | Append-only proofs for all eight ledger-like tables, against the **platform** role | same | ✅ |
| 20.6 | Error, health and unknown-host checks: no stack traces, no tenant ids, no infrastructure names | same | ✅ |
| 20.7 | Dependency audit: API clean; web upgraded `next@15.5.4 → 16.3.5` and `postcss` (37 advisories → 0), `revalidateTag` migrated to the new signature | `apps/web/package.json`, `api/revalidate/route.ts` | ✅ |
| 20.8 | Multi-tenant load profile: three tenants, one running a flash sale, with **per-tenant** thresholds so a noisy neighbour is visible | `infra/load/k6-marketplace.js` | ✅ |
| 20.9 | Restore drill extended to assert enforced RLS and a balanced ledger on the restored copy, and to record the real RTO | `infra/scripts/restore-drill.sh` | ✅ |
| 20.10 | Runbooks: incident, restore, payments, couriers, payouts, tenant lifecycle, launch | `docs/runbooks/*` | ✅ |
| 20.11 | Security review with findings, mitigations and **accepted risks written down** | `docs/security-review.md` | ✅ |
| 20.12 | Go/no-go checklist and first-week watchlist | `docs/runbooks/launch.md` | ✅ |

## Tests (412 api tests pass; the perf test passes on its own marker)
Every tenant table has RLS enabled **and** forced with a policy — this test is what found the
`payment_accounts` gap · the `app` role is neither superuser nor `BYPASSRLS` · all eight append-only
tables refuse `UPDATE` and `DELETE` even as the platform role · no route under `/api/v1/admin`,
`/api/v1/vendor` or `/platform/v1` answers without a token · security headers are present on a
plain storefront response, including `cache-control: no-store` so signed URLs never sit in a proxy ·
health and metrics leak no tenant id or infrastructure name · an unknown host is simply not a store ·
errors carry no stack trace or SQL.

## Notes
- **The audit is a test, not a document.** A written isolation review goes stale the day someone
  adds a table; a test that walks `pg_class` cannot. That is why the finding in 20.3 surfaced at
  all — nobody remembered writing that one migration differently.
- The load profile deliberately leaves checkout out: hammering it would create real orders,
  reservations and ledger entries. Checkout has its own perf test that builds and cleans up its own
  tenant.
- `next@16` was a security upgrade, not a feature one; the only code change it required is noted in
  the review.
- Accepted risks are listed with their reasons in `docs/security-review.md`. A risk nobody wrote
  down is a risk everybody assumes someone else accepted.
