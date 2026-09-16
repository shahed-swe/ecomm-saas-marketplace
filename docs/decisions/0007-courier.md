# ADR 0007 — Courier-only delivery
Status: Accepted · 2026-09-16

## Decision
No first-party riders. `CourierAdapter` interface: `quote, book, cancel, track, label, parse_webhook, fetch_settlements`. V1 adapters: **Pathao, Steadfast, RedX**.
- Credentials per tenant (`courier_accounts`), optionally per vendor when tenant enables `vendor_own_courier`.
- One shipment per sub-order. Normalised status: `booked, picked_up, in_transit, out_for_delivery, delivered, partial_delivered, failed_attempt, returning, returned, cancelled`. Unknown provider status → ops queue.
- Webhook-first, polling every 15–30 min for non-terminal shipments; stuck alert after tenant SLA.
- Courier selection rules by district/area, weight, COD amount; manual override.
- COD settlement statements imported (API or CSV), matched per consignment, posted as `cod_settlement`; mismatches to a queue.
- BD geography (`geo_divisions, geo_districts, geo_upazilas, geo_areas`) holds per-courier area codes.
