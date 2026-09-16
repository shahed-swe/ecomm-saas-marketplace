---
name: delivery-dispatch-rider
description: Deep procedural knowledge for the first-party delivery subsystem — dispatch scoring algorithm, zone polygon matching, rider assignment with COD-cash ceiling enforcement, delivery state machine transitions, proof-of-delivery verification (OTP/photo/signature stored to private bucket), COD cash collection and rider_cash_ledger management, remittance settlement into balanced ledger groups, live tracking via Redis streams, SLA timer enforcement, and failed-attempt/return handling. Load this skill when implementing Phase 9 delivery sub-phases or debugging dispatch, rider, or COD cash flows.
---

# Delivery, Dispatch & Rider — Procedural Skill

## 1. Dispatch engine (ARQ + Redis)

### 1.1 Trigger
A `shipment` with `method=platform` transitions to `ready_for_dispatch`. An ARQ
job `dispatch_delivery` is enqueued.

### 1.2 Zone matching
```python
async def match_zone(address: Address) -> DeliveryZone | None:
    # Option A: postcode set
    zone = await zone_repo.find_by_postcode(address.postcode)
    # Option B: polygon containment (PostGIS or application-level)
    if not zone:
        zone = await zone_repo.find_containing_point(address.lat, address.lng)
    if not zone or not zone.is_active:
        return None  # not serviceable — escalate to admin
    return zone
```

### 1.3 Rider scoring
```python
async def score_riders(zone: DeliveryZone, is_cod: bool, cod_amount: Decimal):
    riders = await rider_repo.find_available_in_zone(zone.id)
    scored = []
    for rider in riders:
        # COD ceiling check
        if is_cod and rider.cod_cash_on_hand + cod_amount > rider.cod_cash_ceiling:
            continue  # skip — over ceiling

        score = (
            zone_proximity(rider, zone) * 0.4 +
            inverse_load(rider) * 0.3 +
            rider.rating * 0.3
        )
        scored.append((rider, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
```

### 1.4 Assignment
```python
async def assign_delivery(sub_order_id, zone, rider):
    delivery = Delivery(
        sub_order_id=sub_order_id,
        vendor_id=sub_order.vendor_id,
        rider_id=rider.id,
        zone_id=zone.id,
        status='assigned',
        assigned_at=utcnow(),
        cod_amount=sub_order.cod_amount or 0,
        sla_deadline=utcnow() + timedelta(minutes=zone.sla_minutes),
    )
    await delivery_repo.create(delivery)
    rider.active_load += 1
    await rider_repo.update(rider)
    # Notify rider via push
    await notify_rider_assigned(rider, delivery)
```

### 1.5 Retry on no-match
If no eligible rider: re-enqueue `dispatch_delivery` with 5-minute delay, max 6
attempts. After 6 failures → escalate to admin dispatch console and send admin
alert.

---

## 2. Delivery state machine

```
pending → assigned → accepted → picked_up → in_transit → arrived → delivered
                                                          ↕
                                              failed_attempt → (reschedule or return)
                                                                        ↓
                                                              returned_to_vendor
```

### 2.1 Transition rules
| From | To | Requires | Side effects |
|---|---|---|---|
| pending | assigned | dispatch assigns rider | push to rider |
| assigned | accepted | rider taps accept | update `accepted_at` |
| accepted | picked_up | rider confirms pickup | update `picked_up_at`, start tracking |
| picked_up | in_transit | automatic after pickup | — |
| in_transit | arrived | rider nears destination | update ETA |
| arrived | delivered | **valid PoD** | `delivered_at`, COD collect (if COD), push to buyer |
| arrived | failed_attempt | buyer unavailable | increment `attempt_count` |
| failed_attempt | assigned | reschedule (attempt < 3) | re-dispatch, new SLA |
| failed_attempt | returned_to_vendor | attempt ≥ 3 | stock reversal, ledger reversal, push vendor |

### 2.2 Invariant
**No transition to `delivered` without `proof_verified = true`.** This is enforced
in the service layer, not just the router.

---

## 3. Proof-of-delivery (PoD)

### 3.1 OTP method
```python
async def generate_pod_otp(delivery_id):
    otp = generate_6_digit()
    hashed = argon2.hash(otp)
    await pod_repo.create(ProofOfDelivery(
        delivery_id=delivery_id,
        type='otp',
        otp_hash=hashed,
        verified=False,
    ))
    await send_sms_to_buyer(delivery.buyer_phone, otp)

async def verify_pod_otp(delivery_id, submitted_otp):
    pod = await pod_repo.get_by_delivery(delivery_id)
    if not argon2.verify(pod.otp_hash, submitted_otp):
        raise AppError("Invalid OTP")
    pod.verified = True
    delivery.proof_verified = True
    delivery.proof_type = 'otp'
```

### 3.2 Photo + signature method
```python
async def upload_pod_photo(delivery_id, photo_bytes, signature_bytes):
    # Upload to PRIVATE bucket (same security as KYC)
    photo_key = f"pod/{delivery_id}/photo_{uuid7()}.webp"
    sig_key = f"pod/{delivery_id}/sig_{uuid7()}.webp"

    await upload_to_private_bucket(photo_key, photo_bytes)
    await upload_to_private_bucket(sig_key, signature_bytes)

    pod = ProofOfDelivery(
        delivery_id=delivery_id,
        type='photo',
        storage_key=photo_key,  # + sig_key in metadata
        verified=True,  # upload success = verified
    )
    await pod_repo.create(pod)
    delivery.proof_verified = True
    delivery.proof_type = 'photo'
```

