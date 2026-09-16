# ADR 0008 — Returns, refunds, store credit
Status: Accepted · 2026-09-16

## Flow
`requested → approved | rejected → pickup_booked → picked_up → received → qc_passed | qc_failed → refunded | returned_to_buyer`
- Buyer requests within tenant/category return window with reason + photos (private bucket).
- Approver: vendor, with tenant override; auto-approve rules per reason optional.
- Reverse pickup booked through the courier adapter; return shipping payer set by reason (vendor fault → vendor; change of mind → buyer, deducted from refund).
- QC by vendor within SLA; dispute escalates to tenant.
- Restock on QC pass writes an inventory movement; write-off otherwise.

## Refund rails
| Original method | Refund to |
|---|---|
| bKash | bKash refund API |
| SSLCommerz | SSLCommerz refund API |
| COD | buyer's choice: bKash number or bank (manual refund task for finance) **or** store credit |
Any method may be refunded as store credit if the buyer chooses.

## Ledger
`return_reversal` group reverses vendor_payable, commission, VAT for returned lines; `refund` group debits `buyer_refund_payable` → credit gateway/bank; `store_credit_issue` credits `store_credit_liability`. Credit note issued.
