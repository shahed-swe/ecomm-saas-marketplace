---
name: double-entry-ledger
description: Strict mathematical accounting rules for the marketplace double-entry ledger — append-only entries, atomic balanced groups (debit=credit=0), chart of accounts, commission snapshot and resolution, reversal patterns for refunds/disputes/returns, hold and reserve logic, vendor balance projections, payout idempotency, and nightly 3-way reconciliation (ledger ↔ provider ↔ projection). Load this skill when implementing Phase 6 capture ledger, Phase 7 payment settlement, Phase 8 refund/cancel reversals, Phase 9 COD remittance, Phase 10 payout runs, or Phase 13 dispute hold/release.
---

# Double-Entry Ledger — Procedural Skill

## 1. The fundamental invariant

**Every `entry_group_id` sums to zero.** This is not a guideline — it is a
database constraint enforced on every write. If a group does not balance, the
transaction rolls back.

```python
async def post_group(entries: list[LedgerEntry]) -> UUID:
    group_id = uuid7()
    total = Decimal(0)
    for entry in entries:
        entry.entry_group_id = group_id
        if entry.direction == 'debit':
            total += entry.amount
        else:
            total -= entry.amount

    if total != Decimal(0):
        raise LedgerImbalanceError(f"Group {group_id} sums to {total}, not 0")

    async with session.begin():
        for entry in entries:
            session.add(entry)

    return group_id
```

---

## 2. Chart of accounts

| Account | Owner | Purpose |
|---|---|---|
| `platform_clearing` | platform | temporary holding during settlement |
| `vendor_payable` | vendor | what the platform owes the vendor |
| `platform_revenue` | platform | commission + retained fees |
| `payment_fees` | platform | gateway fees (if platform-absorbed) |
| `reserve` | vendor | rolling reserve held for new vendors |
| `cod_receivable` | vendor | COD amount due from rider/vendor |

Every entry carries `vendor_id` (nullable for platform-only accounts like
`platform_clearing`), `reference_type` + `reference_id` (e.g. `sub_order`,
`payout_run`, `dispute`), and `entry_type` for categorisation.

---

## 3. Entry types and their balanced groups

### 3.1 Sale capture (Phase 6/7)
When a payment is verified and captured:
```
For each sub_order:
  debit   platform_clearing    +100.00  (buyer paid)
  credit  vendor_payable        -87.00  (vendor earns)
  credit  platform_revenue      -10.00  (commission)
  credit  payment_fees           -3.00  (gateway fee, if platform eats)
  ─────────────────────────────────────
  SUM                             0.00  ✓
```

### 3.2 Refund (Phase 8)
```
  debit   vendor_payable        +87.00  (take back from vendor)
  debit   platform_revenue      +10.00  (return commission — or not, policy)
  credit  platform_clearing    -100.00  (return to buyer via provider)
  ─────────────────────────────────────
  SUM                             0.00  ✓ (reversing entries — never edit originals)
```

If policy retains the commission fee:
```
  debit   vendor_payable        +97.00  (vendor bears full refund)
  credit  platform_clearing    -100.00
  credit  platform_revenue       -0.00  (commission NOT reversed)
  debit   payment_fees           +3.00  (fee NOT reversed)
  ─────────────────────────────────────
  SUM                             0.00  ✓
```
**This must be explicit in the vendor agreement.** Never silently omit a
reversal — use a distinct `entry_type` like `refund_fee_retained`.

### 3.3 Partial refund
Same pattern but with proportional amounts. Commission reversal is proportional
to the refunded amount, not the total order.

### 3.4 COD remittance (Phase 9)
When a rider remits collected COD cash:
```
  debit   cod_receivable        +100.00  (clearing the receivable)
  credit  vendor_payable         -87.00
  credit  platform_revenue       -10.00
  credit  payment_fees            -3.00
  ─────────────────────────────────────
  SUM                              0.00  ✓
```

### 3.5 Dispute hold (Phase 13)
```
  debit   vendor_payable        +87.00  (hold from vendor)
  credit  reserve               -87.00  (move to reserve)
  ─────────────────────────────────────
  SUM                             0.00  ✓
```

### 3.6 Dispute release (Phase 13)
Resolved in vendor's favour:
```
  debit   reserve               +87.00
  credit  vendor_payable        -87.00
  ─────────────────────────────────────
  SUM                             0.00  ✓
```

Resolved in buyer's favour (refund):
```
  debit   reserve               +87.00  (release hold)
  credit  platform_clearing     -87.00  (refund buyer)
  ─────────────────────────────────────
  SUM                             0.00  ✓

  # Plus commission reversal group:
  debit   platform_revenue      +10.00
  credit  reserve               -10.00  (or vendor_payable)
  ─────────────────────────────────────
  SUM                             0.00  ✓
```

### 3.7 Payout (Phase 10)
```
  debit   vendor_payable        +500.00  (reduce vendor's balance)
  credit  platform_clearing    -500.00   (platform sends money)
  ─────────────────────────────────────
  SUM                             0.00  ✓
```

