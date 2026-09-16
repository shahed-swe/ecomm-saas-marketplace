# ADR 0003 — Ledger: chart of accounts and entry model
Status: Accepted · 2026-09-16 · Supersedes all v1 ledger naming

## Decision
Per-tenant, append-only double-entry ledger.

`ledger_entries(id, tenant_id, group_id, account, vendor_id null, buyer_id null, direction debit|credit, amount numeric(14,2) check (amount > 0), entry_type, ref_type, ref_id, memo, created_at)`

- No UPDATE/DELETE: revoked from app role + trigger raising on attempt.
- Group balance enforced by a deferred constraint trigger: Σdebit = Σcredit per `group_id` at commit.
- `ledger_groups(id, tenant_id, entry_type, ref_type, ref_id, idempotency_key unique per tenant, created_at)`.

### Accounts
`gateway_clearing:{bkash|sslcommerz}`, `courier_cod_receivable:{pathao|steadfast|redx}`, `tenant_bank`, `vendor_payable`, `tenant_commission_revenue`, `gateway_fee_expense`, `courier_fee_expense`, `shipping_fee_revenue`, `promo_expense_tenant`, `buyer_refund_payable`, `store_credit_liability`, `vat_output_payable`, `tds_payable`, `reserve_hold`.

### entry_type
`capture, cod_delivery, cod_settlement, commission, gateway_fee, courier_fee, shipping_fee, discount, refund, return_reversal, reserve_hold, reserve_release, dispute_hold, dispute_release, payout, vat, tds, store_credit_issue, store_credit_redeem, adjustment`

## Worked example — prepaid bKash, 2 vendors (VAT exclusive 5% illustrative, commission 10%)
V1 items 1000, V2 items 500, shipping 60+60 (tenant-billed), VAT 75, bKash fee 1.5% of 1695 = 25.43.

| account | vendor | debit | credit |
|---|---|---|---|
| gateway_clearing:bkash | | 1669.57 | |
| gateway_fee_expense | | 25.43 | |
| vendor_payable | V1 | | 900.00 |
| vendor_payable | V2 | | 450.00 |
| tenant_commission_revenue | | | 150.00 |
| shipping_fee_revenue | | | 120.00 |
| vat_output_payable | | | 75.00 |
| **total** | | **1695.00** | **1695.00** |

Posted as one group per sub-order in code (shipping/VAT/fee allocated per sub-order); the table shows the sum.

## COD timing
No capture at checkout. `cod_delivery` group at courier-confirmed delivery (debit `courier_cod_receivable`, credits as above minus gateway fee). `cod_settlement` when courier statement matches (debit `tenant_bank` + `courier_fee_expense`, credit `courier_cod_receivable`).

## Reserve
One rule per tenant: `reserve_mode = window` (hold 100% of vendor_payable for N days after delivery) **or** `percent` (hold P% for N days). Never both.
