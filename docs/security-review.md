# Security review — pre-launch

Written against the deployed commit, by walking the system the way an attacker would rather than
the way it was built. Each finding says what was done about it; nothing here is a to-do list for
later.

## 1. Tenant isolation (the one that matters)

**Model.** Two enforcement layers, always both: every query carries `WHERE tenant_id = :t`, and
Postgres RLS stands behind it with `ENABLE` **and** `FORCE` on every tenant table. The request role
(`app`) is `NOBYPASSRLS` and not a superuser; the `platform` role, which does bypass, is used only
in `app/modules/platform`, the billing platform routes, the build queue and scheduled jobs.

**Audit.** `app/tests/test_security_audit.py` walks `pg_class` on every run and fails the build if
any table with a `tenant_id` lacks enforced RLS with a policy, and asserts the `app` role cannot
bypass. The 2×2 harness (`test_isolation.py`, 204 cases) proves cross-tenant and cross-vendor reads
return **404**, and that a token minted on one store's host is rejected (401) on another's.

**Finding (fixed).** `payment_accounts` — the table holding tenants' encrypted gateway credentials
— had RLS enabled but **not forced**, so the table's owner would read past the policy. Fixed in
migration `0020_hardening.py`; the audit test now prevents a repeat. Credentials are encrypted at
rest, so this was not an exposure of secrets, but it is precisely the gap the audit exists for.

## 2. Authentication and sessions

- Argon2id password hashing; refresh-token families with reuse detection (a replayed refresh
  revokes the family); access tokens are short-lived and carry `tid`, checked against the host on
  every request.
- Vendor membership and vendor status are re-read from the database on **every** vendor request, so
  removing a staff member or suspending a shop takes effect before the token expires.
- Staff permissions come from the database per request and are never trusted from the token.
- OTP attempts and rate limits survive a failed request, because failures return a problem response
  instead of raising and rolling back the counter.

**Audit.** A test walks the live OpenAPI schema and asserts that every route under
`/api/v1/admin`, `/api/v1/vendor` and `/platform/v1` refuses an anonymous caller.

## 3. Secrets

| Secret | Where it lives |
|---|---|
| Gateway and courier credentials | AES-GCM envelope, context-bound to `tenant:provider`, decrypted per request |
| Vendor payout accounts | encrypted; only last four ever leaves the API |
| Refund destinations | encrypted; masked (`••••1111`) in every staff response |
| Analytics API secrets | encrypted; the storefront endpoint returns public ids only |
| App signing keys, Play service accounts | **never in the database** — the build profile stores the *name* of a CI secret |

Audit log scrubs known secret keys; a test asserts no credential appears in the log or in any API
response for payments, couriers and analytics.

## 4. Money

Append-only ledger with balanced groups enforced by a deferred constraint trigger; `UPDATE` and
`DELETE` revoked from both roles. One posting per business event (unique index), so replays are
inert. Payments are verified with the provider before an order is paid, amounts must match to the
paisa, and one paid gateway payment per order is a partial unique index rather than a code check.
Payout lines are unique per `(tenant, vendor, period_end)` and post to the ledger only when marked
paid with a bank reference; maker-checker blocks self-approval where a tenant has two finance
users.

## 5. Input and output

- Every write is a Pydantic model with bounds; raw SQL is parameterised everywhere (`ruff` S608 is
  on, and the two places that build predicates dynamically bind arrays rather than interpolate).
- Custom CSS is parsed with `tinycss2`, scoped to `[data-tenant-css]`, and never loaded on checkout
  or payment pages.
- Uploads go straight to a private bucket through short-lived signed PUTs; the API never handles
  the bytes. Tax invoices, KYC documents, return photos and report exports are all private-bucket
  objects served by signed GETs that expire in minutes.
- Buyer↔vendor messages and support tickets pass a contact-exfiltration filter; messages are
  append-only.

## 6. Transport and browser surface

`SecurityHeadersMiddleware` sets, on every response: `nosniff`, `frame-ancestors 'none'`,
`X-Frame-Options: DENY`, `referrer-policy: no-referrer`, COOP/CORP, a restrictive
`permissions-policy`, `cache-control: no-store` (so signed URLs never sit in a proxy) and HSTS in
production. Caddy terminates TLS with on-demand certificates gated by `tls_host_allowed`, which
answers only for active domains — a stranger cannot mint a certificate for our edge.

## 7. Dependencies

`pip-audit` on the API's runtime dependencies: **no known vulnerabilities**. `pnpm audit --prod` on
the web app initially reported 37 advisories, all from `next@15.5.4` and its `postcss`/`sharp`
transitives; upgraded to `next@16.3.5` (one API change: `revalidateTag` now takes a cache profile)
and `postcss@8.5.28`, after which the audit is clean and the build and typecheck pass.

## 8. Accepted risks, written down

- **Gateway settlement is not reconciled against a bank feed.** Neither bKash nor SSLCommerz offers
  one here, so `gateway_clearing` accumulates and is reconciled against our own captures.
  Mitigation: the nightly drift report; a statement import is the fix when a feed exists.
- **A tenant's staff can read their own customers' data.** That is the product, not a flaw; it is
  bounded by permissions, audited, and exports are private-bucket objects with short-lived links.
- **Flutter apps are built in CI, not here.** Store signing therefore depends on CI secret
  hygiene; the build profile holds names only, and `analyze`/tests run before anything is signed.
- **No WAF in front of the API.** Rate limits are per tenant and per principal in Redis; a volumetric
  attack is an edge concern for the hosting provider.
