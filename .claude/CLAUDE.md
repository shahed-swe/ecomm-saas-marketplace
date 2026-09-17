# CLAUDE.md — Multi-tenant SaaS marketplace (v2)

Project constitution. Read before touching any file. When this file conflicts with a habit, this file wins.

> **Governing documents, in order:** this file → `plan/00-architecture.md` → `docs/decisions/` (ADRs 0001–0014) → `plan/README.md` + `plan/phase-NN-*/`.
> Progress lives in `PROGRESS.md`. Work the lowest unfinished phase; its README is the checklist.

## 1. What this is
A SaaS platform hosting many branded marketplaces (tenants) in Bangladesh. Each tenant runs single- or multi-vendor (house vendor pattern), on a subdomain or custom domain, with its own theme, bKash/SSLCommerz accounts, Pathao/Steadfast/RedX couriers, and white-label buyer + vendor Flutter apps. **No riders, no Stripe, no Nagad** (ADR 0006/0007).

Boundaries (each has its own dependency; never a flag):

| Actor | Dependency | Prefix |
|---|---|---|
| Super-admin | `require_platform_admin` (404 to everyone else) | `/platform/v1` |
| Tenant staff | `require_tenant_staff(permission)` — permissions loaded from DB per request | `/api/v1/admin` |
| Vendor staff | `current_vendor` / `require_vendor_role(perm)` — membership + vendor state re-checked per request | `/api/v1/vendor` |
| Buyer | `current_principal` (kind=buyer) | `/api/v1` |

## 2. Stack (fixed)
FastAPI + SQLAlchemy 2 async + Alembic · PostgreSQL 16 with RLS · Redis 7 · ARQ · Next.js App Router + Tailwind (CSS-variable tokens) · Flutter (Riverpod, Dio from OpenAPI) · MinIO/S3 · Vault · Caddy on-demand TLS · local BD data centre.

## 3. Tenancy rules (P0 if broken)
1. Tenant resolved from **Host** (`resolve_tenant_by_host`, Redis-cached). JWT `tid` must equal host tenant, else 401.
2. Every tenant table: `tenant_id NOT NULL`, RLS ENABLE + FORCE, policy on `app_current_tenant()`; vendor tables add `app_current_vendor()`. Unique constraints lead with `tenant_id`.
3. Filters live in `TenantScopedRepository` / `VendorScopedRepository` **and** in raw SQL (`WHERE tenant_id = :t`). RLS is the second layer, never the only one — the platform role bypasses RLS.
4. `tenant_db` sets RLS variables transaction-locally. The app DB role is NOBYPASSRLS; the platform role is used only in `app/modules/platform`, `billing` platform routes and platform jobs.
5. Every scoped route with a path id is registered in `app/tests/test_isolation.py::SCOPED_ROUTES` (ISO-T, ISO-V, replay). The coverage guard fails the build otherwise.
6. Cache keys `tkey(tid, ...)`, object keys `object_key(tid, ...)`; jobs use `@tenant_job`.
7. Errors never reveal another tenant/vendor (generic "unavailable"); cross-scope reads return **404**.

## 4. Money rules
Ledger per tenant, append-only, balanced groups (ADR 0003 chart of accounts — use those names only). Commission + VAT snapshotted at capture / COD delivery. Payouts idempotent on `(tenant_id, vendor_id, period_end)`, manual approval. Server verifies every payment; callbacks idempotent. Platform billing is a separate book (ADR 0011).

## 5. Theme rules
Presentation is data (ADR 0012): Pydantic `ThemeDocument` is the source; publish = immutable `theme_versions` row + pointer flip; contrast guard blocks publish; no raw hex in components; brand assets only from our upload endpoint; custom CSS (Phase 8) is sanitised and never applied to checkout/payment pages.

## 6. Coding rules
- Vertical slices: `app/modules/<domain>/{models,schemas,service,router}.py`.
- Return (don't raise) `problem_response` when a failed request must still commit (OTP attempts, refresh reuse revocation).
- Every staff/vendor mutation writes `audit.record(...)` in the same transaction.
- Plan limits via `billing.service.require_feature/require_quota` (402 `plan_limit`); suspended tenants are read-only (423).
- Money: `Decimal`, `numeric(12,2)`, half-up to 0.01.
- Heavy CPU (images) in threadpool/worker, never on the event loop.

## 7. Definition of done
- [ ] Reversible migration with RLS + grants for new tables
- [ ] Repository/SQL tenant filter + ISO tests registered
- [ ] Audit entries for mutations; plan limits where applicable
- [ ] `uv run ruff check . && uv run pytest -q` green; OpenAPI + TS contract regenerated (`make contract`)
- [ ] Phase README + `PROGRESS.md` updated

## 8. Commands
`make api-test`, `make web-test`, `make contract`. Local services for tests: Postgres 16 on 127.0.0.1:5432 (trust), Redis on 6379.
