# Payments

## A gateway is down

Symptoms: `POST /api/v1/payments/start` returning 502, or a provider's health check red in
`GET /api/v1/admin/payment-accounts`.

1. Confirm it is them, not us: run the health check for that tenant's account.
2. If it is the provider, tell the tenant to turn COD on (`PATCH /api/v1/admin/checkout-settings`)
   so orders keep landing, and disable the failing gateway rather than letting buyers meet an
   error: `POST /api/v1/admin/payment-accounts/disable`.
3. Orders already started stay `pending_payment`; `reconcile_payments` (every 15 minutes) settles
   or closes them once the provider answers again. Nothing needs to be done by hand.
4. Re-enable with `PUT /api/v1/admin/payment-accounts` when the health check is green.

## Money and the ledger disagree

`GET /api/v1/admin/ledger/reconciliation` reports drift per account. **Do not correct the ledger to
match the provider.** Find the missing event instead:

| Drift | Usually |
|---|---|
| `gateway_clearing:*` higher than captures | a refund posted at the provider we never saw — check `payment_events` |
| `courier_cod_receivable:*` higher than collected receivables | a settlement imported outside the app, or a statement not imported |
| `buyer_refund_payable` above the pending queue | a manual refund paid but never marked with its reference |
| `store_credit_liability` ≠ wallet balances | a credit granted directly in SQL (it should never be) |

Once the missing event is found, post it through the normal path (import the statement, complete
the refund, replay the webhook). The ledger then agrees because the fact reached it, not because
someone typed a number.

## A payment is paid twice

It cannot be: `uq_payment_paid_per_order` forbids a second paid gateway payment per order. If a
buyer says they were charged twice, they have two *orders* — check `payments` by `payer_ref` and
refund the duplicate order through the normal return flow.
