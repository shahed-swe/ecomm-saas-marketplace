# Phase 13 — Returns, refunds & store credit (2 weeks) ✅ COMPLETE

**Mission:** give money back correctly — once, down the rail it came up, only after somebody has actually looked at what came back, and with a credit note so the VAT unwinds too.

**Demo:** a delivered parcel → the buyer picks items, a reason and where the refund should go → the vendor approves, books a reverse pickup with the same courier, receives and inspects → a QC pass restocks the goods and refunds the bKash capture, a QC fail refunds nothing → a COD buyer takes store credit instead and spends it on the next order, with the gateway only asked for the remainder.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 13.1 | Migration: `return_requests` + items + photos, `refunds` (one live refund per return), `store_credit_accounts` + append-only `store_credit_entries`, `credit_notes`, return-window/QC-SLA/auto-approve settings, per-category window | `migrations/versions/0013_returns.py` | ✅ |
| 13.2 | Store credit as tender: `payments.provider` gains `store_credit`, "one paid payment per order" becomes "one paid **gateway** payment", `payments.refunded_amount` tracks what went back | same | ✅ |
| 13.3 | Store credit ledger: balance under a row lock, append-only entries, never negative | `returns/credit.py` | ✅ |
| 13.4 | Return request: delivered only, inside the **shortest** applicable window (category beats tenant), one open return per shipment, refund value priced from the order line — never from the request | `returns/service.create_request`, `window_for`, `_price_items` | ✅ |
| 13.5 | Strict state machine with vendor decision, tenant override, auto-approve rules, and QC SLA escalation | `service.decide`, `TRANSITIONS`, `escalate_overdue_qc` | ✅ |
| 13.6 | Reverse pickup through the same courier adapters, tracked on the return | `service.book_reverse_pickup` | ✅ |
| 13.7 | QC: pass restocks with an append-only movement, fail restocks nothing and sends the parcel back to the buyer | `service.qc` | ✅ |
| 13.8 | Refund rails: bKash/SSLCommerz refund APIs for captures, store credit instantly, **manual queue** for COD (finance pays and records a reference), buyer-fault return postage deducted | `service.refund`, `complete_manual_refund` | ✅ |
| 13.9 | Credit note numbered per tenant with its VAT, issued exactly once per return | `service.issue_credit_note` | ✅ |
| 13.10 | Refund destinations encrypted (AES-GCM, context-bound), shown masked to staff | `service.decrypt_target`, `mask_target` | ✅ |
| 13.11 | Store credit spent at checkout: balance in the quote, applied at placement as a payment, an order it fully covers is confirmed without a gateway, and credit may not be combined with COD | `checkout/service.place_order`, `payments.amount_due` | ✅ |
| 13.12 | APIs: buyer (window, request, private photo upload, my returns, wallet), vendor (queue, decision, pickup, mark, QC), staff (override, refund, refund queue, credit notes, goodwill credit, customer balances) | `returns/router.py` | ✅ |
| 13.13 | Web: return request page, return status and "Return an item" on the order page, store-credit checkbox at checkout | `apps/web/src/app/(shop)/{orders,checkout}` | ✅ |

## Tests (295 api tests pass)
Damaged item: half of a two-unit line priced from the order, second open return refused, approve → reverse pickup booked with the original courier → received → QC pass restocks exactly one unit → refund goes back on the **bKash capture**, the payment becomes `partially_refunded`, a credit note is issued, and a second refund is impossible · QC fail restocks nothing, refunds nothing, and the capture stays `paid` · COD return paid as store credit: the wallet is credited, the balance shows in the next quote, is applied at placement, and the gateway is asked only for the remainder · credit covering the whole order confirms it with no gateway call at all and takes the stock · store credit cannot be overspent or driven negative · COD refund to a bKash number waits in the finance queue, the destination is encrypted at rest and masked (`••••1111`) in the API, completing it with a reference issues the credit note and cannot be repeated · the window is enforced and a category can shorten it (2 days beats the tenant's 7) · a buyer cannot return more than they bought or touch someone else's order · auto-approve rules decide without a human and overdue QC escalates exactly once · staff can overrule a vendor's rejection, and a rejected return is final for the vendor · ten more isolation routes registered.

## Notes
- **Nothing is restocked on the buyer's word.** Goods return to stock at QC pass only, with a movement row, so the stock ledger always explains itself.
- Store credit is modelled as tender, not as a discount: commission, VAT and vendor payable are unaffected by how the buyer funded the order, which keeps Phase 14's postings honest.
- `manual_bkash` / `manual_bank` refunds are deliberately human: bKash disbursement to a number that never paid us is a payout, not a refund, and it belongs in the finance queue with a reference.
- Credit-note **PDFs** (Mushak 6.3 style) arrive with tax documents in Phase 14; the numbered record and its VAT are already here.
