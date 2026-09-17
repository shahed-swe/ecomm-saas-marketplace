# Phase 14 — Ledger, payouts & tax documents (4 weeks) ✅ COMPLETE

**Mission:** make the money answerable. One chart of accounts per tenant, every taka in balanced append-only groups, vendor balances that can be defended line by line, payouts that cannot pay twice, and tax documents the NBR would recognise.

**Demo:** a paid order books capture → commission → VAT → delivery fee; a COD order books the courier's cash and then the bank when the statement lands; a refund unwinds payable, commission and VAT; finance builds a weekly batch, a *second* person approves it, exports a bKash disbursement file, marks a line paid with its reference — and the vendor's balance drops by exactly that amount. The trial balance is still balanced at every step.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 14.1 | Migration: `ledger_entries` (append-only), **deferred constraint trigger that refuses an unbalanced group at COMMIT**, one-posting-per-event unique index, `payout_batches` + `payout_lines`, `tax_invoices`, finance settings (schedule, minimum, reserve rule, TDS, BIN, maker-checker) | `migrations/versions/0014_ledger.py` | ✅ |
| 14.2 | The chart of accounts as code — the only account names the ledger accepts | `ledger/accounts.py` | ✅ |
| 14.3 | `post()`: balanced groups, idempotent per business event, never edited | `ledger/service.py` | ✅ |
| 14.4 | Capture posting per shipment, split across the tenders the buyer actually used (gateway clearing, store-credit liability), with commission and VAT from the **sub-order snapshot** | `service.post_capture`, `split_sub_order` | ✅ |
| 14.5 | COD: `cod_delivery` when the courier takes the cash, `cod_settlement` when it reaches the bank with the courier fee as an expense | `post_cod_delivery`, `post_cod_settlement` | ✅ |
| 14.6 | Refunds: `return_reversal` unwinds payable, commission and VAT; manual refunds clear `buyer_refund_payable` when finance actually pays | `post_refund`, `post_manual_refund_paid` | ✅ |
| 14.7 | Balances: vendor payable, one reserve rule per tenant (percent **or** window, never both), payout holds, trial balance | `vendor_statement`, `vendor_reserve`, `trial_balance` | ✅ |
| 14.8 | Payout batches: idempotent per `(vendor, period_end)`, skips holds / missing methods / below-minimum with reasons, TDS withheld | `ledger/payouts.py` | ✅ |
| 14.9 | Maker-checker approval, BEFTN/NPSB bank CSV and bKash disbursement CSV (last four only, never a full account number), mark-paid posts the payout and completes the batch | `payouts.approve/export/mark_paid` | ✅ |
| 14.10 | Mushak-6.3-style tax invoice and 6.8 credit note as PDFs in the private bucket, numbered gaplessly per tenant, issued once | `ledger/documents.py` | ✅ |
| 14.11 | Nightly reconciliation: ledger vs payments, couriers, refund queue and wallets — **reports drift, never corrects it**, and enumerates from both sides so nothing can hide | `ledger/reconcile.py`, worker cron | ✅ |
| 14.12 | Scheduled payout runs per tenant schedule (drafts only — preparing is not paying) | `workers/settings.payout_runs` | ✅ |
| 14.13 | APIs: staff books, trial balance, reconciliation, vendor balances, batches and lines, tax documents; vendor balance/ledger/payouts; buyer tax invoices | `ledger/router.py` | ✅ |
| 14.14 | Web: tax invoice links on the buyer's order page | `apps/web/src/app/(shop)/orders/[number]` | ✅ |

## Tests (318 api tests pass)
A prepaid capture books the gateway's debt, the vendor's payable net of commission, the VAT and the delivery fee, balances, and **cannot be posted twice** · the database itself refuses a half-entry (the constraint trigger fires at COMMIT even when the service is bypassed) · a commission rate tripled after the sale does not touch the old order — the snapshot decides · COD books `courier_cod_receivable` at delivery, then the statement moves it to the bank with the fee as an expense, and the trial balance still balances · a refund debits payable, commission and VAT and credits the rail it came from; the buyer gets back what they paid, VAT included, and the vendor keeps none of it · a payout batch pays each vendor once (a second batch for the period is refused, nothing is payable before approval, marking paid twice is a no-op), the export carries `••••5678` and never the full wallet number, and the vendor's payable drops to zero · reserves, holds and minimums keep money back with a stated reason per vendor · maker-checker blocks the preparer from approving their own batch and lets a second finance user through · tax invoices are numbered gaplessly, generated once, served by short-lived signed URLs from the private bucket, and invisible to another buyer · a credit note documents a refund with its VAT · a vendor sees only its own entries and balance · reconciliation reports drift and changes nothing.

## Notes
- **The ledger cannot be edited, by anyone.** `UPDATE`/`DELETE` are revoked from both the app and platform roles and a trigger blocks them anyway; corrections are reversing groups. A test proves even the platform role cannot rewrite history.
- Gateway settlement (money moving from the tenant's bKash/SSLCommerz account to their bank) has no API feed from either provider, so `gateway_clearing` accumulates until a statement import is added; reconciliation compares it to captures so the number is always explainable.
- Payout export deliberately stops at the last four digits: the bank portal already holds the beneficiary account, and a CSV of full account numbers is a breach waiting to happen.
- TDS is withheld at the tenant's rate and posted to `tds_payable`; filing it is a finance task, not an automated transfer.
