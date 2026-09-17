# Phase 12 — Fulfilment & couriers (3 weeks) ✅ COMPLETE

**Mission:** get the parcel out of the vendor's hands and the cash back into the tenant's, through **Pathao, Steadfast and RedX** — with the courier as the only source of truth about where a parcel is, and a COD statement that is *matched*, never massaged.

**Demo:** staff store their courier keys and an ordered rule list → a vendor packs a shipment and books it with one click → the parcel's webhooks move the order through picked-up, in transit, delivered → COD becomes money owed → the courier's CSV statement is imported and each consignment is matched, with the short-paid one flagged instead of quietly accepted.

## Work breakdown
| # | Task | Files | Status |
|---|---|---|---|
| 12.1 | Migration: `courier_accounts` (tenant, or per vendor when allowed; encrypted), `courier_rules`, `shipments` (one live parcel per sub-order), append-only `shipment_events`, `courier_settlements` + lines, `geo_courier_areas`; RLS + grants | `migrations/versions/0012_fulfilment.py` | ✅ |
| 12.2 | One courier interface — `quote, book, cancel, track, label, parse_webhook, fetch_settlements, health` — and the **normalised status vocabulary** every provider word maps into | `fulfilment/couriers/base.py` | ✅ |
| 12.3 | Pathao adapter (OAuth token, order create, city/zone/area ids, status map) | `couriers/pathao.py` | ✅ |
| 12.4 | Steadfast adapter (api-key headers, create order, status by consignment) | `couriers/steadfast.py` | ✅ |
| 12.5 | RedX adapter (bearer token, parcel create/track/cancel) | `couriers/redx.py` | ✅ |
| 12.6 | Courier selection: ordered tenant rules by district, zone, weight and COD ceiling, vendor override, healthy-account fallback | `fulfilment/service.select_courier` | ✅ |
| 12.7 | Booking: state check, COD amount = **this shipment's** total (never the order's), area code resolution, shipment row, sub-order → `ready_to_ship`, receivable bound to the parcel | `service.book_shipment` | ✅ |
| 12.8 | Event handling: idempotent on `(tenant, courier, event_id)`, **never backwards** (a late "in transit" cannot un-deliver), unknown word → ops queue, sub-order status mapped, COD stock consumed at pick-up, delivery collects COD, return restocks with a movement | `service.apply_event` | ✅ |
| 12.9 | Webhook per courier routed by the tenant's opaque public id, then an ordinary RLS-scoped session | `fulfilment/router.py` | ✅ |
| 12.10 | Polling every 15 min for non-terminal parcels + stuck-parcel flagging after the SLA | `service.poll_open_shipments`, `flag_stuck_shipments`, `workers/settings.courier_sweep` | ✅ |
| 12.11 | COD settlement import (CSV, any courier's column names): matched / mismatch / unmatched / duplicate lines, receivables settled only on an exact match, mismatches flag the parcel | `service.import_settlement`, `parse_statement_csv` | ✅ |
| 12.12 | Vendor APIs (ready, ship, shipment detail with its event trail, cancel), staff APIs (accounts, ordered rule list, shipment list, ops queue, resolve, settlements), buyer tracking | `fulfilment/router.py` | ✅ |
| 12.13 | Web: courier, parcel status and tracking link on the buyer's order page | `apps/web/src/app/(shop)/orders/[number]` | ✅ |

## Tests (255 api tests pass)
Full COD journey: pack → book (the courier is asked to collect exactly the shipment total) → double booking refused → in-transit takes the stock off the shelf → buyer sees the tracking link → delivered collects the COD and pays the COD payment · events are idempotent (same notification → `duplicate`) and **never run backwards** (a late "in transit" after delivery is `stale`, and COD is not collected twice) · an unmapped courier word moves nothing, flags the parcel with its raw word kept in the event trail, and ops can clear it · return to merchant puts stock back with an append-only `return` movement and cancels the receivable · rules pick the carrier (a ৳1000 COD parcel falls past a ৳500-ceiling rule) and an explicit vendor choice wins · cancelling a booking returns the sub-order to the vendor and allows re-booking; a delivered parcel cannot be cancelled · polling settles a parcel whose webhook never arrived; a silent parcel is flagged once after the SLA · settlement statement: one matched, one mismatch (flagged, **not** settled), one unmatched, re-import refused · a courier webhook for another tenant is `unknown_consignment` and touches nothing · courier credentials are encrypted, masked (`••••9931`), health-checked, and incomplete ones are refused · isolation harness extended with six fulfilment routes.

## Notes
- **Vendors never declare delivery.** They mark goods ready and book; every status after that comes from the courier, because delivery is what moves money.
- Stock timing is deliberate: prepaid stock leaves at payment (Phase 11), COD stock leaves at pick-up, and a return-to-merchant puts it back — every move with a movement row.
- Ledger postings for `cod_delivery` / `cod_settlement` / `courier_fee` are Phase 14; this phase records the facts they will post from.
- `geo_courier_areas` is seeded per courier as tenants onboard (Pathao needs numeric city/zone/area ids); the adapter falls back to the free-text area when no code is known.
- Label PDFs: all three couriers print from their own panels today, so `label()` raises rather than pretending.