### 3.8 Adjustment (admin-only, any phase)
```
  debit   <source_account>     +amount
  credit  <target_account>     -amount
  description = "admin reason here"
  ─────────────────────────────────────
  SUM                             0.00  ✓
```
Adjustments are always admin-authored, always have a reason, and always produce
an audit log entry.

---

## 4. Commission resolution

```python
async def resolve_commission(sub_order) -> CommissionSnapshot:
    # Resolution order:
    # 1. vendor+category override
    rule = await commission_repo.find(
        scope='vendor_category',
        vendor_id=sub_order.vendor_id,
        category_id=sub_order.primary_category_id,
    )
    # 2. vendor override
    if not rule:
        rule = await commission_repo.find(
            scope='vendor', vendor_id=sub_order.vendor_id
        )
    # 3. category rate
    if not rule:
        rule = await commission_repo.find(
            scope='category', category_id=sub_order.primary_category_id
        )
    # 4. global rate
    if not rule:
        rule = await commission_repo.find(scope='platform')

    amount = compute_amount(sub_order.subtotal, rule)
    return CommissionSnapshot(
        rate=rule.percent,
        amount=amount,
        source=f"{rule.scope}:{rule.id}",  # traceable
    )
```

**Snapshot once, at capture, on the sub_order.** Never recompute.

---

## 5. Vendor balances (cached projection)

```python
async def compute_vendor_balance(vendor_id) -> VendorBalance:
    entries = await ledger_repo.sum_by_vendor(vendor_id)

    available = entries.total  # sum of all entries for this vendor
    pending = await sub_order_repo.sum_undelivered(vendor_id)
    reserved = await reserve_repo.sum_held(vendor_id)
    disputed = await dispute_repo.sum_held_amounts(vendor_id)

    payable = available - pending - reserved - disputed
    return VendorBalance(
        available=available,
        pending=pending,
        reserved=reserved,
        disputed=disputed,
        payable=max(payable, Decimal(0)),
        negative_since=... if payable < 0 else None,
    )
```

The `vendor_balances` table is a **cache**. The ledger is truth. Reconcile
nightly.

---

## 6. Payout idempotency

```python
async def execute_payout(vendor_id, period_end) -> PayoutRun:
    # UNIQUE (vendor_id, period_end) — second call is a no-op
    existing = await payout_repo.find(vendor_id, period_end)
    if existing:
        return existing  # idempotent — already run

    balance = await compute_vendor_balance(vendor_id)
    if balance.payable < vendor.payout_min_amount:
        return PayoutRun(status='skipped', amount=0)

    # Execute transfer via provider
    transfer_ref = await stripe.create_transfer(
        amount=balance.payable,
        destination=vendor.connect_account_id,
    )

    # Record BEFORE marking settled (crash-safe)
    payout = PayoutRun(
        vendor_id=vendor_id,
        period_end=period_end,
        amount=balance.payable,
        provider='stripe_connect',
        provider_ref=transfer_ref,
        status='completed',
    )
    await payout_repo.create(payout)

    # Post balanced ledger group
    await post_group([
        LedgerEntry(account='vendor_payable', direction='debit',
                    amount=balance.payable, vendor_id=vendor_id),
        LedgerEntry(account='platform_clearing', direction='credit',
                    amount=balance.payable),
    ])

    return payout
```

---

## 7. Nightly 3-way reconciliation

```python
async def reconcile_nightly():
    for vendor in await vendor_repo.all_approved():
        # Source 1: ledger sum
        ledger_balance = await ledger_repo.sum_by_vendor(vendor.id)
        # Source 2: cached projection
        cached = await vendor_balance_repo.get(vendor.id)
        # Source 3: provider (Stripe balance for connected account)
        provider_balance = await stripe.get_balance(vendor.connect_account_id)

        if ledger_balance != cached.available:
            alert(f"Vendor {vendor.id}: ledger {ledger_balance} != "
                  f"cached {cached.available}")
            # DO NOT overwrite cached to match — find the cause

        if abs(ledger_balance - provider_balance) > DRIFT_THRESHOLD:
            alert(f"Vendor {vendor.id}: ledger {ledger_balance} != "
                  f"provider {provider_balance}")

    # Also: verify all entry groups sum to zero
    unbalanced = await ledger_repo.find_unbalanced_groups()
    if unbalanced:
        critical_alert(f"UNBALANCED GROUPS: {unbalanced}")
```

---

## 8. Test patterns

```python
def test_every_group_sums_to_zero():
    groups = db.execute("SELECT entry_group_id, SUM(CASE WHEN direction='debit'
        THEN amount ELSE -amount END) as balance
        FROM ledger_entries GROUP BY entry_group_id
        HAVING SUM(CASE WHEN direction='debit' THEN amount ELSE -amount END) != 0")
    assert groups == []

def test_payout_idempotency():
    run1 = execute_payout(vendor_id, period_end)
    run2 = execute_payout(vendor_id, period_end)
    assert run1.id == run2.id  # same run returned
    assert count_transfers() == 1  # only one transfer created

def test_refund_reverses_proportionally():
    # Refund 50% of a sub_order
    # Assert commission reversal is exactly 50% of original commission
    # Assert the reversing group sums to zero
```
