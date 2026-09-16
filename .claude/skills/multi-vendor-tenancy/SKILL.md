---
name: multi-vendor-tenancy
description: Enforce multi-tenant data isolation in this marketplace — vendor_id scoping, scoped repositories, Postgres Row Level Security, the current_vendor dependency, vendor staff roles, admin-vs-vendor route separation, and cross-tenant isolation tests. Use this skill for ANY query, endpoint, dashboard, report, or migration that touches vendor-owned data, even when the request sounds routine like "show their orders" or "add a column".
---

# Multi-Tenant Isolation

Shared database, shared schema, `vendor_id` discriminator. Cheapest to operate,
easiest to leak. Defence is structural, not disciplinary.

## Layer 1 — the scoped repository

```python
class VendorScopedRepository:
    model: ClassVar[type[Base]]

    def __init__(self, session: AsyncSession, vendor_id: UUID) -> None:
        self.session = session
        self.vendor_id = vendor_id

    def _base(self) -> Select:
        return select(self.model).where(self.model.vendor_id == self.vendor_id)

    async def get(self, id: UUID):
        return await self.session.scalar(self._base().where(self.model.id == id))
```

There is no constructor without `vendor_id` and no method that skips `_base()`.
Platform-wide access lives in a *differently named* class
(`PlatformProductRepository`) used only by admin routers — so "unscoped" is
always visible at the import site.

## Layer 2 — Row Level Security

```sql
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE products FORCE ROW LEVEL SECURITY;
CREATE POLICY vendor_isolation ON products
  USING (vendor_id = current_setting('app.vendor_id', true)::uuid);
```
Set per request inside the session dependency:
```python
await session.execute(text("SET LOCAL app.vendor_id = :v"), {"v": str(vendor_id)})
```
`SET LOCAL` is transaction-scoped, which is what you want with a connection pool
— a leaked session variable across pooled connections would be worse than no RLS.
Give the admin path a separate DB role that bypasses the policy, rather than
unsetting the variable.

## Layer 3 — the dependency

```python
async def current_vendor(user = Depends(current_user), session = Depends(get_session)) -> Vendor:
    membership = await get_membership(session, user.id)
    if not membership:                       raise Forbidden("not_a_vendor")
    if membership.vendor.status != "approved": raise Forbidden("vendor_not_active")
    return membership.vendor
```
Vendor staff roles (`owner|manager|staff`) are a second dependency
(`require_vendor_role("owner")`), not an `if` in the handler.

## 404, not 403

Returning 403 for another vendor's resource confirms that the id exists. Enumerate
a few thousand ids and you have a competitor's order volume. Vendor-scoped
lookups return 404 for both "missing" and "not yours".

## Where leaks actually happen

Checked queries are rarely the problem. These are:
1. **Aggregates and analytics** — a "top vendors" chart, a percentile, a
   platform-average shown to a vendor with a small cohort.
2. **Search and listing endpoints** written for buyers, then reused in the vendor
   dashboard without scoping.
3. **Exports** — CSV endpoints built quickly and scoped loosely.
4. **Error messages** — "SKU already exists" tells vendor A what vendor B sells.
   Scope unique constraints to the vendor.
5. **Webhooks and emails** — a template rendering an order that spans vendors,
   sent to one of them.
6. **Media** — a predictable storage key, or a KYC document served from the CDN.
7. **Joins** — the scoped table is filtered, the joined one isn't.

## The mandatory test

```python
async def test_vendor_cannot_read_other_vendor_product(client, vendor_a, vendor_b):
    p = await create_product(vendor=vendor_b)
    r = await client.get(f"/vendor/products/{p.id}", headers=auth(vendor_a))
    assert r.status_code == 404
```
Parametrise it over every vendor-scoped route. Add the route to the parametrised
list in the same commit that creates the route; make a missing entry a CI failure
by asserting the tested-route set matches the registered vendor router paths.

## When to move to schema-per-tenant
Only if a single enterprise seller demands physical isolation for compliance.
It multiplies migration cost by the tenant count. Do not pre-emptively adopt it.
