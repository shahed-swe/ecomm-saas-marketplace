---
description: Execute and audit delivery dispatch operations — rider assignment, live tracking, proof-of-delivery verification, COD cash collection, remittance, and rider cash ledger reconciliation.
argument-hint: [assign <sub_order_id> | reconcile <rider_id> | audit | zones]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Operate on the delivery subsystem: **$ARGUMENTS**

Use the `dispatch-delivery-engineer` agent with `payments-payouts-engineer` for
COD reconciliation. Skill: `delivery-dispatch-rider`.

Interpret the argument:

- **`assign <sub_order_id>`** — run the dispatch algorithm for a specific
  sub-order: zone match, eligible riders (available + below COD ceiling + in
  zone), score by (zone proximity, current load, rating), assign the best. Report
  the assignment, rider load after, and whether COD is enabled for this zone.
- **`reconcile <rider_id>`** — reconcile a rider's cash: compare
  `rider_cash_ledger` collects vs remits vs the platform `ledger_entries` for
  each COD delivery. Report any drift, unremitted cash, and whether the rider is
  over their COD ceiling.
- **`audit`** — scan all active deliveries: verify every `delivered` row has
  `proof_verified=true`, check for stuck assignments (assigned but not accepted
  beyond SLA), stale `in_transit` deliveries, and riders over their cash ceiling.
  Report as a table.
- **`zones`** — list all `delivery_zones` with their active rider count,
  in-flight delivery count, COD enablement, and SLA compliance rate.

Rules:
- `delivered` requires valid PoD — never set it without proof verification.
- COD cash reconciliation writes balanced ledger groups — verify sum-to-zero on
  every remittance entry.
- A rider over their COD ceiling is not assigned more COD deliveries.
- Private bucket assets (PoD photos/signatures) use short-lived signed URLs.
