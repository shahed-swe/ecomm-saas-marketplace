"""Pathao Courier (Merchant API v1): OAuth token -> create order -> webhook / order info.

Credentials: {client_id, client_secret, username, password, store_id}.
Pathao addresses need city/zone/area ids; we resolve them from `geo_courier_areas` before booking
and pass them through `Address.area` as "city:zone:area" when known.
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

SANDBOX = "https://courier-api-sandbox.pathao.com"
LIVE = "https://api-hermes.pathao.com"
STATUS = {
    "pickup_requested": "booked",
    "assigned_for_pickup": "booked",
    "picked": "picked_up",
    "pickup": "picked_up",
    "at_the_sorting_hub": "in_transit",
    "in_transit": "in_transit",
    "received_at_last_mile_hub": "in_transit",
    "assigned_for_delivery": "out_for_delivery",
    "delivered": "delivered",
    "partial_delivery": "partial_delivered",
    "delivery_failed": "failed_attempt",
    "return_requested": "returning",
    "on_the_way_to_return": "returning",
    "returned": "returned",
    "pickup_cancelled": "cancelled",
    "delivery_cancelled": "cancelled",
}


class PathaoCourier:
    name = "pathao"

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

    async def _token(self, c: httpx.AsyncClient, cr: dict) -> str:
        r = await c.post(
            f"{self.base}/aladdin/api/v1/issue-token",
            json={
                "client_id": cr["client_id"],
                "client_secret": cr["client_secret"],
                "username": cr["username"],
                "password": cr["password"],
                "grant_type": "password",
            },
        )
        data = r.json()
        if not data.get("access_token"):
            raise CourierError(f"Pathao token failed: {data.get('message', r.status_code)}")
        return data["access_token"]

    def _ids(self, to: Address) -> dict:
        city, zone, area = (to.area or "::").split(":") if to.area else ("", "", "")
        return {
            "recipient_city": int(city) if city.isdigit() else None,
            "recipient_zone": int(zone) if zone.isdigit() else None,
            "recipient_area": int(area) if area.isdigit() else None,
        }

    async def quote(self, *, credentials, to: Address, weight_grams: int, cod_amount: Decimal):
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/aladdin/api/v1/merchant/price-plan",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "store_id": credentials["store_id"],
                    "item_type": 2,
                    "delivery_type": 48,
                    "item_weight": max(weight_grams, 500) / 1000,
                    **self._ids(to),
                },
            )
            data = r.json().get("data") or {}
            return Decimal(str(data["price"])) if data.get("price") is not None else None

    async def book(
        self, *, credentials, pickup_ref, to: Address, reference, weight_grams, cod_amount, note
    ) -> Booking:
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/aladdin/api/v1/orders",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "store_id": pickup_ref or credentials["store_id"],
                    "merchant_order_id": reference,
                    "recipient_name": to.name,
                    "recipient_phone": to.phone,
                    "recipient_address": f"{to.line}, {to.upazila or ''}, {to.district}".strip(
                        ", "
                    ),
                    "delivery_type": 48,
                    "item_type": 2,
                    "item_quantity": 1,
                    "item_weight": max(weight_grams, 500) / 1000,
                    "amount_to_collect": float(cod_amount),
                    "special_instruction": (note or "")[:250],
                    **self._ids(to),
                },
            )
            data = r.json().get("data") or {}
            if not data.get("consignment_id"):
                raise CourierError(
                    f"Pathao booking failed: {r.json().get('message', r.status_code)}"
                )
            return Booking(
                consignment_id=str(data["consignment_id"]),
                tracking_code=str(data.get("consignment_id")),
                tracking_url=f"https://merchant.pathao.com/tracking?consignment_id={data['consignment_id']}",
                delivery_fee=Decimal(str(data["delivery_fee"]))
                if data.get("delivery_fee")
                else None,
                raw=data,
            )

    async def cancel(self, *, credentials, consignment_id: str) -> None:
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.post(
                f"{self.base}/aladdin/api/v1/orders/{consignment_id}/cancel",
                headers={"authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                raise CourierError(f"Pathao cancel failed: {r.status_code}")

    async def track(self, *, credentials, consignment_id: str) -> CourierEvent:
        async with self._session() as c:
            token = await self._token(c, credentials)
            r = await c.get(
                f"{self.base}/aladdin/api/v1/orders/{consignment_id}/info",
                headers={"authorization": f"Bearer {token}"},
            )
            data = r.json().get("data") or {}
            raw_status = str(data.get("order_status", ""))
            return CourierEvent(
                event_id=f"poll:{consignment_id}:{raw_status}",
                consignment_id=consignment_id,
                raw_status=raw_status,
                status=normalise(STATUS, raw_status),
                occurred_at=datetime.now(UTC),
                payload=data,
            )

    async def label(self, *, credentials, consignment_id: str) -> bytes:
        raise CourierError("Pathao prints labels in its own merchant panel")

    def parse_webhook(self, *, credentials, body: bytes, form: dict, headers: dict) -> CourierEvent:
        cid = form.get("consignment_id") or form.get("merchant_order_id")
        raw_status = str(form.get("order_status") or form.get("event") or "")
        if not cid:
            raise CourierError("Pathao webhook without consignment_id")
        return CourierEvent(
            event_id=str(
                form.get("event_id") or f"{cid}:{raw_status}:{form.get('updated_at', '')}"
            ),
            consignment_id=str(cid),
            raw_status=raw_status,
            status=normalise(STATUS, raw_status),
            payload=dict(form),
        )

    async def fetch_settlements(self, *, credentials, since: datetime) -> list[SettlementLine]:
        return []  # Pathao publishes statements in the merchant panel; CSV import is used

    async def health(self, *, credentials) -> None:
        async with self._session() as c:
            await self._token(c, credentials)
