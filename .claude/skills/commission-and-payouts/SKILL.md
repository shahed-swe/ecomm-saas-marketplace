---
name: commission-and-payouts
description: Implement marketplace money movement — commission rate resolution, the append-only double-entry ledger, vendor balances and holds, scheduled payout runs with Stripe Connect or PayPal, negative balances, reserves, chargebacks, and reconciliation. Use this skill for anything involving commission, payout, settlement, vendor balance, ledger, reserve, or "how much do we owe this seller".
---

# Commission, Ledger & Payouts

## Commission resolution

Resolution order, first match wins:
```
vendor_category_rates → vendor_rates → category_rates → platform_default
```
Optional shapes: flat percentage, percentage + fixed listing fee, tiered by
monthly GMV, capped maximum fee. Resolve **once, at order capture**, and store
on the sub-order: `commission_rate`, `commission_amount`, `rate_source`. A rate
change tomorrow must never alter yesterday's settled order, and a vendor asking
"why 12%?" must be answerable from a single row.

## The ledger

Append-only, double-entry, immutable. No UPDATE. No DELETE. Corrections are
reversing entries.

```
ledger_entries(
  id, entry_group_id, vendor_id, account, direction, amount, currency,
  entry_type, reference_type, reference_id, description, created_at
)
```
Accounts: `platform_clearing`, `vendor_payable`, `platform_revenue`,
`payment_fees`, `reserve`.

Worked example — a 1,000 sale, 10% commission, 29 gateway fee absorbed by the
platform:

| entry_type | account | dir | amount |
|---|---|---|---|
| sale | platform_clearing | debit | 1000 |
| sale | vendor_payable | credit | 1000 |
| commission | vendor_payable | debit | 100 |
| commission | platform_revenue | credit | 100 |
| payment_fee | platform_revenue | debit | 29 |
| payment_fee | platform_clearing | credit | 29 |

Vendor payable = 900. Platform revenue = 71. Every `entry_group_id` sums to zero
— assert this in tests, and in a nightly job.

## Balance and eligibility

```
available = SUM(vendor_payable entries)
          − pending_orders (not yet delivered)
          − return_window_holds
          − disputed_amounts
          − rolling_reserve(vendor)
```
New vendors carry a reserve (e.g. 10% for 90 days) and a longer hold. Established
vendors with clean dispute rates graduate. Publish the rules; an unexplained hold
is the fastest way to lose good sellers.

## Payout run

```python
# idempotent on (vendor_id, period_end)
run = await get_or_create_payout_run(vendor_id, period_end)
if run.status == "completed":
    return run                       # replay-safe
transfer = await stripe.Transfer.create(
    amount=run.amount, currency=run.currency,
    destination=vendor.connect_account_id,
    idempotency_key=f"payout:{run.id}",
)
run.provider_ref = transfer.id
await write_ledger_payout(run)       # only after the reference is stored
```
Order matters: create the run row → create the transfer → store the reference →
write the ledger. A crash anywhere leaves a recoverable state, never a double
payment.

Threshold, schedule (daily/weekly/on-demand), and currency are per vendor.
Below-threshold balances carry forward.

## Negative balances
Refunds after payout push the vendor negative. Net it against future sales, show
it plainly in their dashboard with the causing orders listed, and escalate to
admin past a configurable age and amount. Never hide it inside an aggregate.

## Chargebacks
Record the dispute, hold the amount, apply the configured chargeback fee as its
own ledger entry, and reverse commission only when the dispute is lost. Keep the
evidence bundle (order, shipping proof, messages) linked to the dispute record.

## Reconciliation
Nightly: ledger balance vs provider balance vs cached projection, per vendor.
Alert on any non-zero drift with the entry ids that differ. Never repair drift by
overwriting the projection — the projection is the suspect, and if it isn't, you
have a real accounting bug worth finding.

## Test set
Group sums to zero · commission snapshot survives a rate change · payout run
twice → one transfer · refund after payout → negative balance netted correctly ·
multi-currency kept in separate ledgers and never summed across currencies.
