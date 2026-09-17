# Incident response

## First five minutes

1. **Say it out loud.** Post in `#incidents`: what is broken, which tenants, since when. An
   incident nobody announced is an incident one person is quietly fighting alone.
2. **Check the blast radius.** `GET /platform/v1/reports/overview` shows orders per tenant in the
   last 7 days; a tenant at zero today that was not yesterday is affected.
3. **Decide: degrade or stop.** Taking the checkout down deliberately is better than taking money
   for orders that will not ship. `PUT /api/v1/admin/app-config {"maintenance": true}` speaks to
   the apps; Caddy can serve a static page for the web.

## Triage by symptom

| Symptom | First look | Likely |
|---|---|---|
| Every tenant is down | Caddy, then the API health endpoint, then Postgres connections | infra, not code |
| One tenant is down | `resolve_tenant_by_host` cache, that tenant's `status`, its domain's `status` | suspension, expired domain, DNS |
| Checkout fails, browsing works | gateway health (`/api/v1/admin/payment-accounts`), reservation expiry job | provider outage |
| Orders stuck in `pending_payment` | `reconcile_payments` worker, provider webhooks | lost callbacks |
| Parcels not updating | `courier_sweep` worker, courier webhook deliveries | courier outage (see courier.md) |
| Ledger drift alert | `GET /api/v1/admin/ledger/reconciliation` | see payments.md |

## Rules while fixing

* **Never edit the ledger.** Corrections are reversing groups. A "quick fix" in the books is how a
  marketplace loses the ability to say what it owes.
* **Never disable RLS to debug.** Use the platform role in a read-only session instead.
* Keep the tenant informed through their own staff surface, not only in our Slack.

## Afterwards

Write the timeline the same day while it is still boring: what happened, what we saw, what we did,
what we will change. Add a test for the thing that broke — the audit suite in
`app/tests/test_security_audit.py` and the isolation harness exist because of exactly this habit.
