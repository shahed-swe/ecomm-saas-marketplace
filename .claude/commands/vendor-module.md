---
description: Scaffold a vendor-scoped module with tenancy enforced at the repository layer, RLS policy, and a cross-tenant isolation test.
argument-hint: <domain name — what vendors manage>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Build the vendor-scoped domain: **$ARGUMENTS**

Use `vendor-platform-engineer` with `fastapi-architect` and `data-modeler`.

Required, in order:

1. **Model** — `vendor_id UUID NOT NULL` FK, composite index
   `(vendor_id, created_at DESC)`, and any unique constraint scoped to the
   vendor rather than global.
2. **Migration** — table plus the RLS policy:
   ```sql
   ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
   CREATE POLICY vendor_isolation ON <t>
     USING (vendor_id = current_setting('app.vendor_id')::uuid);
   ```
3. **Repository** — extends `VendorScopedRepository`; every query built from
   `self._base(Model)`. No method may accept an override that disables scoping.
4. **Service** — vendor state gate (`approved` only for writes), plus any
   admin-configured bounds on vendor-supplied values.
5. **Router** — `/vendor/<plural>` with `Depends(current_vendor)`. If the
   platform admin also needs this data, that is a *separate* router under
   `/admin/` with `require_platform_admin` — never a flag on this one.
6. **Tests** — the four standard cases plus the mandatory isolation test:
   vendor A requests vendor B's id → 404.

Report: the scoping mechanism, the RLS policy, the isolation test, and any query
in the module that is deliberately platform-wide and why.
