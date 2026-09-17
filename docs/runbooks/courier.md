# Couriers

## Webhooks stopped

1. Check the ops queue: `GET /api/v1/admin/shipments?needs_attention=true`.
2. The 15-minute `courier_sweep` polls every non-terminal parcel, so tracking keeps moving even
   with webhooks down — confirm it is running (`arq` logs, `courier_sweep`).
3. If the courier's API is down too, parcels simply stop advancing. After the tenant's SLA the
   sweep flags them and support can chase the hub with the consignment id.

## A courier's status vocabulary changed

An unmapped status never moves a parcel: `apply_event` stores the raw word, flags the shipment and
leaves the state alone. Add the new word to that adapter's `STATUS` map, deploy, then clear the
flags (`POST /api/v1/admin/shipments/{id}/resolve`). Never guess a mapping for a word that might
mean "returned".

## A tenant wants to switch courier

Courier rules are an ordered list: `PUT /api/v1/admin/courier-rules`. Existing parcels stay with
the courier that has them; only new bookings follow the new rules.

## COD statement does not match

Mismatched lines are flagged rather than settled (`GET /api/v1/admin/courier-settlement-lines
?status=mismatch`). Resolve with the courier, then re-import the corrected statement under a new
`statement_ref` — importing the same ref twice is refused on purpose.
