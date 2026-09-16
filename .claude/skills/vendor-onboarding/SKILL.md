---
name: vendor-onboarding
description: Build seller signup, KYC document collection and review, Stripe Connect account linking, approval and rejection workflow, storefront setup, suspension and reinstatement, and vendor performance gating. Use this skill for anything about vendor registration, verification, approval, documents, store setup, suspension, or "let sellers join".
---

# Vendor Onboarding

Onboarding is a funnel and a compliance gate at the same time. Every extra step
costs sellers; every skipped step costs the platform.

## State machine

```
registered → documents_submitted → under_review → approved
                   ▲                     │            │
      changes_requested ─────────────────┤            ├→ suspended → approved
                                          └→ rejected └→ closed
```
Store the state on `vendors.status`, transitions in `vendor_events` with actor,
reason, and timestamp. Only `approved` unlocks publishing and payouts.

## What to collect, and when

**At signup (keep it to 60 seconds):** store name, contact email, phone, country.
Let them start building the catalogue in draft immediately — a seller who has
loaded 20 products is far more likely to complete verification.

**Before publishing:** business type, legal name, address, tax id, and the
identity documents your jurisdiction requires. Trade licence and TIN/BIN for
Bangladesh; adjust the document set per country with a config-driven schema
rather than hardcoded fields.

**Before payout:** Stripe Connect onboarding (hosted link — never collect bank
details yourself), bank account verification, and `payouts_enabled` confirmed
from the provider, not assumed after a redirect.

## Document handling
Private bucket. Server-side encryption. Access only through a signed-URL endpoint
that logs the viewing admin. Store status and expiry, not extracted ID numbers.
Auto-request renewal before expiry; grace period, then a listing restriction —
not an instant suspension.

## Review queue
Admin sees: documents side by side, extracted metadata, a duplicate check against
existing vendors (same tax id, bank account, device fingerprint, or address is the
classic banned-seller-returning signal), and three actions — approve, request
changes with a reason, reject with a reason. Every action is audited and the
reason is shown to the vendor. Silent rejections generate support tickets.

## Storefront setup (post-approval checklist in their dashboard)
Logo and banner (through the media pipeline), bio, return policy, shipping zones
and rates, holiday mode, payout schedule. Gate "publish first product" on
policies existing — buyers ask for them, and disputes hinge on them.

## Suspension
Triggered by the performance score, a policy violation, or a compliance failure.
Effects: listings hidden within one revalidation cycle, no new orders, existing
orders still fulfillable and still payable. Disputed amounts held; settled money
released. The vendor sees the reason, the evidence summary, and the appeal path.
Reinstatement requires an admin action and is audited.

## Metrics to instrument from day one
Signup → documents submitted → approved → first listing → first sale, with drop-off
at each step and time-in-state. This funnel tells you which requirement is
costing you sellers; without it you are guessing.
