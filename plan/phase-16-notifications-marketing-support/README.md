# Phase 16 — Notifications, marketing & support (3 weeks) ✅ COMPLETE

**Mission:** one way to tell someone something — push, SMS, email or in-app — that respects what they asked for, never says it twice, and never breaks the thing it is describing. Plus the tenant's own marketing tools and a place for customers to ask for help.

**Demo:** an order arrives → the buyer gets a push, an SMS and an inbox entry in their language; the courier retries a webhook and nobody is told twice; the tenant rewords the Bangla push in the admin API and the next order uses it; an abandoned cart is nudged once a day, but never during quiet hours and never to someone who opted out; the purchase lands in GA4 and Meta server-side with hashed identifiers; a buyer opens a ticket and staff answer it with an internal note the customer never sees.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 16.1 | Migration: `notifications` (+ dedupe index), templates, per-buyer preferences, device tokens, `push_campaigns`, `support_tickets` + append-only messages, `analytics_destinations` + queued `analytics_events`, per-user locale | `migrations/versions/0016_notifications.py` | ✅ |
| 16.2 | Default bn/en copy for every event, with tenant overrides layered on top and the inbox mirroring the push wording | `notifications/templates.py`, `service._template` | ✅ |
| 16.3 | One send path: render → decide → record → deliver, transactional always, marketing only with consent and outside quiet hours, `dedupe_key` making retries harmless, delivery failures recorded not raised | `notifications/service.notify` | ✅ |
| 16.4 | Channel adapters: FCM push, SMS (existing gateway), email, in-app; console and fake implementations for dev and tests | `notifications/channels.py` | ✅ |
| 16.5 | Event hooks wired into the real flows: order placed, payment settled, parcel on its way / delivered, refund completed | `checkout/router`, `payments/service`, `fulfilment/service`, `returns/router` | ✅ |
| 16.6 | Buyer surfaces: device registration and revocation, inbox with unread count, marketing preferences and quiet hours | `notifications/router.py` | ✅ |
| 16.7 | Push campaigns with four segments, audience counted before sending, send-once semantics, and a record of sent vs suppressed | `notifications/campaigns.py` | ✅ |
| 16.8 | Abandoned-cart nudge: one per cart per day, after the tenant's own idle threshold, marketing rules applied | `campaigns.nudge_abandoned_carts` | ✅ |
| 16.9 | Per-tenant GA4 + Meta: public ids for the page, secrets encrypted, **server-side purchase events** queued in the same transaction as the order, hashed identifiers, deduplicated against the browser pixel by `event_id` | `notifications/analytics.py` | ✅ |
| 16.10 | Support tickets: buyer and vendor can open them, staff reply publicly or with internal notes, first-response clock, statuses, filtered like chat | `notifications/support.py` | ✅ |
| 16.11 | Hourly marketing sweep (nudges + analytics flush) with its own worker app state | `workers/settings.marketing_sweep` | ✅ |
| 16.12 | Web: GA4/Meta tags from public ids only, buyer notification inbox and preference page | `apps/web/src/components/shop/AnalyticsTags.tsx`, `account/notifications` | ✅ |

## Tests (373 api tests pass)
An order notifies on push, SMS and in-app, then shipping and delivery follow, and marking the inbox read clears the unread count · a retried courier webhook and a second identical status send nothing further — one "on its way" per shipment · a tenant reworded template replaces ours, and disabling one channel silences only that channel · abandoned carts are nudged once per cart per day; a buyer who opts out of marketing gets no campaign push but still gets their order confirmation · a campaign sends once, reports audience vs sent vs suppressed, and a second send is a no-op · a revoked device stops receiving push while the in-app copy still arrives, and the suppression is visible in the notification log · the purchase event is queued server-side with hashed email/phone, flushed exactly once to GA4 with the tenant's own secret, and a rejecting provider leaves the row `failed` with the reason rather than losing it · identifiers hash the way GA4 and Meta ask · quiet hours wrap around midnight correctly, and equal start/end means "off" · a support ticket runs open → staff reply (with an internal note the customer never sees) → customer reply → closed, refuses messages afterwards, and is invisible to another buyer; a vendor can raise one too · five more isolation routes registered.

## Notes
- **Transactional is not marketing.** Order, payment, delivery and refund messages ignore marketing preferences and quiet hours entirely — a person who turned off offers still needs to know their parcel arrived.
- Delivery never raises into the caller: a failed SMS on a paid order writes `failed` on the notification row and leaves the payment alone.
- Analytics secrets are encrypted at rest and never reach the browser; the storefront endpoint returns public ids only, which is exactly what a page needs.
- FCM credentials are per platform project (`FcmPush`); a tenant's white-label app carries its own sender id, which is why the adapter takes them per call rather than at import time — the mobile phases wire the real ones.
