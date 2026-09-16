# ADR 0006 — Payments to tenant-owned accounts
Status: Accepted · 2026-09-16

## Decision
- V1 methods: **COD, bKash (tokenized checkout), SSLCommerz**. Stripe Connect and Nagad removed.
- Buyer money settles to the **tenant's** merchant accounts. The platform never holds GMV.
- Credentials: `payment_accounts(tenant_id, provider, mode sandbox|live, credential_ciphertext, key_version, status unverified|healthy|failing, last_checked_at)`; encrypted with Vault transit; never returned by API (masked).
- Verify-first: bKash `execute` + `query`; SSLCommerz IPN + `validationserverAPI`. Callbacks at `/webhooks/{provider}/{tenant_public_id}`; tenant chosen by path, authenticity proven by that tenant's credential.
- Idempotency: `payment_events(tenant_id, provider, event_id) unique`; `Idempotency-Key` on create.
- COD controls per tenant: max order value, allowed districts, OTP-verified phone, refusal-rate threshold, optional prepaid delivery fee, blocklist.
- Nightly reconciliation per tenant per provider.
- Platform billing (tenant → us) uses the same interface with platform-scoped credentials.
