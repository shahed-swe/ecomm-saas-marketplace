"""RedX Merchant API: bearer access token, parcel create, parcel track.

Credentials: {access_token, pickup_store_id}.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.modules.fulfilment.couriers.base import (
    Address,
    Booking,
    CourierError,
    CourierEvent,
    SettlementLine,
    normalise,
)

SANDBOX = "https://sandbox.redx.com.bd/v1.0.0-beta"
LIVE = "https://openapi.redx.com.bd/v1.0.0-beta"
STATUS = {
    "pickup-pending": "booked",
    "ready-for-pickup": "booked",
    "picked-up": "picked_up",
    "pickup_pending": "booked",
    "picked_up": "picked_up",
    "in-transit": "in_transit",
    "received-at-hub": "in_transit",
    "agent-assigned": "out_for_delivery",
    "out-for-delivery": "out_for_delivery",
    "delivered": "delivered",
    "delivery-failed": "failed_attempt",
    "returned-to-merchant": "returned",
    "return-in-transit": "returning",
    "cancelled": "cancelled",
}


class RedxCourier:
    name = "redx"

    def __init__(self, mode: str = "sandbox", client: httpx.AsyncClient | None = None):
        self.base = SANDBOX if mode == "sandbox" else LIVE
        self._client = client

    @asynccontextmanager
    async def _session(self):
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=25) as c:
            yield c

    def _headers(self, cr: dict) -> dict:
        return {"API-ACCESS-TOKEN": f"Bearer {cr['access_token']}"}

    async def quote(self, *, credentials, to: Address, weight_grams: int, cod_amount: Decimal):
        return None

    async def book(
        self, *, credentials, pickup_ref, to: Address, reference, weight_grams, cod_amount, note
    ) -> Booking:
        async with self._session() as c:
            r = await c.post(
                f"{self.base}/parcel",
                headers=self._headers(credentials),
                json={
                    "customer_name": to.name,
                    "customer_phone": to.phone,
                    "delivery_area": to.area or to.upazila or to.district,
                    "delivery_area_id": int(to.area) if (to.area or "").isdigit() else None,
                    "customer_address": to.line,
                    "merchant_invoice_id": reference,
                    "cash_collection_amount": str(cod_amount),
                    "parcel_weight": max(weight_grams, 500),
                    "instruction": (note or "")[:250],
                    "value": str(cod_amount or 0),
                    "pickup_store_id": pickup_ref or credentials.get("pickup_store_id"),
                },
            )
            data = r.json()
            tracking = (data.get("tracking_id") or "") if isinstance(data, dict) else ""
            if not tracking:
                raise CourierError(f"RedX booking failed: {data.get('message', r.status_code)}")
            return Booking(
                consignment_id=str(tracking),
                tracking_code=str(tracking),
                tracking_url=f"https://redx.com.bd/track-parcel/?trackingId={tracking}",
                raw=data,
            )

    async def cancel(self, *, credentials, consignment_id: str) -> None:
        async with self._session() as c:
            r = await c.put(
                f"{self.base}/parcel/cancel/{consignment_id}", headers=self._headers(credentials)
            )
            if r.status_code >= 400:
                raise CourierError(f"RedX cancel failed: {r.status_code}")

    async def track(self, *, credentials, consignment_id: str) -> CourierEvent:
        async with self._session() as c:
            r = await c.get(
                f"{self.base}/parcel/track/{consignment_id}", headers=self._headers(credentials)
            )
            data = r.json()
            events = data.get("tracking") or data.get("data") or []
            latest = events[-1] if isinstance(events, list) and events else {}
            raw_status = str(latest.get("message_en") or latest.get("status") or "")
            return CourierEvent(
                event_id=f"poll:{consignment_id}:{raw_status}",
                consignment_id=consignment_id,
                raw_status=raw_status,
                status=normalise(STATUS, raw_status),
                occurred_at=datetime.now(UTC),
                payload=data if isinstance(data, dict) else {"data": data},
            )

    async def label(self, *, credentials, consignment_id: str) -> bytes:
        raise CourierError("RedX prints labels in its own merchant panel")

    def parse_webhook(self, *, credentials, body: bytes, form: dict, headers: dict) -> CourierEvent:
        cid = form.get("tracking_id") or form.get("trackingId") or form.get("consignment_id")
        raw_status = str(form.get("status") or form.get("message_en") or "")
        if not cid:
            raise CourierError("RedX webhook without tracking id")
        return CourierEvent(
            event_id=str(form.get("event_id") or f"{cid}:{raw_status}:{form.get('time', '')}"),
            consignment_id=str(cid),
            raw_status=raw_status,
            status=normalise(STATUS, raw_status),
            payload=dict(form),
        )

    async def fetch_settlements(self, *, credentials, since: datetime) -> list[SettlementLine]:
        return []

    async def health(self, *, credentials) -> None:
        async with self._session() as c:
            r = await c.get(f"{self.base}/pickup/stores", headers=self._headers(credentials))
            if r.status_code >= 400:
                raise CourierError(f"RedX credentials rejected ({r.status_code})")
