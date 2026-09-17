"""Steadfast Courier API: api-key/secret headers, create order, status by consignment or invoice.

Credentials: {api_key, secret_key}.
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

BASE = "https://portal.steadfast.com.bd/api/v1"
STATUS = {
    "pending": "booked",
    "in_review": "booked",
    "delivered_approval_pending": "in_transit",
    "partial_delivered_approval_pending": "in_transit",
    "cancelled_approval_pending": "in_transit",
    "unknown_approval_pending": "in_transit",
    "hold": "in_transit",
    "in_transit": "in_transit",
    "delivered": "delivered",
    "partial_delivered": "partial_delivered",
    "cancelled": "cancelled",
    "return": "returned",
    "returned": "returned",
    # "unknown" is deliberately absent: Steadfast uses it for parcels nobody can account for,
    # which is exactly when a human must look rather than a state machine guess.
}


class SteadfastCourier:
    name = "steadfast"

    def __init__(self, mode: str = "sandbox", client: httpx.AsyncClient | None = None):
        self.base = BASE
        self._client = client

    @asynccontextmanager
    async def _session(self):
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=25) as c:
            yield c

    def _headers(self, cr: dict) -> dict:
        return {
            "Api-Key": cr["api_key"],
            "Secret-Key": cr["secret_key"],
            "Content-Type": "application/json",
        }

    async def quote(self, *, credentials, to: Address, weight_grams: int, cod_amount: Decimal):
        return None  # Steadfast prices per merchant contract, not per request

    async def book(
        self, *, credentials, pickup_ref, to: Address, reference, weight_grams, cod_amount, note
    ) -> Booking:
        async with self._session() as c:
            r = await c.post(
                f"{self.base}/create_order",
                headers=self._headers(credentials),
                json={
                    "invoice": reference,
                    "recipient_name": to.name,
                    "recipient_phone": to.phone,
                    "recipient_address": f"{to.line}, {to.area or ''} {to.upazila or ''}, {to.district}",
                    "cod_amount": float(cod_amount),
                    "note": (note or "")[:250],
                },
            )
            data = r.json()
            consignment = data.get("consignment") or {}
            if str(data.get("status")) != "200" or not consignment.get("consignment_id"):
                raise CourierError(
                    f"Steadfast booking failed: {data.get('message', r.status_code)}"
                )
            return Booking(
                consignment_id=str(consignment["consignment_id"]),
                tracking_code=consignment.get("tracking_code"),
                tracking_url=f"https://steadfast.com.bd/t/{consignment.get('tracking_code', '')}",
                raw=consignment,
            )

    async def cancel(self, *, credentials, consignment_id: str) -> None:
        raise CourierError("Steadfast cancels bookings from the merchant portal")

    async def track(self, *, credentials, consignment_id: str) -> CourierEvent:
        async with self._session() as c:
            r = await c.get(
                f"{self.base}/status_by_cid/{consignment_id}", headers=self._headers(credentials)
            )
            data = r.json()
            raw_status = str(data.get("delivery_status", ""))
            return CourierEvent(
                event_id=f"poll:{consignment_id}:{raw_status}",
                consignment_id=consignment_id,
                raw_status=raw_status,
                status=normalise(STATUS, raw_status),
                occurred_at=datetime.now(UTC),
                payload=data,
            )

    async def label(self, *, credentials, consignment_id: str) -> bytes:
        raise CourierError("Steadfast prints labels in its own portal")

    def parse_webhook(self, *, credentials, body: bytes, form: dict, headers: dict) -> CourierEvent:
        cid = form.get("consignment_id") or form.get("cid")
        raw_status = str(form.get("status") or form.get("delivery_status") or "")
        if not cid:
            raise CourierError("Steadfast webhook without consignment_id")
        return CourierEvent(
            event_id=str(
                form.get("notification_id") or f"{cid}:{raw_status}:{form.get('updated_at', '')}"
            ),
            consignment_id=str(cid),
            raw_status=raw_status,
            status=normalise(STATUS, raw_status),
            payload=dict(form),
        )

    async def fetch_settlements(self, *, credentials, since: datetime) -> list[SettlementLine]:
        return []

    async def health(self, *, credentials) -> None:
        async with self._session() as c:
            r = await c.get(f"{self.base}/get_balance", headers=self._headers(credentials))
            if r.status_code >= 400:
                raise CourierError(f"Steadfast credentials rejected ({r.status_code})")
