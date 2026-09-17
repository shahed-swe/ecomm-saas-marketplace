# Phase 11 — Payments & tenant gateways (3 weeks) ✅ COMPLETE

**Mission:** take money safely for **many different merchants at once**. Every tenant pays into its **own** bKash / SSLCommerz account with its own credentials; the platform never holds the funds and never trusts a browser or a callback about whether money arrived.

**Demo:** staff paste their bKash sandbox keys → health check turns the account green → a buyer places an order, is redirected to the gateway, comes back → the API asks bKash itself, confirms the shipments and moves reserved stock into sold stock → a replayed IPN changes nothing → a forged "success" callback for a payment the provider calls failed leaves the order unpaid → an abandoned attempt is closed by the reconciliation sweep.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 11.1 | Migration: `payment_accounts` (per tenant, encrypted credentials, health), `payments` (attempts, partial unique **one paid payment per order**), append-only `payment_events` unique on `(tenant, provider, event_id)`, `cod_receivables`; RLS + grants; `resolve_tenant_by_public_id()` SECURITY DEFINER for webhooks | `migrations/versions/0011_payments.py` | ✅ |
| 11.2 | One gateway interface for every provider: `create / verify / parse_callback / refund / health`, credentials passed per call (never module state) | `payments/gateways/base.py` | ✅ |
| 11.3 | bKash tokenized checkout adapter: grant token → create → execute with fallback to the authoritative status query | `payments/gateways/bkash.py` | ✅ |
| 11.4 | SSLCommerz adapter: session init → hosted page → IPN → `validationserverAPI` (the IPN alone proves nothing) | `payments/gateways/sslcommerz.py` | ✅ |
| 11.5 | Tenant credentials encrypted at rest (AES-GCM envelope, context-bound to `tenant:provider`), decrypted per request, **never** logged or returned — only a `••••1234` hint | `payments/service.py`, `core/crypto.py` | ✅ |
| 11.6 | Start a payment: order ownership, unpaid + still inside its payment window, older attempts superseded, redirect URL returned | `payments/service.start_payment` | ✅ |
| 11.7 | **Verify-first settlement**: provider is asked, amount must match to the paisa, order sub-orders confirmed and reservations consumed into `inventory_movements` — the one place prepaid stock becomes sold | `payments/service.settle`, `confirm_paid_order` | ✅ |
| 11.8 | Idempotent callbacks: `(tenant, provider, event_id)` unique; a replay is a no-op; unmatched events are kept for reconciliation | `payments/service.record_event`, `router.provider_webhook` | ✅ |
| 11.9 | Webhook routed by the tenant's **opaque public id** (never its uuid or host), resolved by one SECURITY DEFINER lookup, then an ordinary RLS-scoped session | `payments/router.py` | ✅ |
| 11.10 | COD: a receivable per shipment from placement, collected on delivery, cancelled with the order; COD orders can never open a gateway session | `payments/service.register_cod / collect_cod / cancel_cod_receivables` | ✅ |
| 11.11 | Per-tenant reconciliation: sweep re-verifies abandoned attempts every 15 minutes; staff summary of gateway + COD totals and unmatched callbacks | `service.reconcile_pending`, `reconciliation_summary`, `workers/settings.py` | ✅ |
| 11.12 | Staff APIs: set / health-check / disable a gateway account (masked), payment list, reconciliation; vendor + admin COD receivable lists | `payments/router.py` | ✅ |
| 11.13 | Web: checkout hands prepaid orders to the gateway, return page verifies with our API (never with the query string) and polls, "Pay now" on an unpaid order, COD wording | `apps/web/src/app/(shop)/orders/[number]/payment`, `checkout`, `orders` | ✅ |

## Tests (224 api tests pass)
Happy path: start → confirm → order `processing`, reserved stock becomes sold with exactly one `-2` inventory movement, a second confirm settles nothing and does not call the provider again, a paid order cannot open a new session · **a callback is a nudge, not evidence**: a browser claiming `status=success` while bKash says failed leaves the order `pending_payment`; the same event replayed returns `duplicate` without a provider call; the later genuine "paid" answer settles it · **amount mismatch** (৳1 for a ৳1000 order) fails as `amount_mismatch` and never confirms the order · **cross-tenant webhook**: tenant B's endpoint with tenant A's reference is `ignored`, A's order untouched; unknown tenant and unknown provider are 404 · COD: receivable created per shipment, listed for vendor and staff, collection is idempotent and pays the COD payment, cancelling the order cancels the receivable and the payment · credentials: ciphertext in the database (no secret in it), nothing secret in the audit log, `••••2345` hint, failing health check flips the account to `failing`, disabled account refuses new payments, incomplete credentials 422 · ownership: another buyer gets 404 on start, confirm, status and the return-page lookup · reconciliation: an abandoned attempt is settled from the provider, a second sweep has nothing to do, an unmatched callback is counted.

## Notes
- **Money never moves on our say-so.** `settle()` is the only path that marks a payment paid, and it always calls the provider first. The partial unique index `uq_payment_paid_per_order` makes a second paid payment for one order impossible even under a race.
- Real bKash / SSLCommerz sandbox credentials are needed to exercise the adapters against the live sandboxes; the suite proves the *flow* with an injected gateway, and `app.state.payment_gateways` is the same seam a sandbox rehearsal uses.
- Refunds are wired at the gateway level (`refund()`) but the refund *workflow* — approvals, store credit, credit notes — is Phase 13; ledger postings for captures and COD settlement are Phase 14.
- COD receivables move `due → collected` on delivery (Phase 12 courier webhooks call `collect_cod`) and `collected → settled` when the courier remits.
