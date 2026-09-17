# Payouts

## A batch will not approve

`maker_checker` refuses approval by the person who prepared the batch when the tenant has more than
one finance user. That is the control working. Have the second finance user approve it, or turn the
control off deliberately in settings — and write down who decided to.

## A line failed at the bank

1. Mark it: `POST /api/v1/admin/payout-lines/{id}/failed` with the bank's reason.
2. Fix the cause (usually a wrong account, which means the vendor must re-enter their payout method
   — that triggers the 72-hour hold on purpose).
3. The vendor's payable is unchanged: the ledger posts only when a line is **marked paid**, so a
   failed transfer leaves the money owed.

## A vendor says they were paid twice

Check `payout_lines` for that vendor and period: `(tenant_id, vendor_id, period_end)` is unique, so
two lines for one period cannot exist. Two *payments* for one line mean the bank file was uploaded
twice at the bank — reconcile with the bank reference on the line and recover through the bank, not
by adjusting the ledger.

## Payouts are stuck at zero

Most often the reserve, not a bug: `GET /api/v1/admin/vendor-balances` shows payable, reserve and
hold per vendor. A tenant using a return-window reserve holds back everything delivered inside the
window; a hold (dispute, payout-method change) blocks the whole payout by design.
