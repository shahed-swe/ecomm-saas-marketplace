# Phase 15 — Trust, safety & messaging (3 weeks) ✅ COMPLETE

**Mission:** make the marketplace worth trusting — reviews that are real, disputes that stop the money, a vendor scorecard built from facts, and a chat nobody can use to pull the deal off the platform.

**Demo:** a delivered buyer writes a review (one per purchased line) and the stars move; a review containing a phone number waits for a moderator instead of publishing; a dispute places a payout hold within seconds and releases it the moment staff decide, either way; a vendor's "WhatsApp me on ০১৭…, pay bKash" turns into `[hidden]` and lands in the staff queue.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 15.1 | Migration: `reviews` (+votes, product/vendor rating rollups), `disputes` (+messages, one open per shipment), `conversations`/`messages` (append-only), `vendor_scores`, trust settings | `migrations/versions/0015_trust.py` | ✅ |
| 15.2 | Contact-exfiltration filter: Bangla digits normalised, phone numbers found through spacing tricks, emails, links (`.com.bd` too), social handles, off-platform payment words | `trust/filters.py` | ✅ |
| 15.3 | Verified reviews only: delivered line, inside the review window, one per `order_item`, flagged text held for moderation, ratings recomputed from **published** reviews only | `trust/service.create_review`, `refresh_ratings` | ✅ |
| 15.4 | Vendor reply (once, filtered), staff moderation (approve / reject / hide), helpful votes once per buyer | `service.reply_to_review`, `moderate_review` | ✅ |
| 15.5 | Disputes: open → hold the vendor's payouts → both sides answer → staff resolve → hold released whichever way it went; overdue disputes escalate themselves | `service.open_dispute`, `resolve_dispute`, `escalate_overdue_disputes` | ✅ |
| 15.6 | Buyer↔vendor chat scoped to a shipment: contact details redacted in place, the attempt flagged and fingerprinted, staff queue and conversation blocking | `service.send_message`, `router` | ✅ |
| 15.7 | Vendor scorecard from five facts — on-time delivery, cancellations, returns, disputes, verified rating — banded good / watch / risk, visible to the vendor as well as the tenant | `service.score_vendor`, `score_all` | ✅ |
| 15.8 | Twice-daily trust sweep: escalate overdue disputes, recompute every scorecard | `workers/settings.trust_sweep` | ✅ |
| 15.9 | APIs: buyer (review, vote, disputes, chat), vendor (reviews, reply, disputes, chat, scorecard), staff (moderation queue, disputes, flagged messages, block, scores) | `trust/router.py` | ✅ |
| 15.10 | Web: PDP review summary, histogram, list with seller replies and helpful votes; `aggregateRating` in JSON-LD | `apps/web/src/components/shop/Reviews.tsx`, PDP | ✅ |

## Tests (351 api tests pass)
Only a delivered purchase can be reviewed; a second review of the same line is refused and a stranger's attempt is a 404; the public list shows "Rumana A." rather than an email, with a histogram · a review containing a phone number is held `pending`, moves no star, appears in the moderation queue with its reason, and rejecting it leaves the rating untouched · a vendor may reply once, and the reply goes through the same filter (a phone number in it becomes `[hidden]`) · helpful votes count once per buyer however many times the button is pressed · a dispute holds the vendor's payouts immediately, refuses a second open dispute on the same shipment, moves state as each side speaks, and releases the hold on resolution — in the vendor's favour, too · an unanswered dispute escalates once and only once · chat redacts a Bangla-digit phone number with WhatsApp and bKash in the same breath, flags all three reasons, shows the redaction to both sides, lists it for staff, and a blocked conversation takes no more messages · messages cannot be edited afterwards, even by the platform role · the filter leaves ordinary sentences with numbers ("3 pieces at 4500 taka") alone · the scorecard reads 100% on-time for a clean vendor and drops once returns and disputes appear, with the same numbers visible to the vendor · eight more isolation routes registered.

## Notes
- **Redact, don't silently drop.** A dropped message teaches people to try harder; a visible `[hidden]` teaches the rule. The original text is never stored — only a SHA-256 fingerprint, which is enough to prove to a moderator that the message was changed.
- A dispute holds *money*, not the vendor: the hold is released on resolution whoever won, because a marketplace that freezes liquidity indefinitely loses its good sellers first.
- The scorecard deliberately contains no subjective input a tenant could tune against a vendor; every term is an event the system recorded.
- Review photos, seller badges and buyer reputation are deliberately out of scope here; notifications for all of these arrive in Phase 16.
