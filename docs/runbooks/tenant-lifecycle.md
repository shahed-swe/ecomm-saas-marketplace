# Tenant lifecycle

## Suspension (non-payment)

Dunning suspends automatically. A suspended tenant is **read-only**: storefront works, writes get
423, sign-in and billing still work so they can pay. Never delete data to make a point.

## Restoring

Mark the invoice paid (`POST /platform/v1/billing/invoices/{id}/mark-paid`); the tenant returns to
`active` and writes resume. Check their queued jobs afterwards — media and imports that failed
during suspension need re-running.

## Export (a tenant leaving)

They own their data. Provide, from their own dashboard: products CSV, orders CSV, ledger CSV
(`/api/v1/admin/reports/exports`), media as a bucket prefix copy, and their theme document JSON.
Their apps publish under their own store accounts, so those simply stay theirs.

## Purge

Only after the retention period agreed in the contract, and only with a written request. Purge
order: media objects → search rows → tenant tables (RLS-scoped, in dependency order) → domains →
the tenant row. The platform's own billing records are kept: they are our books, not theirs.
