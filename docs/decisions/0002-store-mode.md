# ADR 0002 — Store mode via house vendor
Status: Accepted · 2026-09-16

## Decision
`tenants.store_mode ∈ {single, multi}`. On tenant creation a **house vendor** is created (`vendors.is_house = true`, exactly one per tenant, partial unique index). In single mode all products belong to the house vendor, vendor signup is disabled, vendor UI hidden, commission = 0, payouts disabled for the house vendor. Orders, sub-orders and ledger behave identically.

Switching single → multi: enable vendor signup; house vendor remains and keeps selling. Multi → single: only allowed when no non-house vendor has open orders or payable balance.

## Rejected
A `multi_vendor` boolean branching queries — the forgotten branch is a leak.

## Schema impact
`vendors.is_house boolean not null default false`, `create unique index on vendors(tenant_id) where is_house`.
