---
description: Implement, dry-run, or debug a vendor payout run — eligibility, holds, ledger entries, provider transfers, idempotency, and reconciliation.
argument-hint: [vendor id or period, or "implement"]
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

Payout work: **${ARGUMENTS:-implement the payout run}**

Use `payments-payouts-engineer`.

**If implementing**, build in this order:
1. `eligible_balance(vendor, as_of)` — sum of ledger entries excluding
   undelivered orders, orders inside the return window, disputed amounts, and the
   rolling reserve. Write this as a pure function with unit tests first.
2. `payout_runs` table, unique `(vendor_id, period_end)`.
3. The ARQ job: select eligible vendors above the minimum threshold, create the
   run row, create the provider transfer with the run id as the idempotency key,
   store the provider reference, then write the `payout` ledger entries — in that
   order, so a crash leaves a recorded transfer rather than a phantom one.
4. Failure path: retry with backoff, then flag for admin and notify the vendor.
5. Reconciliation job comparing ledger balance, provider balance, and the cached
   projection, alerting on any drift.

**Always produce a dry run first**: a table of vendor, gross, commission, fees,
refunds, held, net payable — and the sum, before a single transfer is created.

**If debugging**, check in this order: is the balance actually eligible (holds?
return window? reserve?), did the run row already exist (idempotency
short-circuit), did the provider reject (account not `payouts_enabled`?), is
there drift between ledger and projection.

Report the dry-run table, the idempotency key, and the ledger entries produced.