### 3.3 Private bucket rule
PoD assets are **never** served through CDN. Admin/dispute access uses short-lived
signed URLs (same as KYC documents).

---

## 4. COD cash flow

### 4.1 Collection (rider delivers COD order)
```python
async def record_cod_collection(delivery):
    # rider_cash_ledger entry
    await rider_cash_repo.create(RiderCashEntry(
        rider_id=delivery.rider_id,
        delivery_id=delivery.id,
        direction='collect',
        amount=delivery.cod_amount,
        balance_after=rider.cod_cash_on_hand + delivery.cod_amount,
    ))
    rider.cod_cash_on_hand += delivery.cod_amount
    await rider_repo.update(rider)

    # Update receivable
    receivable = await cod_repo.get_by_delivery(delivery.id)
    receivable.status = 'collected'
    receivable.collected_at = utcnow()
```

### 4.2 Remittance (rider remits to platform)
```python
async def process_remittance(rider_id, delivery_ids):
    total = Decimal(0)
    for delivery_id in delivery_ids:
        delivery = await delivery_repo.get(delivery_id)
        receivable = await cod_repo.get_by_delivery(delivery_id)

        # rider_cash_ledger entry
        await rider_cash_repo.create(RiderCashEntry(
            rider_id=rider_id,
            delivery_id=delivery_id,
            direction='remit',
            amount=delivery.cod_amount,
            balance_after=rider.cod_cash_on_hand - delivery.cod_amount,
        ))
        rider.cod_cash_on_hand -= delivery.cod_amount
        total += delivery.cod_amount

        # Settle the receivable → balanced ledger group
        commission = receivable.sub_order.commission_amount
        vendor_net = delivery.cod_amount - commission

        await ledger.post_group([
            LedgerEntry(account='cod_receivable', direction='debit',
                        amount=delivery.cod_amount, vendor_id=receivable.vendor_id),
            LedgerEntry(account='vendor_payable', direction='credit',
                        amount=vendor_net, vendor_id=receivable.vendor_id),
            LedgerEntry(account='platform_revenue', direction='credit',
                        amount=commission),
        ])
        # assert group sums to zero

        receivable.status = 'remitted'
        receivable.remitted_at = utcnow()

    await rider_repo.update(rider)
```

### 4.3 Nightly reconciliation
```python
async def reconcile_rider_cash():
    for rider in await rider_repo.all_active():
        expected = sum(collects) - sum(remits)  # from rider_cash_ledger
        actual = rider.cod_cash_on_hand  # cached field

        if expected != actual:
            alert(f"Rider {rider.id}: expected {expected}, cached {actual}")

        # Also cross-check against cod_receivables
        unremitted = await cod_repo.sum_collected_unremitted(rider.id)
        if unremitted != expected:
            alert(f"Rider {rider.id}: ledger {expected} vs receivables {unremitted}")
```

---

## 5. Live tracking

### 5.1 Rider location updates
```python
# Rider app posts every 10s while in_transit
async def update_rider_location(rider_id, lat, lng, heading, speed):
    # Primary: Redis stream (ephemeral, TTL 1h)
    await redis.xadd(f"rider:{rider_id}:location", {
        'lat': lat, 'lng': lng, 'heading': heading,
        'speed': speed, 'ts': utcnow().isoformat()
    }, maxlen=360)  # ~1 hour of 10s updates

    # Audit: sample to DB every 60s
    if should_sample(rider_id):
        await location_repo.create(RiderLocation(
            rider_id=rider_id, lat=lat, lng=lng,
            heading=heading, speed=speed,
        ))
```

### 5.2 Buyer-facing endpoint
```python
@router.get("/tracking/{delivery_id}")
async def get_tracking(delivery_id: UUID, buyer: CurrentUser):
    delivery = await delivery_repo.get(delivery_id)
    # Verify buyer owns this order
    assert delivery.sub_order.order.user_id == buyer.id

    location = await redis.xrevrange(
        f"rider:{delivery.rider_id}:location", count=1
    )
    return TrackingResponse(
        status=delivery.status,
        rider_name=delivery.rider.display_name,  # no phone/personal info
        location=location,
        eta=compute_eta(delivery, location),
    )
```

---

## 6. Failed delivery and returns

```python
async def handle_failed_attempt(delivery):
    delivery.attempt_count += 1
    delivery.status = 'failed_attempt'

    if delivery.attempt_count >= 3:
        delivery.status = 'returned_to_vendor'
        # Reverse stock reservation
        await inventory_service.release_reservation(delivery.sub_order_id)
        # If COD receivable exists, cancel it
        receivable = await cod_repo.get_by_delivery(delivery.id)
        if receivable:
            receivable.status = 'cancelled'
        # Post reversing ledger entries if any were made
        await ledger.reverse_group(delivery.sub_order.capture_group_id)
    else:
        # Reschedule: re-enter dispatch queue
        await dispatch_service.reschedule(delivery)
```
