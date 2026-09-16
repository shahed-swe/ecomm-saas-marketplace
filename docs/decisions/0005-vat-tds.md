# ADR 0005 — VAT and TDS
Status: Accepted · 2026-09-16

## Decision
Tax is **configuration, not code**. Rates are entered and confirmed by each tenant's accountant; the platform ships no statutory defaults beyond editable placeholders.

- `tax_settings(tenant_id, vat_registered, bin, pricing inclusive|exclusive, default_vat_rate)`; `tax_rates(tenant_id, scope category|product, scope_id, rate)`.
- VAT computed server-side in the quote, per line, snapshotted on `order_items.vat_rate/vat_amount`.
- Tax invoice (Mushak-6.3-style layout) PDF per sub-order; seller = vendor (house vendor → tenant). Sequential number per tenant per fiscal year: `invoice_sequences(tenant_id, fiscal_year, next)` with row lock.
- Credit note per refund, referencing the invoice.
- TDS/AIT on vendor payouts: `tds_rules(tenant_id, vendor_type, rate)`, withheld in payout batch, posted to `tds_payable`; certificate CSV/PDF export per vendor per period.

## Consequences
The plan never asserts legal correctness of rates or invoice fields; a tenant-facing checklist asks the accountant to confirm before enabling invoices.
