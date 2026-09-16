# ADR 0001 — Two-level tenancy (tenant + vendor) with RLS
Status: Accepted · 2026-09-16

## Context
The product is a SaaS hosting many marketplaces. Inside each, vendors must not see each other. A leak across tenants is existential; across vendors, a P0.

## Options
A. Database per tenant — strongest isolation, migration and ops cost × tenants.
B. Schema per tenant — same migration multiplication.
C. **Shared schema, `tenant_id` on every tenant row, Postgres RLS, scoped repositories.**

## Decision
C. Two session variables per request/job: `app.tenant_id` (always) and `app.vendor_id` (vendor context only).
- Tenant resolved from Host header via `domains`; JWT claim `tid` must equal it.
- Application DB role has no BYPASSRLS. A separate `platform` role is used only by `app/platform/` jobs.
- Unique constraints lead with `tenant_id`.
- Redis keys `t:{tid}:…`, object keys `t/{tid}/…`, ISR tags `t:{tid}:…`.

## Schema impact
- `tenants(id uuid v7, slug citext unique, name, status, store_mode, plan_id, created_at)`
- `domains(id, tenant_id, host citext unique, kind subdomain|custom, status pending|verified|active|inactive, is_primary, verification_token)`
- Every tenant table: `tenant_id uuid not null references tenants`, index leading with `tenant_id`.
- RLS policy template:
  `USING (tenant_id = current_setting('app.tenant_id')::uuid)`; vendor tables add
  `AND (nullif(current_setting('app.vendor_id', true),'') IS NULL OR vendor_id = current_setting('app.vendor_id')::uuid)`.

## Consequences
Isolation tests are a 2×2 grid (ISO-T, ISO-V) and mandatory for every scoped endpoint. A large tenant can later be moved to a dedicated database without code change (same schema).
