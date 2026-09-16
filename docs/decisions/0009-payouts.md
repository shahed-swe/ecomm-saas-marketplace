# ADR 0009 — Vendor payouts: bank + bKash, manual approval
Status: Accepted · 2026-09-16

## Decision
- Payout methods per vendor: `bank (account name, number, bank, branch, routing)` or `bkash (wallet number)`; stored encrypted; last-4 shown.
- Change of method: owner re-auth + OTP, 72 h payout hold, SMS + email to old and new contacts.
- Schedule per tenant (weekly default), minimum amount, rollover.
- Batch: `payout_batches(tenant_id, period_end, status draft|approved|exported|completed)`, `payout_lines(batch_id, vendor_id, gross, tds, net, method, status pending|paid|failed, reference)`; unique `(tenant_id, vendor_id, period_end)`.
- Maker-checker when tenant has ≥ 2 finance users.
- Export: bank CSV via per-bank templates (BEFTN/NPSB), bKash disbursement CSV.
- Mark paid with reference → `payout` ledger group (debit `vendor_payable`, credit `tenant_bank`; TDS to `tds_payable`). Re-marking is a no-op.
