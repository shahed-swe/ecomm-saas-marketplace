---
name: bd-payment-gateways
description: Step-by-step procedural knowledge for integrating Bangladesh payment gateways (bKash tokenized checkout, Nagad server-verified, SSLCommerz hosted/IPN) and COD receivable tracking in a multi-vendor marketplace with split-aware ledger settlement. Load this skill when implementing Phase 7 payment gateway sub-phases or debugging BD payment verification flows.
---

# BD Payment Gateways — Procedural Skill

## 1. The verification-first principle

Every BD gateway follows the same trust model: **the server verifies, the
callback informs.** A client-side redirect or an unverified callback NEVER marks
an order paid. The flow is always:

```
buyer → initiate (backend) → redirect/SDK (client) → callback/IPN (gateway→backend)
  → VERIFY via server-to-server API call → mark paid → split ledger entry
```

If the verification call fails or returns a non-success, the order stays
`pending_payment`. The sweep drains it after expiry.

---

## 2. bKash Tokenized Checkout

### 2.1 Configuration
```python
BKASH_APP_KEY = env("BKASH_APP_KEY")
BKASH_APP_SECRET = env("BKASH_APP_SECRET")
BKASH_USERNAME = env("BKASH_USERNAME")
BKASH_PASSWORD = env("BKASH_PASSWORD")
BKASH_BASE_URL = env("BKASH_BASE_URL")  # sandbox vs prod
```

### 2.2 Flow
1. **Grant token:** `POST /tokenized/checkout/token/grant` with app_key +
   app_secret + username + password → `id_token` (cached, ~1 hour TTL).
2. **Create payment:** `POST /tokenized/checkout/create` with amount, currency
   (BDT), invoice, intent (`sale`), merchant_invoice_number → `paymentID` +
   `bkashURL`.
3. **Redirect:** buyer completes on bKash's hosted page → redirects to
   `callbackURL` with `paymentID` + `status`.
4. **Execute:** `POST /tokenized/checkout/execute` with `paymentID` → response
   includes `transactionStatus`.
5. **VERIFY (critical):** `POST /tokenized/checkout/payment/status` (query API)
   with `paymentID` → compare `transactionStatus === 'Completed'` AND
   `amount === expected_amount` AND `currency === 'BDT'`.
6. **Record:** write `payments` row + `payment_events (provider='bkash',
   event_id=paymentID)` UNIQUE → split into balanced ledger groups per sub-order.

### 2.3 Idempotency
- `payment_events (provider, event_id)` UNIQUE constraint.
- `Idempotency-Key` on the create call (from the checkout session).
- Duplicate callback → lookup existing `payment_events` → no-op.

### 2.4 Error handling
- Grant token expired → refresh and retry once.
- Execute returns non-Completed → leave as `pending_payment`.
- Query API timeout → retry with exponential backoff (max 3).
- Amount mismatch on verify → reject + alert (potential tampering).

---

## 3. Nagad

### 3.1 Flow
1. **Initialize:** `POST /api/dfs/check-out/initialize/{merchantId}/{orderId}`
   with challenge (signed with merchant private key) → `sensitiveData` +
   `signature`.
2. **Complete:** `POST /api/dfs/check-out/complete/{paymentReferenceId}` with
   sensitive data (challenge response, amount, orderId) → redirects buyer.
3. **Callback:** Nagad posts to `callbackURL` with `payment_ref_id`, `status`,
   `amount`.
4. **VERIFY (critical):** `GET /api/dfs/verify/payment/{paymentReferenceId}` →
   confirm `statusCode === '000'` AND `amount` matches AND `orderId` matches.
5. **Record:** same pattern — `payment_events (provider='nagad',
   event_id=paymentReferenceId)` → split ledger.

### 3.2 Signature verification
Nagad responses are signed. Verify the signature against Nagad's public key
before processing. A failed signature → reject + alert.

---

## 4. SSLCommerz

### 4.1 Flow
1. **Session init:** `POST /gwprocess/v4/api.php` with store_id, store_passwd,
   total_amount, currency, tran_id, success_url, fail_url, cancel_url, ipn_url,
   and customer/shipping info → `GatewayPageURL` + `sessionkey`.
2. **Redirect:** buyer completes on SSLCommerz hosted page.
3. **IPN (Instant Payment Notification):** SSLCommerz posts to `ipn_url` with
   `tran_id`, `val_id`, `amount`, `store_amount`, `status`.
4. **VERIFY (critical):** `GET /validator/api/validationserverAPI.php?val_id=
   {val_id}&store_id={store_id}&store_passwd={store_passwd}&format=json` →
   confirm `status === 'VALID'` AND `currency_amount` matches AND `tran_id`
   matches.
5. **Record:** `payment_events (provider='sslcommerz', event_id=val_id)` →
   split ledger.

### 4.2 IPN security
- Validate the IPN came from SSLCommerz IP ranges.
- Always call the validation API — never trust the IPN POST alone.
- `store_amount` may differ from `currency_amount` if SSLCommerz took a fee
  server-side — record both.

---

## 5. COD Receivable Tracking

COD is **not "no payment"** — it is a payment method with delivery-time
settlement.

### 5.1 At order placement
```python
for sub_order in order.sub_orders:
    if payment_method == 'cod':
        create cod_receivable(
            sub_order_id=sub_order.id,
            vendor_id=sub_order.vendor_id,
            amount=sub_order.total,
            status='due'
        )
```

### 5.2 At delivery (rider collects)
```python
cod_receivable.status = 'collected'
cod_receivable.delivery_id = delivery.id
cod_receivable.collected_at = now
# rider_cash_ledger updated by dispatch-delivery-engineer
```

### 5.3 At remittance (rider remits to platform)
```python
cod_receivable.status = 'remitted'
cod_receivable.remitted_at = now
# Write balanced ledger_entries group:
#   debit  cod_receivable  (clearing the receivable)
#   credit vendor_payable  (vendor earns)
#   debit  vendor_payable  (commission)
#   credit platform_revenue (platform earns)
```

### 5.4 Risk controls
- Order-value cap per zone (admin-configured).
- Area allow-list (not all zones support COD).
- Buyer phone verification required for COD.
- Abuse scoring: high return-rate buyers are COD-blocked.

---

## 6. Split-aware settlement (all providers)

After any verified payment:
```python
for sub_order in order.sub_orders:
    commission = resolve_commission(sub_order)  # snapshot
    sub_order.commission_rate = commission.rate
    sub_order.commission_amount = commission.amount
    sub_order.rate_source = commission.source
    sub_order.vendor_net = sub_order.total - commission.amount

    post_balanced_group(
        entries=[
            (platform_clearing, debit, sub_order.total),
            (vendor_payable, credit, sub_order.vendor_net),
            (platform_revenue, credit, commission.amount),
        ]
    )
    # assert sum == 0
```

The group is written atomically. If any entry fails, the whole group rolls back.
